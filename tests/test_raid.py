"""Raid rules: only settled work deals damage, unknown rounds are not zero."""

from squeek_saga.raid import (
    Prep,
    boss_for_week,
    boss_hp,
    glory_for_rounds,
    next_streak,
    prep_covers,
    raid_progress,
    raid_status,
    raid_week,
    reference_work,
    streak_multiplier_percent,
    week_bounds,
)


def test_weeks_are_1008_round_spans_from_genesis():
    assert raid_week(1) == 0 and raid_week(1008) == 0 and raid_week(1009) == 1
    assert week_bounds(0) == (1, 1008)
    assert week_bounds(2) == (2017, 3024)


def test_every_week_has_a_boss():
    assert all(boss_for_week(week)[0] for week in range(20))


# -- boss strength -------------------------------------------------------------------------


def test_reference_work_is_unknown_until_a_full_week_is_certified():
    assert reference_work(1007, {5: 500}) is None  # not "500": a week we cannot see is unknown


def test_reference_work_sums_the_last_1008_certified_rounds():
    work = {1: 999, 2: 100, 1009: 50, 1010: 7}
    assert reference_work(1009, work) == 150  # rounds 2..1009; round 1 has aged out, 1010 is uncertified


def test_boss_hp_is_a_share_of_real_recent_work():
    assert boss_hp(50_000, percent=90, default_hp=45_000, minimum_hp=10_000) == 45_000
    assert boss_hp(None, percent=90, default_hp=45_000, minimum_hp=10_000) == 45_000  # unknown -> default
    assert boss_hp(0, percent=90, default_hp=45_000, minimum_hp=10_000) == 10_000  # a sleepy week -> floor


# -- damage --------------------------------------------------------------------------------


def test_damage_counts_only_certified_rounds_of_the_week():
    work = {1: 100, 500: 200, 900: 300, 1009: 999}
    progress = raid_progress(0, frontier=600, active_work=work)
    assert progress.damage == 300  # round 900 is above the frontier, round 1009 is next week
    assert progress.rounds_certified == 600
    assert progress.rounds_unknown == 408  # reported as unknown, not as zero-damage rounds
    assert not progress.fully_certified


def test_no_certified_round_yet_means_no_damage_and_everything_unknown():
    progress = raid_progress(1, frontier=1000, active_work={1005: 400})
    assert progress.damage == 0
    assert progress.rounds_certified == 0 and progress.rounds_unknown == 1008
    assert progress.certified_through == 1008


def test_frontier_past_the_week_caps_at_its_last_round():
    progress = raid_progress(0, frontier=5000, active_work={1008: 10, 1009: 10})
    assert progress.damage == 10 and progress.fully_certified and progress.certified_through == 1008


def test_players_cannot_deal_damage():
    # Damage is a function of settled work only: there is no input for player actions.
    assert raid_progress(0, 1008, {}).damage == 0


def test_boss_is_defeated_once_certified_damage_reaches_hp():
    progress = raid_progress(0, frontier=10, active_work={3: 600, 4: 400})
    assert raid_status(1000, progress) == "defeated"
    assert raid_status(1001, progress) == "active"  # the rest of the week is unknown, not lost


def test_boss_retreats_only_when_the_whole_week_is_certified():
    work = {3: 500}
    assert raid_status(1000, raid_progress(0, 1007, work)) == "active"
    assert raid_status(1000, raid_progress(0, 1008, work)) == "retreated"


def test_a_week_of_sleep_ends_peacefully():
    assert raid_status(10_000, raid_progress(0, 1008, {})) == "retreated"


# -- glory ---------------------------------------------------------------------------------


def test_streak_multiplier():
    assert [streak_multiplier_percent(d) for d in (0, 1, 2, 3, 6, 30)] == [100, 100, 110, 120, 150, 150]


def test_streak_progression():
    assert next_streak(0, None, "2026-11-10", "2026-11-09") == 1
    assert next_streak(3, "2026-11-09", "2026-11-10", "2026-11-09") == 4
    assert next_streak(3, "2026-11-07", "2026-11-10", "2026-11-09") == 1  # missed a day
    assert next_streak(3, "2026-11-10", "2026-11-10", "2026-11-09") == 3  # same day


def test_prep_covers_the_following_24_hours():
    assert prep_covers(1000, 1000)
    assert prep_covers(1000, 1000 + 86_399)
    assert not prep_covers(1000, 1000 + 86_400)
    assert not prep_covers(1000, 999)  # preparing after a round ended does not count


def test_glory_goes_to_players_ready_when_work_landed():
    rounds = [(10, 300, 5000), (11, 200, 5600), (12, 0, 6200)]
    preps = [Prep("ann", 4000, 100), Prep("bob", 5300, 120), Prep("late", 9000, 100)]
    glory = glory_for_rounds(rounds, preps)
    assert glory == {"ann": 500, "bob": 240}  # bob only covers round 11, at x1.2


def test_glory_is_not_split_between_players():
    rounds = [(1, 1000, 100)]
    alone = glory_for_rounds(rounds, [Prep("ann", 0, 100)])
    together = glory_for_rounds(rounds, [Prep("ann", 0, 100), Prep("bob", 0, 100)])
    assert alone["ann"] == together["ann"] == 1000


def test_overlapping_preps_use_the_best_multiplier_once():
    rounds = [(1, 1000, 90_000)]
    glory = glory_for_rounds(rounds, [Prep("ann", 10_000, 100), Prep("ann", 80_000, 130)])
    assert glory == {"ann": 1300}


def test_sleeping_rounds_give_no_glory_and_no_penalty():
    assert glory_for_rounds([(1, 0, 100), (2, 0, 700)], [Prep("ann", 0, 150)]) == {}
