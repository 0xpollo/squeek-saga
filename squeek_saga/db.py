"""SQLite storage.

Round ids, work and block numbers are stored as INTEGER after an explicit
range check (see rounds.db_int); uint96 block-unit totals are stored as
decimal TEXT so nothing wider than 63 bits ever touches an INTEGER column.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per SqueekCore BatchSettled log (recent blocks; links rounds to their settlement tx).
CREATE TABLE IF NOT EXISTS settlements (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id               INTEGER NOT NULL,
    block_number           INTEGER NOT NULL,
    block_hash             TEXT NOT NULL,
    tx_hash                TEXT NOT NULL,
    tx_index               INTEGER NOT NULL,
    log_index              INTEGER NOT NULL,
    previous_through_round INTEGER NOT NULL,
    through_round          INTEGER NOT NULL,
    active_round_count     INTEGER NOT NULL,
    cumulative_work        INTEGER NOT NULL,
    cumulative_block_units TEXT NOT NULL,
    UNIQUE (chain_id, block_hash, tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS settlements_through ON settlements (through_round);
CREATE INDEX IF NOT EXISTS settlements_block ON settlements (block_number);

-- One row per active round, read from SqueekCore roundPage at a pinned block.
-- A certified round with no row here did zero work; a round above the frontier is unknown.
CREATE TABLE IF NOT EXISTS rounds (
    round_id               INTEGER PRIMARY KEY,
    work                   INTEGER NOT NULL,
    cumulative_work        INTEGER NOT NULL,
    cumulative_block_units TEXT NOT NULL,
    difficulty_after       INTEGER NOT NULL,
    data_hash              TEXT NOT NULL,
    chain_id               INTEGER NOT NULL,
    read_block             INTEGER NOT NULL,
    read_block_hash        TEXT NOT NULL
);

-- (block, hash, frontier) of each sync, kept to unwind a reorg to the common ancestor.
CREATE TABLE IF NOT EXISTS reads (
    block_number INTEGER PRIMARY KEY,
    block_hash   TEXT NOT NULL,
    frontier     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS guilds (
    guild_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    channel_id TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    added_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS players (
    user_id               TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    class                 TEXT NOT NULL,
    home_guild_id         TEXT NOT NULL,
    joined_at             INTEGER NOT NULL,
    prep_streak           INTEGER NOT NULL DEFAULT 0,
    last_prep_day         TEXT,
    last_featured_chapter INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deeds (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    chapter_id INTEGER,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS deeds_user ON deeds (user_id, id);

CREATE TABLE IF NOT EXISTS chapters (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    status             TEXT NOT NULL DEFAULT 'live',   -- live | orphaned
    first_round        INTEGER NOT NULL,
    last_round         INTEGER NOT NULL,
    work               INTEGER NOT NULL,
    active_rounds      INTEGER NOT NULL,
    branch             TEXT NOT NULL,
    vote_result        TEXT,                           -- JSON, the choice that led here
    title              TEXT NOT NULL,
    body               TEXT NOT NULL,
    choice_prompt      TEXT NOT NULL,
    options            TEXT NOT NULL,                  -- JSON list
    summary            TEXT NOT NULL,                  -- rolling summary after this chapter
    settlement_tx      TEXT NOT NULL,
    settlement_block   INTEGER NOT NULL,
    settlement_hash    TEXT NOT NULL,
    read_block         INTEGER NOT NULL,
    read_block_hash    TEXT NOT NULL,
    source             TEXT NOT NULL,                  -- claude | fallback
    model              TEXT,
    raid_week          INTEGER NOT NULL,
    created_at         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS votes (
    chapter_id INTEGER NOT NULL,
    user_id    TEXT NOT NULL,
    guild_id   TEXT NOT NULL,
    option_idx INTEGER NOT NULL,
    voted_at   INTEGER NOT NULL,
    PRIMARY KEY (chapter_id, user_id)
);

CREATE TABLE IF NOT EXISTS raids (
    week              INTEGER PRIMARY KEY,
    boss_name         TEXT NOT NULL,
    boss_blurb        TEXT NOT NULL,
    hp                INTEGER NOT NULL,
    hp_basis          TEXT NOT NULL,                   -- how hp was derived
    first_round       INTEGER NOT NULL,
    last_round        INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'active',  -- active | defeated | retreated
    damage            INTEGER NOT NULL DEFAULT 0,
    certified_through INTEGER NOT NULL,
    created_at        INTEGER NOT NULL,
    resolved_at       INTEGER
);

CREATE TABLE IF NOT EXISTS preps (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            TEXT NOT NULL,
    day                TEXT NOT NULL,                  -- UTC YYYY-MM-DD
    action             TEXT NOT NULL,
    prepared_at        INTEGER NOT NULL,
    multiplier_percent INTEGER NOT NULL,
    UNIQUE (user_id, day)
);
CREATE INDEX IF NOT EXISTS preps_time ON preps (prepared_at);

-- Derived: fully recomputed from rounds x preps, so a reorg can never leave it wrong.
CREATE TABLE IF NOT EXISTS raid_glory (
    week    INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    glory   INTEGER NOT NULL,
    PRIMARY KEY (week, user_id)
);

CREATE TABLE IF NOT EXISTS posts (
    chapter_id INTEGER NOT NULL,
    guild_id   TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    PRIMARY KEY (chapter_id, guild_id)
);
"""


