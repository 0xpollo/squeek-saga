"""Pure round arithmetic: no network, no database.

Everything here works on Python ints, which are arbitrary precision, so
uint64 / uint96 chain values are never squeezed into a float or a 53-bit number.

The one rule that shapes this module: a round after SqueekCore's
`settledThroughRound` is UNKNOWN, not zero. Functions that would have to guess
return ``None`` instead of a number.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

ROUNDS_PER_HOUR = 6  # 6 x 600 s
ROUNDS_PER_WEEK = 1008  # 7 x 24 x 6
STEERING_MIN_WORK = 40
SMOOTHING_ROUNDS = 30


class Branch(str, Enum):
    """What the hamster did during a saga hour, as settled on SqueekCore."""

    SLEPT = "slept"
    TROTTED = "trotted"
    SPRINTED = "sprinted"


@dataclass(frozen=True)
class LogPosition:
    block_number: int
    block_hash: str
    tx_hash: str
    tx_index: int
    log_index: int

    @property
    def order(self) -> tuple[int, int, int]:
        return (self.block_number, self.tx_index, self.log_index)


@dataclass(frozen=True)
class RoundRecorded:
    """SqueekCore `RoundRecorded`: one active round. Totals are after the round."""

    round_id: int
    work: int
    cumulative_work: int
    cumulative_block_units: int
    difficulty_after: int
    data_hash: str
    position: LogPosition


@dataclass(frozen=True)
class BatchSettled:
    """SqueekCore `BatchSettled`: advances the certified frontier, even with zero active rounds."""

    settled_through_round: int
    previous_settled_through_round: int
    active_round_count: int
    cumulative_work: int
    cumulative_block_units: int
    nonce: int
    position: LogPosition


@dataclass(frozen=True)
class LedgerPosition:
    """SqueekCore `LedgerPosition`: totals and the difficulty for the next active round."""

    cumulative_work: int
    cumulative_block_units: int
    difficulty: int


@dataclass(frozen=True)
class RoundCheckpoint:
    """SqueekCore `RoundCheckpoint` from `roundPage`: one active round, totals after it."""

    round_id: int
    cumulative_work: int
    cumulative_block_units: int
    difficulty_after: int
    data_hash: str


def price_then_steer(
    block_units: int, cumulative_work: int, difficulty: int, work: int
) -> tuple[int, int, int]:
    """SqueekCore's integer rule for one active round, ported exactly.

    The round is priced at the difficulty in force BEFORE it (the previous
    position's difficulty), then difficulty steers for the next active round.
    """
    next_units = block_units + (work * 10**12) // difficulty
    next_work = cumulative_work + work
    if work < STEERING_MIN_WORK:
        next_difficulty = difficulty
    else:
        next_difficulty = (
            (SMOOTHING_ROUNDS - 1) * difficulty + (work * 1000) // STEERING_MIN_WORK
        ) // SMOOTHING_ROUNDS
    return next_units, next_work, next_difficulty


def current_round(timestamp: int, genesis_timestamp: int, round_duration: int) -> int:
    """Wall-clock round, derived as the docs say (SqueekCore has no currentRound())."""
    if timestamp < genesis_timestamp:
        return 0
    return (timestamp - genesis_timestamp) // round_duration + 1


def round_start_time(round_id: int, genesis_timestamp: int, round_duration: int) -> int:
    return genesis_timestamp + (round_id - 1) * round_duration


def round_end_time(round_id: int, genesis_timestamp: int, round_duration: int) -> int:
    return genesis_timestamp + round_id * round_duration


@dataclass(frozen=True)
class WindowPlan:
    """The rounds the next chapter covers."""

    first_round: int
    last_round: int
    hours: int
    skipped_rounds: int  # rounds passed over without a chapter after a long outage


def plan_next_window(cursor: int, frontier: int, max_hours: int) -> WindowPlan | None:
    """Pick the next chapter's rounds, or None while the next hour is not fully certified.

    ``cursor`` is the last round a chapter already covered. A chapter needs at
    least one whole hour (6 rounds) at or below the certified frontier. If
    several hours were certified at once they are told as one chapter; beyond
    ``max_hours`` the oldest are skipped so a long outage becomes a time skip
    instead of a burst of chapters.
    """
    if frontier < cursor:
        return None
    complete_hours = (frontier - cursor) // ROUNDS_PER_HOUR
    if complete_hours < 1:
        return None
    skipped_hours = max(0, complete_hours - max_hours)
    first = cursor + skipped_hours * ROUNDS_PER_HOUR + 1
    hours = complete_hours - skipped_hours
    return WindowPlan(
        first_round=first,
        last_round=first + hours * ROUNDS_PER_HOUR - 1,
        hours=hours,
        skipped_rounds=skipped_hours * ROUNDS_PER_HOUR,
    )


@dataclass(frozen=True)
class WindowReading:
    first_round: int
    last_round: int
    work: int
    active_rounds: int
    peak_round_work: int

    @property
    def hours(self) -> int:
        return (self.last_round - self.first_round + 1) // ROUNDS_PER_HOUR


def read_window(
    first_round: int, last_round: int, frontier: int, active_work: Mapping[int, int]
) -> WindowReading | None:
    """Sum the settled work of rounds first..last.

    ``active_work`` maps round id -> work for rounds that emitted
    `RoundRecorded`. A certified round absent from it did zero work (settlements
    omit zero-work rounds). A round above ``frontier`` is unknown, so the whole
    reading is unknown: return None, never a partial sum.
    """
    if first_round < 1 or last_round < first_round:
        raise ValueError("invalid round range")
    if last_round > frontier:
        return None
    works = [active_work[r] for r in range(first_round, last_round + 1) if r in active_work]
    return WindowReading(
        first_round=first_round,
        last_round=last_round,
        work=sum(works),
        active_rounds=len(works),
        peak_round_work=max(works, default=0),
    )


def choose_branch(reading: WindowReading | None, sprint_work_per_hour: int) -> Branch | None:
    """Slept / trotted / sprinted from settled work. Unknown stays unknown (None)."""
    if reading is None:
        return None
    if reading.work == 0:
        return Branch.SLEPT
    if reading.work >= sprint_work_per_hour * max(1, reading.hours):
        return Branch.SPRINTED
    return Branch.TROTTED


# SQLite INTEGER is a signed 64-bit value. Round ids and work are uint64 on
# chain; we refuse to store a value that would not round-trip rather than
# truncate it. uint96 totals are stored as decimal text instead.
_SQLITE_MAX = 2**63 - 1


def db_int(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"expected int, got {type(value).__name__}")
    if value < 0 or value > _SQLITE_MAX:
        raise OverflowError(f"{value} does not fit a signed 64-bit column")
    return value
