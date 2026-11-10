"""Weekly boss raid rules. Pure functions.

Players prepare; only the hamster's settled work deals damage. Squeektoshi is
never asked to run: a boss that is not worn down by the end of the week simply
wanders off, and that is a legitimate ending, not a failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .rounds import ROUNDS_PER_WEEK

PREP_COVER_SECONDS = 24 * 3600
MAX_STREAK_BONUS_DAYS = 5

BOSSES: tuple[tuple[str, str], ...] = (
    ("The Vacuum Leviathan", "It roars across the carpet plains once a week, swallowing crumbs and courage alike."),
    ("Grimalkin of the Nine Naps", "A great cat-shaped shadow that guards the warm windowsill and yawns thunder."),
    ("The Clockwork Owl", "A brass night-watcher whose ticking eyes count every seed in the burrow."),
    ("Baron Von Drizzle", "A storm cloud in a waistcoat who rains on every picnic out of sheer politeness."),
    ("The Sock Hydra", "For every sock pulled from the laundry cave, two odd ones take its place."),
    ("Madame Static", "She hums in the walls and steals the sparks out of fur and stories."),
    ("The Hollow Gourd King", "A rolling pumpkin monarch who taxes the harvest one sunflower seed at a time."),
    ("Old Frostwhisker", "The winter draft itself, slipping under doors to nip at tails."),
)


def raid_week(round_id: int) -> int:
    """Raid weeks are 1008-round spans counted from genesis. Round 1 is in week 0."""
    if round_id < 1:
        raise ValueError("rounds start at 1")
    return (round_id - 1) // ROUNDS_PER_WEEK


def week_bounds(week: int) -> tuple[int, int]:
    first = week * ROUNDS_PER_WEEK + 1
    return first, first + ROUNDS_PER_WEEK - 1


def boss_for_week(week: int) -> tuple[str, str]:
    return BOSSES[week % len(BOSSES)]


def reference_work(frontier: int, active_work: Mapping[int, int]) -> int | None:
    """Settled work over the 1008 certified rounds ending at the frontier.

    None when fewer than 1008 rounds are certified: a week we cannot fully see
    is unknown, not small.
    """
    if frontier < ROUNDS_PER_WEEK:
        return None
    start = frontier - ROUNDS_PER_WEEK + 1
    return sum(work for round_id, work in active_work.items() if start <= round_id <= frontier)


def boss_hp(reference: int | None, percent: int, default_hp: int, minimum_hp: int) -> int:
    """Boss hit points = a share of what the hamster really did over the last known week."""
    if reference is None:
        return max(minimum_hp, default_hp)
    return max(minimum_hp, reference * percent // 100)


@dataclass(frozen=True)
class RaidProgress:
    week: int
    damage: int  # settled work in the week's certified rounds
    certified_through: int  # last certified round inside the week (first-1 if none yet)
    rounds_certified: int
    rounds_total: int

    @property
    def fully_certified(self) -> bool:
        return self.rounds_certified == self.rounds_total

    @property
    def rounds_unknown(self) -> int:
        return self.rounds_total - self.rounds_certified


def raid_progress(week: int, frontier: int, active_work: Mapping[int, int]) -> RaidProgress:
    """Damage dealt so far. Rounds above the frontier are unknown and are not counted as zero:
    they are reported separately as ``rounds_unknown``."""
    first, last = week_bounds(week)
    certified_through = max(first - 1, min(frontier, last))
    damage = sum(
        work for round_id, work in active_work.items() if first <= round_id <= certified_through
    )
    return RaidProgress(
        week=week,
        damage=damage,
        certified_through=certified_through,
        rounds_certified=certified_through - first + 1,
        rounds_total=ROUNDS_PER_WEEK,
    )


def raid_status(hp: int, progress: RaidProgress) -> str:
    """'defeated' as soon as certified damage reaches HP; 'retreated' only once the whole
    week is certified below HP; otherwise still 'active' (the rest is unknown)."""
    if progress.damage >= hp:
        return "defeated"
    if progress.fully_certified:
        return "retreated"
    return "active"


def streak_multiplier_percent(streak_days: int) -> int:
    """100% on the first day, +10% per extra consecutive day, capped at +50%."""
    bonus_days = max(0, min(streak_days, MAX_STREAK_BONUS_DAYS + 1) - 1)
    return 100 + 10 * bonus_days


def next_streak(previous_streak: int, last_prep_day: str | None, today: str, yesterday: str) -> int:
    if last_prep_day == today:
        return previous_streak
    if last_prep_day == yesterday:
        return previous_streak + 1
    return 1


@dataclass(frozen=True)
class Prep:
    user_id: str
    prepared_at: int  # unix seconds
    multiplier_percent: int


def prep_covers(prepared_at: int, round_end: int) -> bool:
    """A preparation readies a player for the 24 hours that follow it."""
    return prepared_at <= round_end < prepared_at + PREP_COVER_SECONDS


def glory_for_rounds(
    rounds: Iterable[tuple[int, int, int]], preps: Iterable[Prep]
) -> dict[str, int]:
    """Share out glory for settled rounds.

    ``rounds`` yields (round_id, work, round_end_time). Every player whose
    preparation covers a round's end earns that round's settled work times
    their streak multiplier. Glory is not split: being ready when the hamster
    strikes is what counts, and nobody's preparation takes from anyone else's.
    Returns {user_id: glory}. Zero-work rounds give nothing, and so do unknown
    rounds, because callers only pass certified rounds.
    """
    preps = list(preps)
    glory: dict[str, int] = {}
    for _round_id, work, round_end in rounds:
        if work <= 0:
            continue
        best: dict[str, int] = {}
        for prep in preps:
            if prep_covers(prep.prepared_at, round_end):
                best[prep.user_id] = max(best.get(prep.user_id, 0), prep.multiplier_percent)
        for user_id, percent in best.items():
            glory[user_id] = glory.get(user_id, 0) + work * percent // 100
    return glory
