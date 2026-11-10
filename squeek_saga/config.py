"""Runtime settings, read once from the environment.

No private key is ever read here: the bot only reads the chain and signs nothing.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# The only sources of truth for chain identity, addresses and ABIs.
DEPLOYMENT_URL = "https://docs.squeekchain.com/assets/deployments/sepolia.json"
ABI_URL_TEMPLATE = "https://docs.squeekchain.com/assets/abi/{name}.json"
EXPECTED_CHAIN_ID = 11155111  # Ethereum Sepolia
SQUEEK_API_BASE = "https://api.squeekchain.com/v1"  # display only


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


@dataclass(frozen=True)
class Settings:
    # Chain access (read-only JSON-RPC).
    rpc_url: str
    confirmations: int
    log_chunk_blocks: int
    log_lookback_blocks: int
    tick_seconds: int

    # Storage.
    data_dir: Path

    # Discord.
    discord_token: str
    discord_client_id: str
    demo_invite_url: str

    # Story.
    saga_model: str
    anthropic_configured: bool
    sprint_work_per_hour: int
    max_catchup_hours: int

    # Raid.
    raid_hp_percent: int
    raid_default_hp: int
    raid_min_hp: int

    # Web.
    site_url: str
    port: int
    genesis_meta_name: str
    genesis_meta_content: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "saga.sqlite3"

    @property
    def invite_url(self) -> str:
        if not self.discord_client_id:
            return ""
        # View Channel + Send Messages + Embed Links.
        return (
            "https://discord.com/oauth2/authorize"
            f"?client_id={self.discord_client_id}&scope=bot+applications.commands&permissions=19456"
        )


_DISCORD_INVITE = re.compile(r"^https://(discord\.gg|discord\.com/invite)/[A-Za-z0-9-]{2,32}$")


def demo_invite(raw: str) -> str:
    """Only a plain Discord invite link is accepted; anything else is ignored."""
    return raw if _DISCORD_INVITE.match(raw) else ""


def load_settings() -> Settings:
    return Settings(
        rpc_url=_str("RPC_URL", "https://ethereum-sepolia-rpc.publicnode.com"),
        confirmations=_int("CONFIRMATIONS", 12),
        log_chunk_blocks=_int("LOG_CHUNK_BLOCKS", 1000),
        log_lookback_blocks=_int("LOG_LOOKBACK_BLOCKS", 3000),
        tick_seconds=_int("TICK_SECONDS", 60),
        data_dir=Path(_str("DATA_DIR", "./data")),
        discord_token=_str("DISCORD_TOKEN"),
        discord_client_id=_str("DISCORD_CLIENT_ID"),
        demo_invite_url=demo_invite(_str("DISCORD_DEMO_INVITE")),
        saga_model=_str("SAGA_MODEL", "claude-opus-5-5"),
        anthropic_configured=bool(_str("ANTHROPIC_API_KEY") or _str("ANTHROPIC_AUTH_TOKEN")),
        sprint_work_per_hour=_int("SPRINT_WORK_PER_HOUR", 1400),
        max_catchup_hours=_int("MAX_CATCHUP_HOURS", 6),
        raid_hp_percent=_int("RAID_HP_PERCENT", 90),
        raid_default_hp=_int("RAID_DEFAULT_HP", 45000),
        raid_min_hp=_int("RAID_MIN_HP", 10000),
        site_url=_str("SITE_URL", "http://localhost:8000").rstrip("/"),
        port=_int("PORT", 8000),
        genesis_meta_name=_str("GENESIS_META_NAME"),
        genesis_meta_content=_str("GENESIS_META_CONTENT"),
    )
