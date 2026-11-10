"""The public chronicle: the whole saga, each chapter linked to the settlement
transaction that triggered it, the live raid, rankings, and the add-to-Discord button.

Chapter text, player names and server names are data: Jinja escapes all of it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .characters import CLASSES
from .engine import SagaEngine
from .squeek_api import SqueekApi
from .story import PREMISE

HERE = Path(__file__).parent
PAGE_SIZE = 24
BRANCH_LABEL = {
    "slept": ("Slept", "The Dreamtide was in"),
    "trotted": ("Trotted", "An ordinary waking hour"),
    "sprinted": ("Sprinted", "The Wheelwind roared"),
}


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def create_app(engine: SagaEngine, api: SqueekApi) -> FastAPI:
    app = FastAPI(title="Squeek Saga", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    store, settings, deployment = engine.store, engine.settings, engine.deployment

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        return response

    def chapter_vm(row: Any, *, latest_id: int | None) -> dict[str, Any]:
        options = json.loads(row["options"])
        counts = store.vote_counts(row["id"], len(options))
        following = store.one(
            "SELECT vote_result FROM chapters WHERE status = 'live' AND id > ? ORDER BY id LIMIT 1", (row["id"],)
        )
        outcome = json.loads(following["vote_result"]) if following and following["vote_result"] else None
        label, mood = BRANCH_LABEL.get(row["branch"], (row["branch"], ""))
        return {
            "id": row["id"],
            "number": store.chapter_number(row["id"]),
            "title": row["title"],
            "paragraphs": [p.strip() for p in row["body"].split("\n") if p.strip()],
            "branch": row["branch"],
            "branch_label": label,
            "branch_mood": mood,
            "work": row["work"],
            "active_rounds": row["active_rounds"],
            "first_round": row["first_round"],
            "last_round": row["last_round"],
            "round_count": row["last_round"] - row["first_round"] + 1,
            "hours": (row["last_round"] - row["first_round"] + 1) // 6,
            "tx": row["settlement_tx"],
            "tx_url": deployment.tx_url(row["settlement_tx"]),
            "settlement_block": row["settlement_block"],
            "read_block": row["read_block"],
            "led_here": json.loads(row["vote_result"]) if row["vote_result"] else None,
            "choice_prompt": row["choice_prompt"],
            "options": [
                {"label": option["label"], "detail": option["detail"], "votes": counts[index],
                 "won": bool(outcome and outcome["winner"] == index)}
                for index, option in enumerate(options)
            ],
            "votes_total": sum(counts),
            "open": row["id"] == latest_id,
            "outcome": outcome,
            "written": _iso(row["created_at"]),
            "source": row["source"],
        }

    def context(request: Request, **extra: Any) -> dict[str, Any]:
        base = {
            "request": request,
            "site_url": settings.site_url,
            "invite_url": settings.invite_url,
            "deployment": deployment,
            "core_url": deployment.address_url(deployment.core_address),
            "status": engine.status.as_dict(),
            "status_message": engine.status.message,
            "version": __version__,
            "genesis_meta_name": "",
            "genesis_meta_content": "",
            "home": False,
        }
        base.update(extra)
        return base

    def latest_id() -> int | None:
        latest = store.latest_chapter()
        return latest["id"] if latest else None

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        current = latest_id()
        recent = [chapter_vm(row, latest_id=current) for row in store.list_chapters(0, 4, newest_first=True)]
        return templates.TemplateResponse(
            request, "index.html",
            context(
                request,
                home=True,
                genesis_meta_name=settings.genesis_meta_name,
                genesis_meta_content=settings.genesis_meta_content,
                latest=recent[0] if recent else None,
                earlier=recent[1:],
                premise=PREMISE,
                raid=engine.raid_view(),
                chapter_count=store.chapter_count(),
                player_count=store.player_count(),
                guild_count=store.guild_count(),
                top_guilds=store.top_guilds(limit=5),
                top_players=_players(store.top_players(limit=5)),
                eta=engine.next_chapter_eta(),
            ),
        )

    def _players(rows: list[Any]) -> list[dict[str, Any]]:
        return [
            {"name": row["name"], "role": CLASSES.get(row["class"], CLASSES["wanderer"])[0], "glory": row["glory"]}
            for row in rows
        ]

    @app.get("/saga", response_class=HTMLResponse)
    async def saga(request: Request, page: int = 1):
        total = store.chapter_count()
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(1, page), pages)
        current = latest_id()
        rows = store.list_chapters((page - 1) * PAGE_SIZE, PAGE_SIZE)
        return templates.TemplateResponse(
            request, "saga.html",
            context(request, chapters=[chapter_vm(row, latest_id=current) for row in rows],
                    page=page, pages=pages, total=total, premise=PREMISE),
        )

    @app.get("/chapter/{number}", response_class=HTMLResponse)
    async def chapter(request: Request, number: int):
        row = store.chapter_by_number(number)
        if row is None:
            raise HTTPException(status_code=404, detail="No such chapter")
        return templates.TemplateResponse(
            request, "chapter.html",
            context(request, chapter=chapter_vm(row, latest_id=latest_id()), total=store.chapter_count()),
        )

    @app.get("/raid", response_class=HTMLResponse)
    async def raid(request: Request):
        current = engine.raid_view()
        history = [
            {
                "week": row["week"], "boss": row["boss_name"], "status": row["status"], "hp": row["hp"],
                "damage": row["damage"], "percent": min(100, row["damage"] * 100 // max(1, row["hp"])),
            }
            for row in store.recent_raids(12)
        ]
        week = current["week"] if current else None
        return templates.TemplateResponse(
            request, "raid.html",
            context(
                request, raid=current, history=history,
                week_guilds=store.top_guilds(week=week, limit=10) if week is not None else [],
                week_players=_players(store.top_players(week=week, limit=10)) if week is not None else [],
            ),
        )

    @app.get("/leaderboard", response_class=HTMLResponse)
    async def leaderboard(request: Request):
        return templates.TemplateResponse(
            request, "leaderboard.html",
            context(request, guilds=store.top_guilds(limit=50), players=_players(store.top_players(limit=50)),
                    guild=None),
        )

    @app.get("/leaderboard/{guild_id}", response_class=HTMLResponse)
    async def guild_leaderboard(request: Request, guild_id: str):
        guild = store.get_guild(guild_id) if guild_id.isdigit() else None
        if guild is None:
            raise HTTPException(status_code=404, detail="No such server")
        return templates.TemplateResponse(
            request, "leaderboard.html",
            context(request, guilds=[], players=_players(store.top_players(guild_id=guild_id, limit=50)),
                    guild={"name": guild["name"]}),
        )

    @app.get("/api/state")
    async def state():
        """Live state for the page's polling script. Big numbers are decimal strings."""
        latest = store.latest_chapter()
        votes = None
        if latest is not None:
            options = json.loads(latest["options"])
            votes = {"chapter": store.chapter_number(latest["id"]), "counts": store.vote_counts(latest["id"], len(options))}
        return JSONResponse(
            {
                "saga": engine.status.as_dict(),
                "chapters": store.chapter_count(),
                "votes": votes,
                "raid": engine.raid_view(),
                "nextChapterRoundEndsAt": engine.next_chapter_eta(),
                # Display only, straight from the public Squeekchain API. Never used for the story.
                "wheelLive": await api.wheel_now(),
            },
            headers={"Cache-Control": "public, max-age=5"},
        )

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz():
        return "ok"

    @app.get("/robots.txt", response_class=PlainTextResponse)
    async def robots():
        return "User-agent: *\nAllow: /\n"

    return app
