"""The published Squeekchain deployment record and ABIs.

Addresses, expected identities and ABIs come only from docs.squeekchain.com.
A copy is kept on disk so a docs outage does not stop a running saga, but if
neither the live record nor a previously fetched copy is readable we stop
instead of guessing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import ABI_URL_TEMPLATE, DEPLOYMENT_URL, EXPECTED_CHAIN_ID

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BYTES32 = re.compile(r"^0x[0-9a-fA-F]{64}$")


class DeploymentUnavailable(RuntimeError):
    """The deployment record or an ABI could not be read from the docs or the cache."""


class DeploymentInvalid(RuntimeError):
    """The record was read but does not describe the expected Sepolia deployment."""


@dataclass(frozen=True)
class Deployment:
    chain_id: int
    release: str
    explorer: str
    deployment_block: int
    core_address: str
    core_implementation: str
    core_runtime_hash: str
    core_layout_digest: str
    core_proxy_admin: str
    timelock: str
    stream_id: str
    genesis_timestamp: int
    round_duration: int
    max_work_per_round: int
    max_rounds_per_batch: int
    block_units_per_block: int
    initial_work_per_thousand_blocks: int
    from_cache: bool = False

    def tx_url(self, tx_hash: str) -> str:
        return f"{self.explorer}/tx/{tx_hash}"

    def address_url(self, address: str) -> str:
        return f"{self.explorer}/address/{address}"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DeploymentInvalid(message)


def parse_deployment(record: dict[str, Any], *, from_cache: bool = False) -> Deployment:
    """Validate the record's shape and identity, then keep the fields we rely on."""
    _require(record.get("network") == "sepolia", "record is not the sepolia deployment")
    _require(record.get("chainId") == EXPECTED_CHAIN_ID, "record chainId is not 11155111")
    try:
        core = record["contracts"]["SqueekCore"]
        timelock = record["operational"]["governanceTimelock"]["address"]
        launch = record["launchConfiguration"]["core"]
        explorer = str(record["explorer"]).rstrip("/")
        deployment = Deployment(
            chain_id=int(record["chainId"]),
            release=str(record["release"]),
            explorer=explorer,
            deployment_block=int(core["deployedAtBlock"]),
            core_address=core["address"],
            core_implementation=core["implementation"],
            core_runtime_hash=core["implementationRuntimeHash"],
            core_layout_digest=core["storageLayoutDigest"],
            core_proxy_admin=core["proxyAdmin"],
            timelock=timelock,
            stream_id=launch["streamId"],
            genesis_timestamp=int(launch["genesisTimestamp"]),
            round_duration=int(launch["roundDurationSeconds"]),
            max_work_per_round=int(launch["maxWorkPerRound"]),
            max_rounds_per_batch=int(launch["maxRoundsPerBatch"]),
            block_units_per_block=int(launch["blockUnitsPerBlock"]),
            initial_work_per_thousand_blocks=int(launch["initialWorkPerThousandBlocks"]),
            from_cache=from_cache,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DeploymentInvalid(f"record is missing a field we rely on: {exc!r}") from exc

    for label, value in (
        ("SqueekCore address", deployment.core_address),
        ("SqueekCore implementation", deployment.core_implementation),
        ("SqueekCore proxyAdmin", deployment.core_proxy_admin),
        ("governanceTimelock", deployment.timelock),
    ):
        _require(bool(_ADDRESS.match(value)), f"{label} is not an address")
    for label, value in (
        ("implementationRuntimeHash", deployment.core_runtime_hash),
        ("storageLayoutDigest", deployment.core_layout_digest),
        ("streamId", deployment.stream_id),
    ):
        _require(bool(_BYTES32.match(value)), f"{label} is not a bytes32")
    _require(explorer.startswith("https://"), "explorer is not an https URL")
    _require(deployment.round_duration > 0, "round duration must be positive")
    _require(deployment.deployment_block > 0, "deployment block must be positive")
    return deployment


async def _fetch_json(client: httpx.AsyncClient, url: str) -> Any:
    response = await client.get(url, timeout=20.0)
    response.raise_for_status()
    return response.json()


async def _load_cached(
    client: httpx.AsyncClient, url: str, cache_file: Path
) -> tuple[Any, bool]:
    """Return (document, from_cache). Live docs win; the cache is only a fallback."""
    try:
        document = await _fetch_json(client, url)
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        if cache_file.exists():
            return json.loads(cache_file.read_text()), True
        raise DeploymentUnavailable(f"cannot read {url} and no cached copy exists: {exc!r}") from exc
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(document))
    return document, False


async def load_deployment(data_dir: Path, client: httpx.AsyncClient) -> Deployment:
    record, from_cache = await _load_cached(
        client, DEPLOYMENT_URL, data_dir / "deployment" / "sepolia.json"
    )
    return parse_deployment(record, from_cache=from_cache)


async def load_abi(data_dir: Path, client: httpx.AsyncClient, name: str = "SqueekCore") -> list[dict]:
    abi, _ = await _load_cached(
        client, ABI_URL_TEMPLATE.format(name=name), data_dir / "deployment" / f"{name}.abi.json"
    )
    if not isinstance(abi, list) or not abi:
        raise DeploymentInvalid(f"{name} ABI is not a non-empty JSON array")
    return abi
