"""Branch choice from settled rounds, and the 'unknown is not zero' rule."""

import pytest

from squeek_saga.rounds import (
    Branch,
    choose_branch,
    current_round,
    db_int,
    plan_next_window,
    price_then_steer,
    read_window,
    round_end_time,
)

SPRINT = 1400


def test_price_then_steer_matches_the_documented_integer_rule():
    # U_next = U + floor(w * 10^12 / D); D_next = floor((29*D + floor(w*1000/40)) / 30) when w >= 40
    units, work, difficulty = price_then_steer(0, 0, 10875, 100)
    assert units == 100 * 10**12 // 10875 == 9_195_402_298
    assert work == 100
    assert difficulty == (29 * 10875 + 2500) // 30 == 10595


def test_small_rounds_earn_progress_without_steering():
    units, work, difficulty = price_then_steer(5, 7, 10875, 39)
    assert units == 5 + 39 * 10**12 // 10875
    assert work == 46
    assert difficulty == 10875  # under 40 work: difficulty unchanged


def test_price_then_steer_stays_exact_for_uint96_sized_totals():
    big = 2**95
    units, _, _ = price_then_steer(big, 2**62, 9657, 10000)
    assert units == big + 10000 * 10**12 // 9657
    assert isinstance(units, int)


def test_current_round_follows_the_docs_formula():
    assert current_round(999, 1000, 600) == 0  # before genesis
    assert current_round(1000, 1000, 600) == 1
    assert current_round(1599, 1000, 600) == 1
    assert current_round(1600, 1000, 600) == 2
    assert round_end_time(6, 1000, 600) == 4600


# -- which rounds the next chapter covers ----------------------------------------------


def test_no_chapter_while_the_hour_is_not_fully_certified():
    # Rounds 7..12 are needed; only 7..11 are certified. Round 12 is unknown, so we wait.
    assert plan_next_window(cursor=6, frontier=11, max_hours=6) is None


def test_chapter_as_soon_as_six_rounds_are_certified():
    plan = plan_next_window(cursor=6, frontier=12, max_hours=6)
    assert (plan.first_round, plan.last_round, plan.hours, plan.skipped_rounds) == (7, 12, 1, 0)


def test_partial_extra_rounds_wait_for_their_own_hour():
    plan = plan_next_window(cursor=6, frontier=17, max_hours=6)
    assert (plan.first_round, plan.last_round) == (7, 12)


def test_several_certified_hours_become_one_chapter():
    plan = plan_next_window(cursor=0, frontier=18, max_hours=6)
    assert (plan.first_round, plan.last_round, plan.hours) == (1, 18, 3)


def test_long_outage_becomes_a_time_skip_not_a_burst():
    plan = plan_next_window(cursor=0, frontier=60, max_hours=6)  # ten hours waiting
    assert plan.skipped_rounds == 24
    assert (plan.first_round, plan.last_round, plan.hours) == (25, 60, 6)


def test_frontier_behind_cursor_is_not_a_chapter():
    assert plan_next_window(cursor=12, frontier=6, max_hours=6) is None


# -- reading the hour's work -----------------------------------------------------------


def test_unknown_round_makes_the_whole_reading_unknown_not_zero():
    reading = read_window(7, 12, frontier=11, active_work={7: 300, 8: 200})
    assert reading is None
    assert choose_branch(reading, SPRINT) is None  # unknown never becomes "slept"


def test_certified_round_without_a_record_is_zero_work():
    reading = read_window(7, 12, frontier=12, active_work={})
    assert reading is not None
    assert reading.work == 0 and reading.active_rounds == 0
    assert choose_branch(reading, SPRINT) is Branch.SLEPT


def test_work_outside_the_window_is_ignored():
    reading = read_window(7, 12, frontier=20, active_work={6: 999, 7: 10, 12: 5, 13: 999})
    assert reading.work == 15 and reading.active_rounds == 2 and reading.peak_round_work == 10


@pytest.mark.parametrize(
    "works, expected",
    [
        ({}, Branch.SLEPT),
        ({7: 1}, Branch.TROTTED),
        ({7: 400, 8: 400, 9: 400, 10: 199}, Branch.TROTTED),  # 1399 < 1400
        ({7: 400, 8: 400, 9: 400, 10: 200}, Branch.SPRINTED),  # exactly the threshold
        ({7: 600, 8: 600, 9: 600}, Branch.SPRINTED),
    ],
)
def test_branch_from_settled_work(works, expected):
    assert choose_branch(read_window(7, 12, 12, works), SPRINT) is expected


def test_sprint_threshold_scales_with_merged_hours():
    # 1500 work over two hours is a trot (needs 2800 to count as a sprint).
    reading = read_window(1, 12, 12, {1: 750, 8: 750})
    assert reading.hours == 2
    assert choose_branch(reading, SPRINT) is Branch.TROTTED
    assert choose_branch(read_window(1, 12, 12, {1: 1400, 8: 1400}), SPRINT) is Branch.SPRINTED


def test_invalid_range_is_rejected():
    with pytest.raises(ValueError):
        read_window(0, 5, 10, {})


def test_db_int_refuses_values_that_would_not_round_trip():
    assert db_int(2**63 - 1) == 2**63 - 1
    with pytest.raises(OverflowError):
        db_int(2**63)  # a uint64 above the signed range is never silently truncated
    with pytest.raises(TypeError):
        db_int(1.0)
