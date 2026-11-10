"""Test doubles: a tiny in-memory SqueekCore and a scripted storyteller.

FakeChain behaves like the parts of SqueekCore the saga reads: it prices rounds
with the same price-then-steer rule, serves roundPage and state() per block,
emits RoundRecorded/BatchSettled logs, and can reorganise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from squeek_saga.chain import CoreState
from squeek_saga.config import Settings
from squeek_saga.db import Store
from squeek_saga.deployment import parse_deployment
from squeek_saga.engine import SagaEngine
from squeek_saga.rounds import (
    BatchSettled,
    LedgerPosition,
    LogPosition,
    RoundCheckpoint,
    RoundRecorded,
    price_then_steer,
)
from squeek_saga.story import ChapterDraft, ChapterRequest, Storyteller, fallback_chapter

GENESIS = 1_700_000_000

RECORD = {
    "network": "sepolia",
    "chainId": 11155111,
    "release": "test-release",
    "explorer": "https://explorer.example",
    "contracts": {
        "SqueekCore": {
            "address": "0x" + "c0" * 20,
            "deployedAtBlock": 1000,
            "implementation": "0x" + "1a" * 20,
            "implementationRuntimeHash": "0x" + "aa" * 32,
            "storageLayoutDigest": "0x" + "bb" * 32,
            "proxyAdmin": "0x" + "ad" * 20,
        }
    },
    "operational": {"governanceTimelock": {"address": "0x" + "71" * 20}},
    "launchConfiguration": {
        "core": {
            "streamId": "0x" + "5e" * 32,
            "genesisTimestamp": GENESIS,
            "roundDurationSeconds": 600,
            "maxWorkPerRound": 10000,
            "maxRoundsPerBatch": 16,
            "initialWorkPerThousandBlocks": 10875,
            "blockUnitsPerBlock": 1000000000,
        }
    },
}


@pytest.fixture
def deployment():
    return parse_deployment(RECORD)


@dataclass
class _Settlement:
    block: int
    tx_hash: str
    previous: int
    through: int
    rounds: list[RoundCheckpoint]
    works: list[int]
    end: LedgerPosition


class FakeChain:
    """Implements the ChainReader surface used by sync_ledger."""

    def __init__(self, deployment):
        self.deployment = deployment
        self.tip = deployment.deployment_block + 10
        self.salt = 0
        self.salt_from: dict[int, int] = {}  # block -> salt used for its hash
        self.settlements: list[_Settlement] = []
        self.time = GENESIS
        self.verified = True
        self.fail_reads = False
        self.calls: list[str] = []

    # -- building the chain ----------------------------------------------------------
    def _hash(self, block: int) -> str:
        return "0x" + f"{block:056x}" + f"{self.salt_from.get(block, 0):08x}"

    def mine(self, count: int = 1) -> None:
        for _ in range(count):
            self.tip += 1
            self.salt_from[self.tip] = self.salt

    def _position(self) -> LedgerPosition:
        if self.settlements:
            return self.settlements[-1].end
        return LedgerPosition(0, 0, self.deployment.initial_work_per_thousand_blocks)

    def frontier(self) -> int:
        return self.settlements[-1].through if self.settlements else 0

    def settle(self, through: int, works: dict[int, int] | None = None) -> _Settlement:
        """Mine one block holding a settlement that certifies rounds up to ``through``."""
        self.mine()
        position = self._position()
        rounds, amounts = [], []
        for round_id in sorted(works or {}):
            work = works[round_id]
            units, total, difficulty = price_then_steer(
                position.cumulative_block_units, position.cumulative_work, position.difficulty, work
            )
            position = LedgerPosition(total, units, difficulty)
            rounds.append(RoundCheckpoint(round_id, total, units, difficulty, "0x" + f"{round_id:064x}"))
            amounts.append(work)
        settlement = _Settlement(
            block=self.tip,
            tx_hash="0x" + f"{self.tip:060x}" + f"{self.salt:04x}",
            previous=self.frontier(),
            through=through,
            rounds=rounds,
            works=amounts,
            end=position,
        )
        self.settlements.append(settlement)
        return settlement

    def settle_hour(self, first_round: int, works: dict[int, int] | None = None) -> None:
        """Certify six rounds one settlement at a time, like the live Reporter does."""
        works = works or {}
        for round_id in range(first_round, first_round + 6):
            self.settle(round_id, {round_id: works[round_id]} if round_id in works else None)

    def reorg(self, depth: int) -> None:
        """Replace the last ``depth`` blocks with empty ones that have new hashes."""
        fork_point = self.tip - depth
        self.settlements = [s for s in self.settlements if s.block <= fork_point]
        self.salt += 1
        for block in range(fork_point + 1, self.tip + 1):
            self.salt_from[block] = self.salt

    # -- ChainReader surface -----------------------------------------------------------
    async def latest_block_number(self) -> int:
        if self.fail_reads:
            raise ConnectionError("rpc down")
        return self.tip

    async def block_header(self, number: int) -> tuple[str, int]:
        return self._hash(number), self.time

    async def verify_core(self, block: int) -> None:
        self.calls.append("verify_core")
        if not self.verified:
            from squeek_saga.chain import VerificationFailed

            raise VerificationFailed("implementation changed")

    async def verify_proxy_slots(self, block: int) -> str:
        self.calls.append("verify_proxy_slots")
        if not self.verified:
            from squeek_saga.chain import VerificationFailed

            raise VerificationFailed("implementation changed")
        return self.deployment.core_implementation

    def _at(self, block: int) -> list[_Settlement]:
        return [s for s in self.settlements if s.block <= block]

    async def core_state(self, block: int) -> CoreState:
        visible = self._at(block)
        end = visible[-1].end if visible else LedgerPosition(0, 0, self.deployment.initial_work_per_thousand_blocks)
        return CoreState(
            settled_through_round=visible[-1].through if visible else 0,
            cumulative_work=end.cumulative_work,
            cumulative_block_units=end.cumulative_block_units,
            block_height=end.cumulative_block_units // 10**9,
            block_progress=end.cumulative_block_units % 10**9,
            work_per_thousand_blocks=end.difficulty,
            paused=False,
        )

    async def round_page(self, after_round: int, through_round: int, block: int):
        visible = self._at(block)
        frontier = visible[-1].through if visible else 0
        assert after_round <= through_round <= frontier, "InvalidRoundPageBounds"
        checkpoints = [c for s in visible for c in s.rounds]
        initial = LedgerPosition(0, 0, self.deployment.initial_work_per_thousand_blocks)

        def position_after(round_id: int) -> LedgerPosition:
            earlier = [c for c in checkpoints if c.round_id <= round_id]
            if not earlier:
                return initial
            last = earlier[-1]
            return LedgerPosition(last.cumulative_work, last.cumulative_block_units, last.difficulty_after)

        wanted = [c for c in checkpoints if after_round < c.round_id <= through_round]
        page, certified = wanted[:16], through_round
        if len(wanted) > 16:
            certified = wanted[16].round_id - 1
        return certified, position_after(after_round), position_after(certified), page

    async def fetch_events(self, from_block: int, to_block: int):
        rounds, batches = [], []
        for settlement in self.settlements:
            if not from_block <= settlement.block <= to_block:
                continue
            block_hash = self._hash(settlement.block)
            for index, (checkpoint, work) in enumerate(zip(settlement.rounds, settlement.works)):
                rounds.append(
                    RoundRecorded(
                        round_id=checkpoint.round_id, work=work,
                        cumulative_work=checkpoint.cumulative_work,
                        cumulative_block_units=checkpoint.cumulative_block_units,
                        difficulty_after=checkpoint.difficulty_after, data_hash=checkpoint.data_hash,
                        position=LogPosition(settlement.block, block_hash, settlement.tx_hash, 0, index),
                    )
                )
            batches.append(
                BatchSettled(
                    settled_through_round=settlement.through,
                    previous_settled_through_round=settlement.previous,
                    active_round_count=len(settlement.rounds),
                    cumulative_work=settlement.end.cumulative_work,
                    cumulative_block_units=settlement.end.cumulative_block_units,
                    nonce=0,
                    position=LogPosition(settlement.block, block_hash, settlement.tx_hash, 0, len(settlement.rounds)),
                )
            )
        return rounds, batches


@dataclass
class ScriptedStoryteller(Storyteller):
    """Records every request and answers with the deterministic fallback text."""

    model: str = "test-model"
    requests: list[ChapterRequest] = field(default_factory=list)

    async def write(self, request: ChapterRequest) -> ChapterDraft:
        self.requests.append(request)
        self.calls += 1
        draft = fallback_chapter(request)
        cameos = tuple((companion.ref, f"did something memorable in chapter {request.number}")
                       for companion in request.featured)
        return ChapterDraft(
            title=draft.title, body=draft.body, choice_prompt=draft.choice_prompt, options=draft.options,
            summary=draft.summary, cameos=cameos, source="claude", model=self.model,
        )


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        rpc_url="http://rpc.invalid",
        confirmations=0,
        log_chunk_blocks=1000,
        log_lookback_blocks=3000,
        tick_seconds=60,
        data_dir=tmp_path,
        discord_token="",
        discord_client_id="123",
        demo_invite_url="",
        saga_model="test-model",
        anthropic_configured=False,
        sprint_work_per_hour=1400,
        max_catchup_hours=6,
        raid_hp_percent=90,
        raid_default_hp=45000,
        raid_min_hp=10000,
        site_url="https://saga.example",
        port=8000,
        genesis_meta_name="",
        genesis_meta_content="",
    )
    values.update(overrides)
    return Settings(**values)


class Clock:
    def __init__(self, start: int):
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


@pytest.fixture
def store():
    database = Store(":memory:")
    yield database
    database.close()


@pytest.fixture
def chain(deployment):
    return FakeChain(deployment)


@pytest.fixture
def world(tmp_path, deployment, store, chain):
    """An engine wired to the fake chain, a scripted storyteller and a controllable clock."""
    clock = Clock(GENESIS)
    storyteller = ScriptedStoryteller()
    engine = SagaEngine(
        settings=make_settings(tmp_path), deployment=deployment, store=store, reader=chain,
        storyteller=storyteller, clock=clock,
    )

    class World:
        pass

    w = World()
    w.engine, w.chain, w.store, w.clock, w.storyteller, w.deployment = (
        engine, chain, store, clock, storyteller, deployment
    )

    def at_round_end(round_id: int) -> None:
        """Move chain time and wall clock to just after ``round_id`` ended."""
        moment = GENESIS + round_id * 600 + 60
        clock.now = max(clock.now, moment)
        chain.time = clock.now

    w.at_round_end = at_round_end
    return w
