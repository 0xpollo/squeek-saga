"""Read-only access to SqueekCore on Ethereum Sepolia.

Nothing here signs or sends a transaction; there is no key to hold.
Every read is pinned to one Ethereum block (``tip - confirmations``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from eth_utils import event_abi_to_log_topic, keccak, to_checksum_address
from web3 import AsyncHTTPProvider, AsyncWeb3

from .db import Store
from .deployment import Deployment
from .ledger import (
    LedgerMismatch,
    Rollback,
    apply_logs,
    apply_round_page,
    current_position,
    record_read,
    rollback_to,
)
from .rounds import BatchSettled, LedgerPosition, LogPosition, RoundCheckpoint, RoundRecorded

ROUND_PAGE_MAX_RECORDS = 16  # SqueekCore rejects more than 16 records per page

log = logging.getLogger(__name__)

# EIP-1967 slots, as used by the docs' verify.ts.
IMPLEMENTATION_SLOT = 0x360894A13BA1A3210667C828492DB98DCA3E2076CC3735A920A3CA505D382BBC
ADMIN_SLOT = 0xB53127684A568B3173AE13B9F8A6016E243E63B6E8EE1178D6A717850B5D6103

# ProxyAdmin is OpenZeppelin's; the docs' verify.ts reads its owner() the same way.
OWNER_ABI = [
    {"type": "function", "name": "owner", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]}
]


class VerificationFailed(RuntimeError):
    """The chain does not match the published deployment record. Stop reading."""


class ReorgDuringRead(RuntimeError):
    """The pinned block changed hash while we were reading it."""


def _hex(value: Any) -> str:
    return "0x" + bytes(value).hex()


def _same(a: str, b: str) -> bool:
    return a.lower() == b.lower()


@dataclass(frozen=True)
class CoreState:
    settled_through_round: int
    cumulative_work: int
    cumulative_block_units: int
    block_height: int
    block_progress: int
    work_per_thousand_blocks: int
    paused: bool


@dataclass(frozen=True)
class Snapshot:
    """What one sync established, all at a single Ethereum block."""

    block_number: int
    block_hash: str
    block_timestamp: int
    frontier: int
    state: CoreState
    rollback: Rollback | None = None


class ChainReader:
    def __init__(self, rpc_url: str, deployment: Deployment, core_abi: list[dict]):
        self.deployment = deployment
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        self.core_address = to_checksum_address(deployment.core_address)
        self.core = self.w3.eth.contract(address=self.core_address, abi=core_abi)
        events = {item["name"]: item for item in core_abi if item.get("type") == "event"}
        for name in ("RoundRecorded", "BatchSettled"):
            if name not in events:
                raise VerificationFailed(f"published SqueekCore ABI has no {name} event")
        self._topic_round = _hex(event_abi_to_log_topic(events["RoundRecorded"]))
        self._topic_batch = _hex(event_abi_to_log_topic(events["BatchSettled"]))

    async def close(self) -> None:
        await self.w3.provider.disconnect()

    # -- identity ----------------------------------------------------------------------
    async def verify_chain(self) -> None:
        chain_id = await self.w3.eth.chain_id
        if chain_id != self.deployment.chain_id:
            raise VerificationFailed(f"RPC is on chain {chain_id}, expected {self.deployment.chain_id}")

    async def _slot_address(self, slot: int, block: int) -> str:
        word = await self.w3.eth.get_storage_at(self.core_address, slot, block_identifier=block)
        return to_checksum_address(bytes(word)[-20:])

    async def verify_proxy_slots(self, block: int) -> str:
        """Cheap check run on every tick: chain id and the proxy's implementation and admin slots."""
        await self.verify_chain()
        implementation = await self._slot_address(IMPLEMENTATION_SLOT, block)
        admin = await self._slot_address(ADMIN_SLOT, block)
        if not _same(implementation, self.deployment.core_implementation):
            raise VerificationFailed("SqueekCore implementation differs from the deployment record")
        if not _same(admin, self.deployment.core_proxy_admin):
            raise VerificationFailed("SqueekCore ProxyAdmin differs from the deployment record")
        return implementation

    async def verify_core(self, block: int) -> None:
        """Full SqueekCore identity check, following the docs' "Verify the deployment".

        We only read SqueekCore, so this verifies SqueekCore: chain id, EIP-1967
        implementation and admin slots, runtime code hash, storage-layout digest,
        ProxyAdmin owner (the admin Timelock), and the launch configuration the
        saga's round arithmetic depends on.
        """
        d = self.deployment
        implementation = await self.verify_proxy_slots(block)
        code = await self.w3.eth.get_code(implementation, block_identifier=block)
        if not code or not _same(_hex(keccak(bytes(code))), d.core_runtime_hash):
            raise VerificationFailed("SqueekCore runtime code hash differs from the deployment record")
        reported = await self.core.functions.implementationCodeAddress().call(block_identifier=block)
        if not _same(reported, implementation):
            raise VerificationFailed("implementationCodeAddress() differs from the proxy slot")
        layout = await self.core.functions.implementationStorageLayoutDigest().call(block_identifier=block)
        if not _same(_hex(layout), d.core_layout_digest):
            raise VerificationFailed("storage-layout digest differs from the deployment record")
        admin = self.w3.eth.contract(address=to_checksum_address(d.core_proxy_admin), abi=OWNER_ABI)
        owner = await admin.functions.owner().call(block_identifier=block)
        if not _same(owner, d.timelock):
            raise VerificationFailed("ProxyAdmin is not owned by the admin Timelock")

        config = self._named(await self.core.functions.configuration().call(block_identifier=block), "configuration")
        expected = {
            "genesisTimestamp": d.genesis_timestamp,
            "roundDuration": d.round_duration,
            "maxWorkPerRound": d.max_work_per_round,
            "maxRoundsPerBatch": d.max_rounds_per_batch,
            "blockUnitsPerBlock": d.block_units_per_block,
            "initialWorkPerThousandBlocks": d.initial_work_per_thousand_blocks,
        }
        for field, value in expected.items():
            if int(config[field]) != value:
                raise VerificationFailed(f"configuration().{field} differs from the deployment record")
        if not _same(_hex(config["streamId"]), d.stream_id):
            raise VerificationFailed("configuration().streamId differs from the deployment record")

    def _named(self, values: Any, function_name: str) -> dict[str, Any]:
        """Map a returned tuple to its ABI component names, never by remembered position."""
        abi = next(
            item for item in self.core.abi
            if item.get("type") == "function" and item.get("name") == function_name
        )
        components = abi["outputs"][0]["components"]
        if len(components) != len(values):
            raise VerificationFailed(f"{function_name}() returned an unexpected tuple")
        return {component["name"]: value for component, value in zip(components, values)}

    # -- reads ---------------------------------------------------------------------------
    async def latest_block_number(self) -> int:
        return int(await self.w3.eth.block_number)

    async def block_header(self, number: int) -> tuple[str, int]:
        block = await self.w3.eth.get_block(number)
        return _hex(block["hash"]), int(block["timestamp"])

    async def core_state(self, block: int) -> CoreState:
        raw = self._named(await self.core.functions.state().call(block_identifier=block), "state")
        state = CoreState(
            settled_through_round=int(raw["settledThroughRound"]),
            cumulative_work=int(raw["cumulativeWork"]),
            cumulative_block_units=int(raw["cumulativeBlockUnits"]),
            block_height=int(raw["blockHeight"]),
            block_progress=int(raw["blockProgress"]),
            work_per_thousand_blocks=int(raw["workPerThousandBlocks"]),
            paused=bool(raw["paused"]),
        )
        units = self.deployment.block_units_per_block
        if (
            state.block_height != state.cumulative_block_units // units
            or state.block_progress != state.cumulative_block_units % units
        ):
            raise LedgerMismatch("state(): block height/progress inconsistent with block units")
        return state

    async def round_page(
        self, after_round: int, through_round: int, block: int
    ) -> tuple[int, LedgerPosition, LedgerPosition, list[RoundCheckpoint]]:
        """SqueekCore roundPage: active rounds after ``after_round``, with both end positions.

        Returns (certifiedThroughRound, start, end, rounds). A page may stop before
        ``through_round``; callers continue from the returned certifiedThroughRound.
        """
        raw = self._named(
            await self.core.functions.roundPage(after_round, through_round, ROUND_PAGE_MAX_RECORDS).call(
                block_identifier=block
            ),
            "roundPage",
        )

        def position(values: Any) -> LedgerPosition:
            cumulative_work, cumulative_block_units, difficulty = values
            return LedgerPosition(int(cumulative_work), int(cumulative_block_units), int(difficulty))

        checkpoints = [
            RoundCheckpoint(
                round_id=int(round_id),
                cumulative_work=int(cumulative_work),
                cumulative_block_units=int(cumulative_block_units),
                difficulty_after=int(difficulty),
                data_hash=_hex(data_hash),
            )
            for round_id, cumulative_work, cumulative_block_units, difficulty, data_hash in raw["rounds"]
        ]
        return int(raw["certifiedThroughRound"]), position(raw["start"]), position(raw["end"]), checkpoints

    async def fetch_events(self, from_block: int, to_block: int) -> tuple[list[RoundRecorded], list[BatchSettled]]:
        logs = await self.w3.eth.get_logs(
            {
                "address": self.core_address,
                "fromBlock": from_block,
                "toBlock": to_block,
                "topics": [[self._topic_round, self._topic_batch]],
            }
        )
        rounds: list[RoundRecorded] = []
        batches: list[BatchSettled] = []
        for entry in logs:
            # A matching topic is not enough: only the verified proxy's logs count.
            if not _same(entry["address"], self.core_address):
                continue
            if entry.get("removed"):
                continue
            topic0 = _hex(entry["topics"][0])
            position = LogPosition(
                block_number=int(entry["blockNumber"]),
                block_hash=_hex(entry["blockHash"]),
                tx_hash=_hex(entry["transactionHash"]),
                tx_index=int(entry["transactionIndex"]),
                log_index=int(entry["logIndex"]),
            )
            if topic0 == self._topic_round:
                args = self.core.events.RoundRecorded().process_log(entry)["args"]
                rounds.append(
                    RoundRecorded(
                        round_id=int(args["roundId"]),
                        work=int(args["work"]),
                        cumulative_work=int(args["cumulativeWork"]),
                        cumulative_block_units=int(args["cumulativeBlockUnits"]),
                        difficulty_after=int(args["workPerThousandBlocks"]),
                        data_hash=_hex(args["dataHash"]),
                        position=position,
                    )
                )
            elif topic0 == self._topic_batch:
                args = self.core.events.BatchSettled().process_log(entry)["args"]
                batches.append(
                    BatchSettled(
                        settled_through_round=int(args["settledThroughRound"]),
                        previous_settled_through_round=int(args["previousSettledThroughRound"]),
                        active_round_count=int(args["activeRoundCount"]),
                        cumulative_work=int(args["cumulativeWork"]),
                        cumulative_block_units=int(args["cumulativeBlockUnits"]),
                        nonce=int(args["nonce"]),
                        position=position,
                    )
                )
        return rounds, batches


