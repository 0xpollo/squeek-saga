"""The saga worker: follows settled rounds and turns each certified hour into a chapter.

One tick does, in order:
1. sync the ledger from SqueekCore at one pinned block (verify, state, logs, reorg check);
2. refresh the weekly raid and the glory derived from it;
3. if a whole saga hour is now certified, close the vote, pick the branch from the
   settled work and write the next chapter.

Anything that cannot be established from the chain leaves the saga waiting. A
round above the certified frontier is unknown: no chapter is written from it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from .chain import ChainReader, ReorgDuringRead, Snapshot, VerificationFailed, sync_ledger
from .characters import (
    CLASSES,
    DEFAULT_CLASS,
    PREP_ACTIONS,
    Candidate,
    pick_featured,
    sanitize_name,
    to_featured,
)
from .config import Settings
from .db import Store, now
from .deployment import Deployment
from .ledger import LedgerMismatch
from .raid import (
    PREP_COVER_SECONDS,
    Prep,
    boss_for_week,
    boss_hp,
    glory_for_rounds,
    next_streak,
    raid_progress,
    raid_status,
    raid_week,
    reference_work,
    streak_multiplier_percent,
    week_bounds,
)
from .rounds import (
    ROUNDS_PER_HOUR,
    Branch,
    choose_branch,
    current_round,
    plan_next_window,
    read_window,
    round_end_time,
)
from .story import PREMISE, ChapterRequest, PreviousChoice, RaidBrief, Storyteller
from .votes import VoteResult, tally

log = logging.getLogger(__name__)

MIN_CHAPTER_INTERVAL = 50 * 60  # never more than one storyteller call per saga hour
FULL_VERIFY_INTERVAL = 60 * 60

ChapterListener = Callable[[int], Awaitable[None]]


@dataclass
class EngineStatus:
    state: str = "starting"  # starting | ok | waiting | paused
    message: str = "Starting up."
    synced_at: int = 0
    block_number: int = 0
    block_hash: str = ""
    frontier: int | None = None  # None: not known yet
    cumulative_work: int | None = None
    block_height: int | None = None
    core_paused: bool | None = None
    next_first_round: int | None = None
    next_last_round: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "message": self.message,
            "syncedAt": self.synced_at,
            "ethereumBlock": str(self.block_number) if self.block_number else None,
            "ethereumBlockHash": self.block_hash or None,
            "settledThroughRound": None if self.frontier is None else str(self.frontier),
            "cumulativeWork": None if self.cumulative_work is None else str(self.cumulative_work),
            "squeekBlockHeight": None if self.block_height is None else str(self.block_height),
            "corePaused": self.core_paused,
            "nextChapterRounds": (
                None
                if self.next_first_round is None
                else [str(self.next_first_round), str(self.next_last_round)]
            ),
        }


def utc_day(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class SagaEngine:
    settings: Settings
    deployment: Deployment
    store: Store
    reader: ChainReader | None
    storyteller: Storyteller
    status: EngineStatus = field(default_factory=EngineStatus)
    listeners: list[ChapterListener] = field(default_factory=list)
    clock: Callable[[], int] = now
    _last_full_verify: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _background: set = field(default_factory=set)

    # -- the loop ------------------------------------------------------------------------
    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # keep the worker alive; the status says what is wrong
                log.exception("saga tick failed")
                self.status.state, self.status.message = "paused", "Unexpected error; retrying shortly."
            await asyncio.sleep(self.settings.tick_seconds)

    async def tick(self) -> int | None:
        """Run one cycle. Returns the id of a chapter written this tick, if any."""
        async with self._lock:
            snapshot = await self._sync()
            if snapshot is None:
                return None
            self.refresh_raids(snapshot.block_timestamp)
            chapter_id = await self._maybe_write_chapter(snapshot)
        if chapter_id is not None:
            # Announcing a chapter (Discord posts) must never hold up the worker.
            for listener in list(self.listeners):
                task = asyncio.create_task(self._notify(listener, chapter_id))
                self._background.add(task)
                task.add_done_callback(self._background.discard)
        return chapter_id

    @staticmethod
    async def _notify(listener: ChapterListener, chapter_id: int) -> None:
        try:
            await asyncio.wait_for(listener(chapter_id), timeout=300)
        except Exception:
            log.exception("chapter listener failed")

    async def _sync(self) -> Snapshot | None:
        if self.reader is None:
            self.status.state, self.status.message = "paused", "No chain reader configured."
            return None
        full = time.monotonic() - self._last_full_verify > FULL_VERIFY_INTERVAL or not self._last_full_verify
        try:
            snapshot = await sync_ledger(
                self.reader,
                self.store,
                confirmations=self.settings.confirmations,
                chunk_blocks=self.settings.log_chunk_blocks,
                log_lookback_blocks=self.settings.log_lookback_blocks,
                full_verify=full,
            )
        except VerificationFailed as exc:
            # The chain no longer matches the published record: stop reading, say so.
            log.error("deployment verification failed: %s", exc)
            self.status.state = "paused"
            self.status.message = "The contracts no longer match the published deployment record. Saga paused."
            return None
        except LedgerMismatch as exc:
            log.error("ledger mismatch: %s", exc)
            self.status.state = "paused"
            self.status.message = "Chain data did not reconcile. Saga paused until it does."
            return None
        except ReorgDuringRead:
            self.status.state, self.status.message = "waiting", "The chain reorganised mid-read; retrying."
            return None
        except Exception as exc:  # RPC outage, timeout, rate limit...
            log.warning("chain read failed: %r", exc)
            self.status.state = "waiting"
            self.status.message = "Cannot reach the chain right now. The wheel's state is unknown, not zero."
            return None
        if full:
            self._last_full_verify = time.monotonic()
        if snapshot.rollback:
            log.warning("rolled back to block %s (frontier %s, %s chapters withdrawn)",
                        snapshot.rollback.to_block, snapshot.rollback.frontier,
                        snapshot.rollback.orphaned_chapters)

        status = self.status
        status.synced_at = self.clock()
        status.block_number, status.block_hash = snapshot.block_number, snapshot.block_hash
        status.frontier = snapshot.frontier
        status.cumulative_work = snapshot.state.cumulative_work
        status.block_height = snapshot.state.block_height
        status.core_paused = snapshot.state.paused
        status.state, status.message = "ok", "Following settled rounds."
        return snapshot

    # -- raid ----------------------------------------------------------------------------
    def refresh_raids(self, chain_time: int) -> list[tuple[int, str]]:
        """Create this week's raid if needed and update damage, status and glory.

        Returns [(week, event)] for raids that changed state: appeared | defeated | retreated.
        """
        d = self.deployment
        frontier = self.store.frontier()
        wall_round = current_round(chain_time, d.genesis_timestamp, d.round_duration)
        events: list[tuple[int, str]] = []
        if wall_round < 1:
            return events
        week_now = raid_week(wall_round)

        if self.store.get_raid(week_now) is None:
            reference = reference_work(frontier, self.store.active_work(max(1, frontier - 1007), frontier))
            hp = boss_hp(reference, self.settings.raid_hp_percent, self.settings.raid_default_hp,
                         self.settings.raid_min_hp)
            basis = (
                f"{self.settings.raid_hp_percent}% of the {reference} work units settled over rounds "
                f"{frontier - 1007}-{frontier}"
                if reference is not None
                else "default: fewer than 1008 certified rounds were known when the raid began"
            )
            name, blurb = boss_for_week(week_now)
            first, last = week_bounds(week_now)
            self.store.create_raid(week_now, name, blurb, hp, basis, first, last)
            events.append((week_now, "appeared"))

        dirty = self.store.get_meta_int("glory_dirty", 0) == 1
        # Open raids: still active, this week's, or not yet certified to their last round.
        open_raids = self.store.all(
            "SELECT * FROM raids WHERE status = 'active' OR week = ? OR certified_through < last_round",
            (week_now,),
        )
        if dirty:
            open_raids = self.store.all("SELECT * FROM raids")
        for raid in open_raids:
            week = raid["week"]
            first, last = raid["first_round"], raid["last_round"]
            work = self.store.active_work(first, last)
            progress = raid_progress(week, frontier, work)
            status = raid_status(raid["hp"], progress)
            changed = (
                dirty
                or progress.certified_through != raid["certified_through"]
                or progress.damage != raid["damage"]
                or status != raid["status"]
            )
            if changed:
                self.store.update_raid(week, progress.damage, progress.certified_through, status)
            if status != raid["status"] and status in ("defeated", "retreated"):
                events.append((week, status))
            if changed or self._prep_revision_changed():
                self._recompute_glory(week, first, progress.certified_through, work)
        if dirty:
            self.store.set_meta("glory_dirty", 0)
        self.store.set_meta("prep_revision_seen", self.store.get_meta_int("prep_revision", 0))
        for week, event in events:
            pending = json.loads(self.store.get_meta("raid_events", "[]"))
            pending.append([week, event])
            self.store.set_meta("raid_events", json.dumps(pending))
        return events

    def _prep_revision_changed(self) -> bool:
        return self.store.get_meta_int("prep_revision", 0) != self.store.get_meta_int("prep_revision_seen", 0)

    def _recompute_glory(self, week: int, first: int, certified_through: int, work: dict[int, int]) -> None:
        """Glory is a pure function of (certified rounds, preparations): recomputed, never patched."""
        d = self.deployment
        rounds = [
            (round_id, amount, round_end_time(round_id, d.genesis_timestamp, d.round_duration))
            for round_id, amount in sorted(work.items())
            if first <= round_id <= certified_through
        ]
        if not rounds:
            self.store.replace_week_glory(week, {})
            return
        preps = [
            Prep(row["user_id"], row["prepared_at"], row["multiplier_percent"])
            for row in self.store.preps_between(rounds[0][2] - PREP_COVER_SECONDS, rounds[-1][2])
        ]
        self.store.replace_week_glory(week, glory_for_rounds(rounds, preps))

    # -- player actions (called by the bot) -------------------------------------------------
    def ensure_player(self, user_id: str, display_name: str, guild_id: str, klass: str | None = None) -> Any:
        name = sanitize_name(display_name, user_id)
        player = self.store.get_player(user_id)
        if player is None:
            self.store.create_player(user_id, name, klass or DEFAULT_CLASS, guild_id)
            self.store.add_deed(user_id, "join", f"joined the company as a {CLASSES[klass or DEFAULT_CLASS][0]}")
        else:
            changes: dict[str, Any] = {}
            if player["name"] != name:
                changes["name"] = name
            if klass and klass != player["class"]:
                changes["class"] = klass
            if klass and player["home_guild_id"] != guild_id:
                changes["home_guild_id"] = guild_id  # an explicit /join moves your home server
            self.store.update_player(user_id, **changes)
        return self.store.get_player(user_id)

    def forget(self, user_id: str) -> bool:
        """Delete everything stored about a player. Published chapters are not rewritten."""
        return self.store.delete_player(user_id)

    def prepare(self, user_id: str, action: str) -> dict[str, Any]:
        """Daily raid preparation. One per UTC day; it covers the next 24 hours of rounds."""
        if action not in PREP_ACTIONS:
            raise ValueError("unknown preparation")
        timestamp = self.clock()
        today = utc_day(timestamp)
        existing = self.store.get_prep(user_id, today)
        player = self.store.get_player(user_id)
        if existing is not None:
            return {"ok": False, "streak": player["prep_streak"], "multiplier": existing["multiplier_percent"]}
        yesterday = utc_day(timestamp - 86400)
        streak = next_streak(player["prep_streak"], player["last_prep_day"], today, yesterday)
        multiplier = streak_multiplier_percent(streak)
        self.store.add_prep(user_id, today, action, timestamp, multiplier)
        self.store.update_player(user_id, prep_streak=streak, last_prep_day=today)
        self.store.set_meta("prep_revision", self.store.get_meta_int("prep_revision", 0) + 1)
        if streak in (3, 5, 7, 14, 30):
            self.store.add_deed(user_id, "prep", f"kept a {streak}-day watch without missing a night")
        return {"ok": True, "streak": streak, "multiplier": multiplier}

    def vote(self, user_id: str, guild_id: str, chapter_id: int, option_idx: int) -> str:
        """Returns 'recorded', 'changed' or 'closed'."""
        latest = self.store.latest_chapter()
        if latest is None or latest["id"] != chapter_id:
            return "closed"
        options = json.loads(latest["options"])
        if not 0 <= option_idx < len(options):
            return "closed"
        first = self.store.cast_vote(chapter_id, user_id, guild_id, option_idx)
        return "recorded" if first else "changed"

    # -- chapters ------------------------------------------------------------------------
    def _close_vote(self, chapter: Any, wheel_work: int) -> tuple[VoteResult, PreviousChoice]:
        options = json.loads(chapter["options"])
        counts = self.store.vote_counts(chapter["id"], len(options))
        result = tally(counts, wheel_work)
        labels = tuple(option["label"] for option in options)
        number = self.store.chapter_number(chapter["id"])
        for voter in self.store.voters(chapter["id"]):
            sided = voter["option_idx"] == result.winner
            verb = "counselled" if sided else "argued for"
            outcome = "and the company agreed" if sided else "though the company chose otherwise"
            self.store.add_deed(
                voter["user_id"], "vote",
                f"{verb} \"{labels[voter['option_idx']]}\" in chapter {number}, {outcome}",
                chapter["id"],
            )
        choice = PreviousChoice(
            prompt=chapter["choice_prompt"], options=labels, winner=result.winner,
            counts=result.counts, decided_by=result.decided_by,
        )
        return result, choice

    def _candidates(self, chapter: Any | None, result: VoteResult | None, at_time: int) -> list[Candidate]:
        candidates: list[Candidate] = []
        if chapter is not None and result is not None:
            labels = [option["label"] for option in json.loads(chapter["options"])]
            for voter in self.store.voters(chapter["id"]):
                sided = voter["option_idx"] == result.winner
                candidates.append(
                    Candidate(
                        user_id=voter["user_id"], name=voter["name"], klass=voter["class"],
                        last_featured_chapter=voter["last_featured_chapter"],
                        did=(
                            f"urged Tamsin to \"{labels[voter['option_idx']]}\""
                            + (", which is what she did" if sided else ", though she chose otherwise")
                        ),
                        priority=0 if sided else 1,
                    )
                )
        for row in self.store.all(
            "SELECT p.user_id, p.name, p.class, p.last_featured_chapter, r.action FROM preps r "
            "JOIN players p ON p.user_id = r.user_id WHERE r.prepared_at <= ? AND r.prepared_at > ?",
            (at_time, at_time - PREP_COVER_SECONDS),
        ):
            candidates.append(
                Candidate(
                    user_id=row["user_id"], name=row["name"], klass=row["class"],
                    last_featured_chapter=row["last_featured_chapter"],
                    did=f"{PREP_ACTIONS.get(row['action'], ('', 'made ready'))[1]} ahead of the weekly boss",
                    priority=2,
                )
            )
        return candidates

    def _raid_brief(self, week: int) -> RaidBrief | None:
        raid = self.store.get_raid(week)
        if raid is None:
            return None
        pending = json.loads(self.store.get_meta("raid_events", "[]"))
        # If several things happened to this boss since the last chapter, tell the weightiest.
        happened = {name for event_week, name in pending if event_week == week}
        event = next((name for name in ("defeated", "retreated", "appeared") if name in happened), "")
        certified = raid["certified_through"] - raid["first_round"] + 1
        percent = None if certified <= 0 else min(100, raid["damage"] * 100 // max(1, raid["hp"]))
        return RaidBrief(raid["boss_name"], raid["boss_blurb"], percent, event)

    async def _maybe_write_chapter(self, snapshot: Snapshot) -> int | None:
        store, status = self.store, self.status
        frontier = snapshot.frontier

        if store.get_meta("saga_cursor") is None:
            # First run: start at the hour boundary at or below the frontier; no backfill.
            start = frontier - frontier % ROUNDS_PER_HOUR
            store.set_meta("saga_cursor", start)
            store.set_meta("saga_start_cursor", start)
        cursor = store.get_meta_int("saga_cursor")

        plan = plan_next_window(cursor, frontier, self.settings.max_catchup_hours)
        status.next_first_round, status.next_last_round = cursor + 1, cursor + ROUNDS_PER_HOUR
        if plan is None:
            status.state = "waiting"
            status.message = (
                f"Next chapter needs rounds {cursor + 1}-{cursor + ROUNDS_PER_HOUR} settled; "
                f"SqueekCore has certified through round {frontier}. Later rounds are unknown, not zero."
            )
            return None
        if self.clock() - store.get_meta_int("last_chapter_at", 0) < MIN_CHAPTER_INTERVAL:
            return None

        reading = read_window(
            plan.first_round, plan.last_round, frontier, store.active_work(plan.first_round, plan.last_round)
        )
        branch = choose_branch(reading, self.settings.sprint_work_per_hour)
        if reading is None or branch is None:
            return None  # cannot happen below the frontier; unknown never becomes a chapter
        settlement = store.settlement_covering(plan.last_round)
        if settlement is None:
            # The settlement log for this hour is outside the RPC's history (long outage).
            # A chapter must link to its settlement, so this hour is passed over.
            log.warning("no settlement log for round %s; skipping rounds %s-%s",
                        plan.last_round, plan.first_round, plan.last_round)
            store.set_meta("saga_cursor", plan.last_round)
            return None

        previous = store.latest_chapter()
        result: VoteResult | None = None
        choice: PreviousChoice | None = None
        if previous is not None:
            result, choice = self._close_vote(previous, reading.work)

        d = self.deployment
        window_end = round_end_time(plan.last_round, d.genesis_timestamp, d.round_duration)
        week = raid_week(plan.last_round)
        chosen = pick_featured(self._candidates(previous, result, window_end))
        memories = {
            c.user_id: [row["text"] for row in store.recent_deeds(c.user_id, 2, kinds=("cameo", "prep", "raid"))]
            for c in chosen
        }
        featured = to_featured(chosen, memories)
        number = store.chapter_count() + 1
        request = ChapterRequest(
            number=number,
            branch=branch,
            hours=plan.hours,
            work=reading.work,
            active_rounds=reading.active_rounds,
            summary=previous["summary"] if previous is not None else PREMISE,
            previous_title=previous["title"] if previous is not None else None,
            previous_body=previous["body"] if previous is not None else None,
            previous_choice=choice,
            raid=self._raid_brief(week),
            featured=featured,
            skipped_hours=plan.skipped_rounds // ROUNDS_PER_HOUR,
        )
        draft = await self.storyteller.write(request)
        if draft.source == "fallback":
            log.warning("chapter %s used the fallback text: %s", number, draft.note)

        chapter_id = store.insert_chapter(
            first_round=plan.first_round,
            last_round=plan.last_round,
            work=reading.work,
            active_rounds=reading.active_rounds,
            branch=branch.value,
            vote_result=(
                None if result is None else {
                    "chapter": previous["id"], "winner": result.winner, "counts": list(result.counts),
                    "decidedBy": result.decided_by, "label": choice.options[result.winner],
                }
            ),
            title=draft.title,
            body=draft.body,
            choice_prompt=draft.choice_prompt,
            options=list(draft.options),
            summary=draft.summary,
            settlement_tx=settlement["tx_hash"],
            settlement_block=settlement["block_number"],
            settlement_hash=settlement["block_hash"],
            read_block=snapshot.block_number,
            read_block_hash=snapshot.block_hash,
            source=draft.source,
            model=draft.model,
            raid_week=week,
            created_at=self.clock(),
        )
        store.set_meta("saga_cursor", plan.last_round)
        store.set_meta("last_chapter_at", self.clock())

        by_ref = {companion.ref: companion for companion in featured}
        for ref, note in draft.cameos:
            companion = by_ref[ref]
            store.add_deed(companion.user_id, "cameo", f"{note} (chapter {number})", chapter_id)
            store.update_player(companion.user_id, last_featured_chapter=chapter_id)

        # Raid events were told in this chapter; players who earned glory that week remember it.
        pending = json.loads(store.get_meta("raid_events", "[]"))
        for event_week, event in pending:
            if event == "defeated":
                raid = store.get_raid(event_week)
                for row in store.all("SELECT user_id FROM raid_glory WHERE week = ?", (event_week,)):
                    store.add_deed(row["user_id"], "raid",
                                   f"stood ready the week {raid['boss_name']} was worn down", chapter_id)
        store.set_meta("raid_events", "[]")

        status.state, status.message = "ok", f"Chapter {number} written from rounds {plan.first_round}-{plan.last_round}."
        status.next_first_round, status.next_last_round = plan.last_round + 1, plan.last_round + ROUNDS_PER_HOUR
        log.info("chapter %s (%s, work %s, rounds %s-%s, tx %s)", number, branch.value, reading.work,
                 plan.first_round, plan.last_round, settlement["tx_hash"])
        return chapter_id

    # -- views shared by the bot and the site ------------------------------------------------
    def raid_view(self) -> dict[str, Any] | None:
        d = self.deployment
        wall_round = current_round(self.clock(), d.genesis_timestamp, d.round_duration)
        if wall_round < 1:
            return None
        raid = self.store.get_raid(raid_week(wall_round)) or (self.store.recent_raids(1) or [None])[0]
        if raid is None:
            return None
        certified = max(0, raid["certified_through"] - raid["first_round"] + 1)
        total = raid["last_round"] - raid["first_round"] + 1
        ends_at = round_end_time(raid["last_round"], d.genesis_timestamp, d.round_duration)
        return {
            "week": raid["week"],
            "bossName": raid["boss_name"],
            "bossBlurb": raid["boss_blurb"],
            "hp": str(raid["hp"]),
            "hpBasis": raid["hp_basis"],
            "damage": str(raid["damage"]),
            "percent": min(100, raid["damage"] * 100 // max(1, raid["hp"])),
            "status": raid["status"],
            "firstRound": str(raid["first_round"]),
            "lastRound": str(raid["last_round"]),
            "roundsCertified": certified,
            "roundsUnknown": total - certified,
            "roundsTotal": total,
            "endsAt": ends_at,
            "readyNow": self.store.active_prep_count(self.clock(), PREP_COVER_SECONDS),
        }

    def next_chapter_eta(self) -> int | None:
        """Wall-clock end of the next chapter's last round (settlement follows a few minutes later)."""
        cursor = self.store.get_meta("saga_cursor")
        if cursor is None:
            return None
        d = self.deployment
        return round_end_time(int(cursor) + ROUNDS_PER_HOUR, d.genesis_timestamp, d.round_duration)
