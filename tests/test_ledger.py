"""The local projection of SqueekCore: verification, continuity, and reorgs."""

import pytest

from squeek_saga.chain import sync_ledger
from squeek_saga.ledger import LedgerMismatch, apply_logs, apply_round_page
from squeek_saga.rounds import BatchSettled, LedgerPosition, LogPosition, RoundCheckpoint, price_then_steer

SYNC = dict(confirmations=0, chunk_blocks=1000, log_lookback_blocks=3000, full_verify=True)


async def sync(chain, store, **overrides):
    return await sync_ledger(chain, store, **{**SYNC, **overrides})


async def test_sync_reads_active_rounds_and_frontier(chain, store):
    chain.settle(1)
    chain.settle(2, {2: 300})
    chain.settle(3, {3: 150})
    snapshot = await sync(chain, store)
    assert snapshot.frontier == 3
    assert store.frontier() == 3
    assert store.active_work(1, 3) == {2: 300, 3: 150}  # round 1 certified with zero work: no row
    assert snapshot.state.cumulative_work == 450


async def test_zero_work_settlements_still_advance_the_frontier(chain, store):
    for round_id in range(1, 7):
        chain.settle(round_id)
    snapshot = await sync(chain, store)
    assert snapshot.frontier == 6
    assert store.active_work(1, 6) == {}
    assert store.one("SELECT COUNT(*) AS n FROM settlements")["n"] == 6


async def test_each_round_links_to_the_settlement_that_certified_it(chain, store):
    chain.settle(2, {1: 100})
    second = chain.settle(5, {4: 200})
    await sync(chain, store)
    row = store.settlement_covering(4)
    assert row["tx_hash"] == second.tx_hash
    assert store.settlement_covering(3)["tx_hash"] == second.tx_hash  # zero-work round, same batch
    assert store.settlement_covering(6) is None  # not certified: no settlement, not "empty"


async def test_incremental_sync_only_adds_new_rounds(chain, store):
    chain.settle(1, {1: 100})
    await sync(chain, store)
    chain.settle(2, {2: 50})
    snapshot = await sync(chain, store, full_verify=False)
    assert snapshot.frontier == 2
    assert store.active_work(1, 2) == {1: 100, 2: 50}
    assert chain.calls.count("verify_core") == 1 and "verify_proxy_slots" in chain.calls


async def test_more_than_sixteen_active_rounds_are_paged(chain, store):
    chain.settle(40, {r: 100 + r for r in range(1, 41)})
    snapshot = await sync(chain, store)
    assert snapshot.frontier == 40
    assert len(store.active_work(1, 40)) == 40


async def test_confirmations_keep_unconfirmed_settlements_out(chain, store):
    chain.settle(1, {1: 100})
    chain.mine(3)
    chain.settle(2, {2: 999})  # only 0 blocks deep
    snapshot = await sync(chain, store, confirmations=2)
    assert snapshot.frontier == 1
    assert store.active_work(1, 5) == {1: 100}  # round 2 is unknown to us, not zero


async def test_verification_failure_stops_before_any_read(chain, store):
    from squeek_saga.chain import VerificationFailed

    chain.settle(1, {1: 100})
    chain.verified = False
    with pytest.raises(VerificationFailed):
        await sync(chain, store)
    assert store.frontier() == 0 and store.active_work(1, 10) == {}


def _page_args(deployment, **overrides):
    args = dict(
        chain_id=deployment.chain_id, initial_difficulty=10875, max_work_per_round=10000,
        read_block=2000, read_block_hash="0x" + "00" * 32,
    )
    args.update(overrides)
    return args


def test_round_page_with_wrong_block_units_is_rejected(store, deployment):
    units, work, difficulty = price_then_steer(0, 0, 10875, 200)
    bad = RoundCheckpoint(1, work, units + 1, difficulty, "0x" + "11" * 32)
    with pytest.raises(LedgerMismatch):
        apply_round_page(store, checkpoints=[bad], new_frontier=1,
                         expected_end=LedgerPosition(work, units + 1, difficulty), **_page_args(deployment))
    assert store.frontier() == 0  # nothing stored, nothing patched


