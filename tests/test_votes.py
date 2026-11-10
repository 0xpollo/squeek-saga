"""Vote tally: most votes wins, the wheel breaks ties, and it is deterministic."""

import pytest

from squeek_saga.votes import tally


def test_clear_winner():
    result = tally([2, 5, 1], wheel_work=0)
    assert result.winner == 1
    assert result.total == 8
    assert result.decided_by == "votes"
    assert not result.tied


def test_single_vote_wins():
    assert tally([0, 0, 1], wheel_work=123).winner == 2


def test_two_way_tie_is_broken_by_settled_work():
    # Options 0 and 2 are tied; work modulo 2 picks among them in option order.
    assert tally([3, 1, 3], wheel_work=10).winner == 0
    assert tally([3, 1, 3], wheel_work=11).winner == 2
    assert tally([3, 1, 3], wheel_work=11).decided_by == "wheel_tiebreak"


def test_three_way_tie():
    assert [tally([2, 2, 2], wheel_work=w).winner for w in (0, 1, 2, 3)] == [0, 1, 2, 0]


def test_no_votes_lets_the_wheel_pick():
    result = tally([0, 0, 0], wheel_work=4)
    assert result.winner == 1  # 4 % 3
    assert result.total == 0
    assert result.decided_by == "wheel_no_votes"


def test_a_sleeping_hamster_still_resolves_a_tie():
    # Zero settled work is a valid tiebreak value: sleep moves the story too.
    assert tally([1, 1, 0], wheel_work=0).winner == 0


def test_tiebreak_never_picks_a_losing_option():
    for work in range(50):
        assert tally([4, 0, 4], wheel_work=work).winner in (0, 2)


def test_same_inputs_same_outcome():
    assert tally([1, 1, 1], 777) == tally([1, 1, 1], 777)


def test_huge_work_values_are_fine():
    assert tally([1, 1], wheel_work=2**64 + 1).winner == 1


@pytest.mark.parametrize("counts, work", [([], 0), ([1, -1], 0), ([1, 1], -5)])
def test_invalid_input_is_rejected(counts, work):
    with pytest.raises(ValueError):
        tally(counts, work)
