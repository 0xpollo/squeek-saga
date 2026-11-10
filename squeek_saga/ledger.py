"""The local projection of SqueekCore, and how it survives reorgs.

Two on-chain sources feed it, both read at one pinned Ethereum block:

* ``roundPage`` (state view) gives every active round up to the certified
  frontier. It is the source of per-round work: work(r) is the difference of
  consecutive ``cumulativeWork`` values, and each round is re-priced with the
  PREVIOUS position's difficulty and must reproduce the returned totals.
* ``BatchSettled`` / ``RoundRecorded`` logs give the settlement transaction
  that certified each round, and are cross-checked against the state reads.

Rules from docs.squeekchain.com that are enforced here:
- rounds above the frontier are unknown, never zero;
- every BatchSettled advances the frontier, including zero-work batches, and
  consecutive settlements must chain without a gap;
- derived state is rolled back to the common ancestor on a reorg.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .db import Store
from .rounds import BatchSettled, LedgerPosition, RoundCheckpoint, RoundRecorded, db_int, price_then_steer

READS_KEPT = 2000


class LedgerMismatch(RuntimeError):
    """Our projection disagrees with the chain. Never patched over: the saga pauses."""


def current_position(store: Store, initial_difficulty: int) -> LedgerPosition:
    row = store.last_round_row()
    if row is None:
        return LedgerPosition(0, 0, initial_difficulty)
    return LedgerPosition(row["cumulative_work"], int(row["cumulative_block_units"]), row["difficulty_after"])


def apply_round_page(
    store: Store,
    *,
    chain_id: int,
    checkpoints: Sequence[RoundCheckpoint],
    new_frontier: int,
    expected_end: LedgerPosition,
    initial_difficulty: int,
    max_work_per_round: int,
    read_block: int,
    read_block_hash: str,
) -> int:
    """Advance the ledger to ``new_frontier`` with the active rounds read from roundPage.

    Atomic: either every round is stored and the frontier moves, or nothing changes.
    Returns the number of active rounds added.
    """
    frontier = store.frontier()
    if new_frontier < frontier:
        raise LedgerMismatch(f"chain frontier {new_frontier} is behind ours {frontier}")
    position = current_position(store, initial_difficulty)
    rows = []
    previous_round = frontier
    for checkpoint in checkpoints:
        if checkpoint.round_id <= previous_round:
            raise LedgerMismatch(f"round {checkpoint.round_id} is not after round {previous_round}")
        if checkpoint.round_id > new_frontier:
            raise LedgerMismatch(f"round {checkpoint.round_id} lies beyond the certified frontier")
        work = checkpoint.cumulative_work - position.cumulative_work
        if work <= 0 or work > max_work_per_round:
            raise LedgerMismatch(f"round {checkpoint.round_id}: implausible work {work}")
        units, work_total, difficulty = price_then_steer(
            position.cumulative_block_units, position.cumulative_work, position.difficulty, work
        )
        if units != checkpoint.cumulative_block_units or difficulty != checkpoint.difficulty_after:
            raise LedgerMismatch(f"round {checkpoint.round_id}: re-priced totals differ from roundPage")
        position = LedgerPosition(work_total, units, difficulty)
        rows.append(
            (
                db_int(checkpoint.round_id), db_int(work), db_int(checkpoint.cumulative_work),
                str(checkpoint.cumulative_block_units), db_int(checkpoint.difficulty_after),
                checkpoint.data_hash, chain_id, db_int(read_block), read_block_hash,
            )
        )
        previous_round = checkpoint.round_id
    if position != expected_end:
        raise LedgerMismatch("replayed position differs from state() at the pinned block")

    with store.conn:
        store.conn.executemany(
            "INSERT INTO rounds (round_id, work, cumulative_work, cumulative_block_units, difficulty_after, "
            "data_hash, chain_id, read_block, read_block_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        store.set_meta("frontier", db_int(new_frontier), commit=False)
    return len(rows)


def apply_logs(
    store: Store,
    *,
    chain_id: int,
    rounds: Sequence[RoundRecorded],
    batches: Sequence[BatchSettled],
    contiguous: bool = True,
) -> int:
    """Store settlements from a fetched log range and cross-check them with the state reads.

    Call after ``apply_round_page`` for the same pinned block. ``contiguous`` is False
    when the range does not start right after the previous one (first run, or after an
    outage longer than the RPC's log history): the first settlement then has no stored
    predecessor to chain to. Returns settlements added.
    """
    frontier = store.frontier()
    events: list[RoundRecorded | BatchSettled] = sorted(
        [*rounds, *batches], key=lambda event: event.position.order
    )
    latest = store.latest_settlement()
    held = latest["through_round"] if (latest and contiguous) else None  # None: start of a log window
    pending: list[RoundRecorded] = []
    added = 0
    with store.conn:
        for event in events:
            if isinstance(event, RoundRecorded):
                row = store.conn.execute(
                    "SELECT work, cumulative_work, cumulative_block_units, difficulty_after, data_hash "
                    "FROM rounds WHERE round_id = ?",
                    (event.round_id,),
                ).fetchone()
                if row is None:
                    raise LedgerMismatch(f"RoundRecorded for round {event.round_id} that roundPage did not return")
                if (
                    row["work"] != event.work
                    or row["cumulative_work"] != event.cumulative_work
                    or int(row["cumulative_block_units"]) != event.cumulative_block_units
                    or row["difficulty_after"] != event.difficulty_after
                    or row["data_hash"].lower() != event.data_hash.lower()
                ):
                    raise LedgerMismatch(f"RoundRecorded for round {event.round_id} differs from roundPage")
                pending.append(event)
                continue

            if held is not None and event.previous_settled_through_round != held:
                raise LedgerMismatch(
                    f"settlement starts at {event.previous_settled_through_round} but we hold {held}: "
                    "a settlement was missed"
                )
            if event.settled_through_round <= event.previous_settled_through_round:
                raise LedgerMismatch("settlement does not advance the frontier")
            if event.settled_through_round > frontier:
                raise LedgerMismatch("settlement log is ahead of state() at the pinned block")
            if event.active_round_count != len(pending):
                raise LedgerMismatch(
                    f"settlement reports {event.active_round_count} active rounds, saw {len(pending)}"
                )
            for recorded in pending:
                if recorded.position.tx_hash != event.position.tx_hash:
                    raise LedgerMismatch("RoundRecorded does not belong to its settlement transaction")
                if not event.previous_settled_through_round < recorded.round_id <= event.settled_through_round:
                    raise LedgerMismatch("a recorded round lies outside its settlement's span")
            stored_active = store.conn.execute(
                "SELECT COUNT(*) AS n FROM rounds WHERE round_id > ? AND round_id <= ?",
                (event.previous_settled_through_round, event.settled_through_round),
            ).fetchone()["n"]
            if stored_active != event.active_round_count:
                raise LedgerMismatch("settlement's active round count differs from roundPage")
            store.conn.execute(
                "INSERT INTO settlements (chain_id, block_number, block_hash, tx_hash, tx_index, log_index, "
                "previous_through_round, through_round, active_round_count, cumulative_work, cumulative_block_units) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chain_id, db_int(event.position.block_number), event.position.block_hash,
                    event.position.tx_hash, event.position.tx_index, event.position.log_index,
                    db_int(event.previous_settled_through_round), db_int(event.settled_through_round),
                    event.active_round_count, db_int(event.cumulative_work), str(event.cumulative_block_units),
                ),
            )
            held = event.settled_through_round
            pending = []
            added += 1
        if pending:
            # A range ends on a whole block and BatchSettled closes its transaction's RoundRecorded logs.
            raise LedgerMismatch("RoundRecorded without a closing BatchSettled")
    return added


def record_read(store: Store, block_number: int, block_hash: str, frontier: int) -> None:
    """Remember what the frontier was at a pinned block, so a reorg can be unwound to it."""
    with store.conn:
        store.conn.execute(
            "INSERT OR REPLACE INTO reads (block_number, block_hash, frontier) VALUES (?, ?, ?)",
            (db_int(block_number), block_hash, db_int(frontier)),
        )
        store.conn.execute(
            "DELETE FROM reads WHERE block_number NOT IN "
            "(SELECT block_number FROM reads ORDER BY block_number DESC LIMIT ?)",
            (READS_KEPT,),
        )


@dataclass(frozen=True)
class Rollback:
    to_block: int  # last block still canonical (0 when nothing we read survives)
    frontier: int
    orphaned_chapters: int


def rollback_to(store: Store, good_block: int, good_frontier: int) -> Rollback:
    """Unwind everything derived from reads after ``good_block``."""
    with store.conn:
        store.conn.execute("DELETE FROM reads WHERE block_number > ?", (good_block,))
        store.conn.execute("DELETE FROM rounds WHERE round_id > ?", (good_frontier,))
        store.conn.execute("DELETE FROM settlements WHERE block_number > ?", (good_block,))
        store.set_meta("frontier", good_frontier, commit=False)
        store.set_meta("indexed_block", good_block, commit=False)

        # Chapters told from rounds or settlements that no longer exist are withdrawn,
        # and the saga resumes from the last round that is still certified.
        orphaned = store.conn.execute(
            "UPDATE chapters SET status = 'orphaned' WHERE status = 'live' "
            "AND (last_round > ? OR settlement_block > ?)",
            (good_frontier, good_block),
        ).rowcount
        if orphaned:
            last_live = store.conn.execute(
                "SELECT COALESCE(MAX(last_round), 0) AS c FROM chapters WHERE status = 'live'"
            ).fetchone()["c"]
            start = store.get_meta_int("saga_start_cursor", 0)
            store.set_meta("saga_cursor", max(last_live, start), commit=False)
        store.set_meta("glory_dirty", 1, commit=False)
    return Rollback(to_block=good_block, frontier=good_frontier, orphaned_chapters=orphaned)
