"""Vote tally. Pure: the same votes and the same wheel always pick the same option."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class VoteResult:
    winner: int  # index of the chosen option
    counts: tuple[int, ...]
    total: int
    decided_by: str  # "votes" | "wheel_tiebreak" | "wheel_no_votes"

    @property
    def tied(self) -> bool:
        return self.decided_by != "votes"


def tally(counts: Sequence[int], wheel_work: int) -> VoteResult:
    """Pick the winning option.

    Most votes wins. A tie (including nobody voting) is broken by the wheel:
    the hour's settled work, taken modulo the number of tied options, picks
    among them in option order. Players decide; the hamster only arbitrates.
    """
    if not counts:
        raise ValueError("a choice needs at least one option")
    if any(c < 0 for c in counts):
        raise ValueError("vote counts cannot be negative")
    if wheel_work < 0:
        raise ValueError("work cannot be negative")
    counts = tuple(int(c) for c in counts)
    total = sum(counts)
    top = max(counts)
    leaders = [index for index, count in enumerate(counts) if count == top]
    if len(leaders) == 1:
        return VoteResult(winner=leaders[0], counts=counts, total=total, decided_by="votes")
    winner = leaders[wheel_work % len(leaders)]
    decided_by = "wheel_no_votes" if total == 0 else "wheel_tiebreak"
    return VoteResult(winner=winner, counts=counts, total=total, decided_by=decided_by)