def now() -> int:
    return int(time.time())


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One connection, used from the single asyncio event-loop thread in production.
        # check_same_thread is relaxed only because test clients drive the app from a worker thread.
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- low level ---------------------------------------------------------
    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def run(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self.conn:
            return self.conn.execute(sql, params)

    # -- meta ----------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def get_meta_int(self, key: str, default: int = 0) -> int:
        value = self.get_meta(key)
        return int(value) if value is not None else default

    def set_meta(self, key: str, value: Any, *, commit: bool = True) -> None:
        sql = "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value"
        if commit:
            self.run(sql, (key, str(value)))
        else:
            self.conn.execute(sql, (key, str(value)))

    # -- ledger reads ----------------------------------------------------------
    def frontier(self) -> int:
        """Last certified round we hold. 0 before the first settlement."""
        return self.get_meta_int("frontier", 0)

    def active_work(self, first_round: int, last_round: int) -> dict[int, int]:
        rows = self.all(
            "SELECT round_id, work FROM rounds WHERE round_id BETWEEN ? AND ?",
            (first_round, last_round),
        )
        return {row["round_id"]: row["work"] for row in rows}

    def settlement_covering(self, round_id: int) -> sqlite3.Row | None:
        """The BatchSettled that certified ``round_id``."""
        return self.one(
            "SELECT * FROM settlements WHERE previous_through_round < ? AND through_round >= ? "
            "ORDER BY through_round LIMIT 1",
            (round_id, round_id),
        )

    def last_round_row(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM rounds ORDER BY round_id DESC LIMIT 1")

    def latest_settlement(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM settlements ORDER BY through_round DESC LIMIT 1")

    # -- guilds ------------------------------------------------------------------
    def upsert_guild(self, guild_id: str, name: str) -> None:
        self.run(
            "INSERT INTO guilds (guild_id, name, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id) DO UPDATE SET name = excluded.name, active = 1",
            (guild_id, name, now()),
        )

    def set_guild_channel(self, guild_id: str, channel_id: str | None) -> None:
        self.run("UPDATE guilds SET channel_id = ? WHERE guild_id = ?", (channel_id, guild_id))

    def deactivate_guild(self, guild_id: str) -> None:
        self.run("UPDATE guilds SET active = 0 WHERE guild_id = ?", (guild_id,))

    def get_guild(self, guild_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM guilds WHERE guild_id = ?", (guild_id,))

    def guild_channels(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM guilds WHERE active = 1 AND channel_id IS NOT NULL")

    def guild_count(self) -> int:
        return self.one("SELECT COUNT(*) AS n FROM guilds WHERE active = 1")["n"]

    # -- players -------------------------------------------------------------------
    def get_player(self, user_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM players WHERE user_id = ?", (user_id,))

    def create_player(self, user_id: str, name: str, klass: str, guild_id: str) -> None:
        self.run(
            "INSERT INTO players (user_id, name, class, home_guild_id, joined_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, name, klass, guild_id, now()),
        )

    def update_player(self, user_id: str, **fields: Any) -> None:
        allowed = {"name", "class", "home_guild_id", "prep_streak", "last_prep_day", "last_featured_chapter"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown player fields: {unknown}")
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        self.run(f"UPDATE players SET {assignments} WHERE user_id = ?", (*fields.values(), user_id))

    def player_count(self) -> int:
        return self.one("SELECT COUNT(*) AS n FROM players")["n"]

    def add_deed(self, user_id: str, kind: str, text: str, chapter_id: int | None = None) -> None:
        self.run(
            "INSERT INTO deeds (user_id, chapter_id, kind, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, chapter_id, kind, text, now()),
        )

    def recent_deeds(self, user_id: str, limit: int = 5, kinds: Iterable[str] | None = None) -> list[sqlite3.Row]:
        if kinds:
            kinds = list(kinds)
            marks = ", ".join("?" for _ in kinds)
            return self.all(
                f"SELECT * FROM deeds WHERE user_id = ? AND kind IN ({marks}) ORDER BY id DESC LIMIT ?",
                (user_id, *kinds, limit),
            )
        return self.all("SELECT * FROM deeds WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))

    def player_glory(self, user_id: str) -> int:
        row = self.one("SELECT COALESCE(SUM(glory), 0) AS g FROM raid_glory WHERE user_id = ?", (user_id,))
        return row["g"]

    # -- chapters --------------------------------------------------------------------
    def insert_chapter(self, **fields: Any) -> int:
        fields.setdefault("created_at", now())
        for key in ("options", "vote_result"):
            if key in fields and fields[key] is not None and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key])
        columns = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cursor = self.run(f"INSERT INTO chapters ({columns}) VALUES ({marks})", tuple(fields.values()))
        return int(cursor.lastrowid)

    def latest_chapter(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM chapters WHERE status = 'live' ORDER BY id DESC LIMIT 1")

    def get_chapter(self, chapter_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM chapters WHERE id = ? AND status = 'live'", (chapter_id,))

    def chapter_number(self, chapter_id: int) -> int:
        """Public chapter number: position among live chapters (orphaned ones are not counted)."""
        return self.one(
            "SELECT COUNT(*) AS n FROM chapters WHERE status = 'live' AND id <= ?", (chapter_id,)
        )["n"]

    def chapter_by_number(self, number: int) -> sqlite3.Row | None:
        if number < 1:
            return None
        return self.one(
            "SELECT * FROM chapters WHERE status = 'live' ORDER BY id LIMIT 1 OFFSET ?", (number - 1,)
        )

    def chapter_count(self) -> int:
        return self.one("SELECT COUNT(*) AS n FROM chapters WHERE status = 'live'")["n"]

    def list_chapters(self, offset: int = 0, limit: int = 50, newest_first: bool = False) -> list[sqlite3.Row]:
        order = "DESC" if newest_first else "ASC"
        return self.all(
            f"SELECT * FROM chapters WHERE status = 'live' ORDER BY id {order} LIMIT ? OFFSET ?",
            (limit, offset),
        )

    # -- votes -------------------------------------------------------------------------
    def cast_vote(self, chapter_id: int, user_id: str, guild_id: str, option_idx: int) -> bool:
        """Record or change a vote. Returns True when this is the user's first vote on the chapter."""
        existing = self.one(
            "SELECT option_idx FROM votes WHERE chapter_id = ? AND user_id = ?", (chapter_id, user_id)
        )
        self.run(
            "INSERT INTO votes (chapter_id, user_id, guild_id, option_idx, voted_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (chapter_id, user_id) DO UPDATE SET option_idx = excluded.option_idx, "
            "guild_id = excluded.guild_id, voted_at = excluded.voted_at",
            (chapter_id, user_id, guild_id, option_idx, now()),
        )
        return existing is None

    def vote_counts(self, chapter_id: int, option_count: int) -> list[int]:
        counts = [0] * option_count
        for row in self.all(
            "SELECT option_idx, COUNT(*) AS n FROM votes WHERE chapter_id = ? GROUP BY option_idx",
            (chapter_id,),
        ):
            if 0 <= row["option_idx"] < option_count:
                counts[row["option_idx"]] = row["n"]
        return counts

    def voters(self, chapter_id: int) -> list[sqlite3.Row]:
        return self.all(
            "SELECT v.user_id, v.option_idx, p.name, p.class, p.last_featured_chapter "
            "FROM votes v JOIN players p ON p.user_id = v.user_id WHERE v.chapter_id = ?",
            (chapter_id,),
        )

    def player_votes(self, user_id: str, limit: int = 5) -> list[sqlite3.Row]:
        return self.all(
            "SELECT v.chapter_id, v.option_idx, c.title, c.options FROM votes v "
            "JOIN chapters c ON c.id = v.chapter_id WHERE v.user_id = ? AND c.status = 'live' "
            "ORDER BY v.chapter_id DESC LIMIT ?",
            (user_id, limit),
        )

    def vote_total(self, user_id: str) -> int:
        return self.one("SELECT COUNT(*) AS n FROM votes WHERE user_id = ?", (user_id,))["n"]

    # -- raids -------------------------------------------------------------------------
    def get_raid(self, week: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM raids WHERE week = ?", (week,))

    def create_raid(self, week: int, boss_name: str, boss_blurb: str, hp: int, hp_basis: str,
                    first_round: int, last_round: int) -> None:
        self.run(
            "INSERT OR IGNORE INTO raids (week, boss_name, boss_blurb, hp, hp_basis, first_round, last_round, "
            "certified_through, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (week, boss_name, boss_blurb, hp, hp_basis, first_round, last_round, first_round - 1, now()),
        )

    def update_raid(self, week: int, damage: int, certified_through: int, status: str) -> None:
        resolved = now() if status != "active" else None
        self.run(
            "UPDATE raids SET damage = ?, certified_through = ?, status = ?, "
            "resolved_at = CASE WHEN ? = 'active' THEN NULL ELSE COALESCE(resolved_at, ?) END WHERE week = ?",
            (damage, certified_through, status, status, resolved, week),
        )

    def recent_raids(self, limit: int = 6) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM raids ORDER BY week DESC LIMIT ?", (limit,))

    # -- preps & glory -----------------------------------------------------------------
    def get_prep(self, user_id: str, day: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM preps WHERE user_id = ? AND day = ?", (user_id, day))

    def add_prep(self, user_id: str, day: str, action: str, prepared_at: int, multiplier_percent: int) -> None:
        self.run(
            "INSERT INTO preps (user_id, day, action, prepared_at, multiplier_percent) VALUES (?, ?, ?, ?, ?)",
            (user_id, day, action, prepared_at, multiplier_percent),
        )

    def preps_between(self, start_ts: int, end_ts: int) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM preps WHERE prepared_at >= ? AND prepared_at <= ?", (start_ts, end_ts)
        )

    def active_prep_count(self, at_ts: int, cover_seconds: int) -> int:
        return self.one(
            "SELECT COUNT(DISTINCT user_id) AS n FROM preps WHERE prepared_at <= ? AND prepared_at > ?",
            (at_ts, at_ts - cover_seconds),
        )["n"]

    def replace_week_glory(self, week: int, glory: dict[str, int]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM raid_glory WHERE week = ?", (week,))
            self.conn.executemany(
                "INSERT INTO raid_glory (week, user_id, glory) VALUES (?, ?, ?)",
                [(week, user_id, amount) for user_id, amount in glory.items() if amount > 0],
            )

    def top_players(self, guild_id: str | None = None, week: int | None = None, limit: int = 10) -> list[sqlite3.Row]:
        where, params = [], []
        if guild_id is not None:
            where.append("p.home_guild_id = ?")
            params.append(guild_id)
        if week is not None:
            where.append("g.week = ?")
            params.append(week)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self.all(
            "SELECT p.user_id, p.name, p.class, p.home_guild_id, SUM(g.glory) AS glory "
            f"FROM raid_glory g JOIN players p ON p.user_id = g.user_id {clause} "
            "GROUP BY p.user_id ORDER BY glory DESC, p.joined_at ASC LIMIT ?",
            (*params, limit),
        )

    def top_guilds(self, week: int | None = None, limit: int = 10) -> list[sqlite3.Row]:
        clause, params = ("WHERE g.week = ?", [week]) if week is not None else ("", [])
        return self.all(
            "SELECT s.guild_id, s.name, SUM(g.glory) AS glory, COUNT(DISTINCT p.user_id) AS heroes "
            "FROM raid_glory g JOIN players p ON p.user_id = g.user_id "
            f"JOIN guilds s ON s.guild_id = p.home_guild_id {clause} "
            "GROUP BY s.guild_id ORDER BY glory DESC, s.added_at ASC LIMIT ?",
            (*params, limit),
        )

    # -- posts -------------------------------------------------------------------------
    def record_post(self, chapter_id: int, guild_id: str, channel_id: str, message_id: str) -> None:
        self.run(
            "INSERT OR REPLACE INTO posts (chapter_id, guild_id, channel_id, message_id) VALUES (?, ?, ?, ?)",
            (chapter_id, guild_id, channel_id, message_id),
        )

    def posted_guilds(self, chapter_id: int) -> set[str]:
        return {row["guild_id"] for row in self.all("SELECT guild_id FROM posts WHERE chapter_id = ?", (chapter_id,))}

    def posts_for(self, chapter_id: int) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM posts WHERE chapter_id = ?", (chapter_id,))