async def detect_reorg(reader: ChainReader, store: Store) -> Rollback | None:
    """Compare the block hashes of our past reads with the canonical chain.

    If the newest read is no longer canonical, walk back to the newest one that
    is (the common ancestor we know of) and unwind everything read after it.
    """
    newest = store.one("SELECT * FROM reads ORDER BY block_number DESC LIMIT 1")
    if newest is None:
        return None
    canonical, _ = await reader.block_header(newest["block_number"])
    if _same(canonical, newest["block_hash"]):
        return None

    log.warning("reorg detected: block %s changed hash", newest["block_number"])
    good_block, good_frontier = 0, 0
    for row in store.all("SELECT * FROM reads ORDER BY block_number DESC LIMIT 2000 OFFSET 1"):
        canonical, _ = await reader.block_header(row["block_number"])
        if _same(canonical, row["block_hash"]):
            good_block, good_frontier = row["block_number"], row["frontier"]
            break
    return rollback_to(store, good_block, good_frontier)


async def sync_ledger(
    reader: ChainReader,
    store: Store,
    *,
    confirmations: int,
    chunk_blocks: int,
    log_lookback_blocks: int,
    full_verify: bool,
) -> Snapshot:
    """Bring the local ledger up to ``tip - confirmations``, everything read at that one block."""
    deployment = reader.deployment
    tip = await reader.latest_block_number()
    target = tip - confirmations
    if target < deployment.deployment_block:
        raise VerificationFailed("chain tip is below the SqueekCore deployment block")
    target_hash, target_timestamp = await reader.block_header(target)

    if full_verify:
        await reader.verify_core(target)
    else:
        await reader.verify_proxy_slots(target)

    rollback = await detect_reorg(reader, store)

    # 1. State: every active round up to the certified frontier, via roundPage.
    state = await reader.core_state(target)
    chain_frontier = state.settled_through_round
    local_frontier = store.frontier()
    if chain_frontier < local_frontier:
        raise LedgerMismatch(f"state() frontier {chain_frontier} is behind ours {local_frontier}")
    position = current_position(store, deployment.initial_work_per_thousand_blocks)
    checkpoints: list[RoundCheckpoint] = []
    cursor = local_frontier
    while cursor < chain_frontier:
        certified_through, start, end, page = await reader.round_page(cursor, chain_frontier, target)
        if start != position:
            raise LedgerMismatch(f"roundPage start after round {cursor} differs from our position")
        if certified_through <= cursor or certified_through > chain_frontier:
            raise LedgerMismatch("roundPage did not advance")
        checkpoints.extend(page)
        position, cursor = end, certified_through
    expected_end = LedgerPosition(
        state.cumulative_work, state.cumulative_block_units, state.work_per_thousand_blocks
    )
    apply_round_page(
        store,
        chain_id=deployment.chain_id,
        checkpoints=checkpoints,
        new_frontier=chain_frontier,
        expected_end=expected_end,
        initial_difficulty=deployment.initial_work_per_thousand_blocks,
        max_work_per_round=deployment.max_work_per_round,
        read_block=target,
        read_block_hash=target_hash,
    )

    # 2. Logs: the settlement transaction behind each recent round, cross-checked with step 1.
    indexed = store.get_meta_int("indexed_block", 0)
    window_floor = max(deployment.deployment_block, target - log_lookback_blocks)
    start_block = max(indexed + 1, window_floor)
    contiguous = indexed > 0 and start_block == indexed + 1
    while start_block <= target:
        end_block = min(start_block + chunk_blocks - 1, target)
        rounds, batches = await reader.fetch_events(start_block, end_block)
        apply_logs(store, chain_id=deployment.chain_id, rounds=rounds, batches=batches, contiguous=contiguous)
        store.set_meta("indexed_block", end_block)
        contiguous = True
        start_block = end_block + 1

    latest = store.latest_settlement()
    if chain_frontier > local_frontier and (latest is None or latest["through_round"] != chain_frontier):
        # The frontier moved since our last read, so a BatchSettled for it must be in the window.
        if indexed > 0 and target - indexed <= log_lookback_blocks:
            raise LedgerMismatch("frontier advanced but its BatchSettled log was not found")

    # 3. The docs: fetch the block again after your reads; a changed hash is a reorg mid-read.
    hash_after, _ = await reader.block_header(target)
    if not _same(hash_after, target_hash):
        raise ReorgDuringRead(f"block {target} changed hash during the read")
    record_read(store, target, target_hash, chain_frontier)

    return Snapshot(
        block_number=target,
        block_hash=target_hash,
        block_timestamp=target_timestamp,
        frontier=chain_frontier,
        state=state,
        rollback=rollback,
    )