def test_round_page_must_land_on_state(store, deployment):
    units, work, difficulty = price_then_steer(0, 0, 10875, 200)
    good = RoundCheckpoint(1, work, units, difficulty, "0x" + "11" * 32)
    with pytest.raises(LedgerMismatch):
        apply_round_page(store, checkpoints=[good], new_frontier=1,
                         expected_end=LedgerPosition(work + 5, units, difficulty), **_page_args(deployment))


def test_round_beyond_the_frontier_is_rejected(store, deployment):
    units, work, difficulty = price_then_steer(0, 0, 10875, 200)
    ahead = RoundCheckpoint(9, work, units, difficulty, "0x" + "11" * 32)
    with pytest.raises(LedgerMismatch):
        apply_round_page(store, checkpoints=[ahead], new_frontier=5,
                         expected_end=LedgerPosition(work, units, difficulty), **_page_args(deployment))


def test_work_above_the_per_round_cap_is_rejected(store, deployment):
    units, work, difficulty = price_then_steer(0, 0, 10875, 10001)
    over = RoundCheckpoint(1, work, units, difficulty, "0x" + "11" * 32)
    with pytest.raises(LedgerMismatch):
        apply_round_page(store, checkpoints=[over], new_frontier=1,
                         expected_end=LedgerPosition(work, units, difficulty), **_page_args(deployment))


def _batch(previous, through, block, active=0):
    position = LogPosition(block, "0x" + f"{block:064x}", "0x" + f"{block + 7:064x}", 0, 0)
    return BatchSettled(through, previous, active, 0, 0, 0, position)


def test_a_skipped_settlement_is_detected(store, deployment):
    apply_round_page(store, checkpoints=[], new_frontier=3,
                     expected_end=LedgerPosition(0, 0, 10875), **_page_args(deployment))
    apply_logs(store, chain_id=1, rounds=[], batches=[_batch(0, 1, 2001)])
    with pytest.raises(LedgerMismatch, match="missed"):
        apply_logs(store, chain_id=1, rounds=[], batches=[_batch(2, 3, 2003)])  # 1 -> 2 is missing


def test_first_log_window_may_start_mid_history(store, deployment):
    apply_round_page(store, checkpoints=[], new_frontier=500,
                     expected_end=LedgerPosition(0, 0, 10875), **_page_args(deployment))
    added = apply_logs(store, chain_id=1, rounds=[], batches=[_batch(498, 499, 2001), _batch(499, 500, 2002)],
                       contiguous=False)
    assert added == 2


def test_settlement_ahead_of_state_is_rejected(store, deployment):
    apply_round_page(store, checkpoints=[], new_frontier=2,
                     expected_end=LedgerPosition(0, 0, 10875), **_page_args(deployment))
    with pytest.raises(LedgerMismatch, match="ahead"):
        apply_logs(store, chain_id=1, rounds=[], batches=[_batch(0, 3, 2001)], contiguous=False)


# -- reorgs ------------------------------------------------------------------------------


async def test_reorg_rolls_back_to_the_common_ancestor(chain, store):
    chain.settle(1, {1: 100})
    await sync(chain, store)
    chain.settle(2, {2: 700})
    chain.settle(3, {3: 50})
    await sync(chain, store)
    assert store.frontier() == 3

    chain.reorg(2)  # the blocks holding rounds 2 and 3 are replaced
    chain.settle(2, {2: 5})  # the new history settles round 2 differently
    snapshot = await sync(chain, store)

    assert snapshot.rollback is not None and snapshot.rollback.frontier == 1
    assert store.frontier() == 2
    assert store.active_work(1, 5) == {1: 100, 2: 5}  # replayed from the ancestor, not deduplicated
    assert store.settlement_covering(3) is None


async def test_reorg_without_any_surviving_read_starts_over(chain, store):
    chain.settle(1, {1: 100})
    await sync(chain, store)
    chain.reorg(1)
    chain.settle(1, {1: 42})
    snapshot = await sync(chain, store)
    assert snapshot.rollback.to_block == 0
    assert store.active_work(1, 5) == {1: 42}


async def test_no_reorg_means_no_rollback(chain, store):
    chain.settle(1, {1: 100})
    await sync(chain, store)
    chain.mine(5)
    assert (await sync(chain, store)).rollback is None
