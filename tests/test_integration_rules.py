"""Squeekchain integration rules: deployment record, API 'unavailable', and the website."""

import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from squeek_saga.deployment import (
    DeploymentInvalid,
    DeploymentUnavailable,
    load_deployment,
    parse_deployment,
)
from squeek_saga.squeek_api import SqueekApi
from squeek_saga.web import create_app

from conftest import RECORD, make_settings


# -- deployment record ----------------------------------------------------------------------


def test_record_is_parsed(deployment):
    assert deployment.chain_id == 11155111
    assert deployment.round_duration == 600
    assert deployment.tx_url("0xabc") == "https://explorer.example/tx/0xabc"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(chainId=1),  # mainnet is never accepted
        lambda r: r.update(network="mainnet"),
        lambda r: r["contracts"]["SqueekCore"].update(address="0x1234"),
        lambda r: r["contracts"].pop("SqueekCore"),
        lambda r: r["launchConfiguration"]["core"].pop("genesisTimestamp"),
        lambda r: r["contracts"]["SqueekCore"].update(implementationRuntimeHash="0xzz"),
    ],
)
def test_wrong_or_incomplete_records_are_rejected(mutate):
    record = copy.deepcopy(RECORD)
    mutate(record)
    with pytest.raises(DeploymentInvalid):
        parse_deployment(record)


async def test_docs_outage_without_cache_stops_instead_of_guessing(tmp_path):
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(DeploymentUnavailable):
            await load_deployment(tmp_path, client)


async def test_docs_outage_uses_the_last_fetched_record(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=RECORD))) as client:
        live = await load_deployment(tmp_path, client)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
        cached = await load_deployment(tmp_path, client)
    assert not live.from_cache and cached.from_cache
    assert cached.core_address == live.core_address


# -- public API: display only, 'unavailable' is unknown ---------------------------------------------


def api_with(handler):
    return SqueekApi(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_unavailable_status_is_unknown_not_empty():
    api = api_with(lambda r: httpx.Response(200, json={"schemaVersion": 1, "status": "unavailable", "reason": "indexer_unavailable"}))
    reading = await api.get("/core")
    assert reading.known is False and reading.data is None
    assert reading.reason == "indexer_unavailable"


async def test_wheel_summary_keeps_unknown_fields_as_none():
    api = api_with(lambda r: httpx.Response(200, json={"schemaVersion": 1, "measurement": None, "builder": {"status": "unavailable", "reason": "no_measurement"}}))
    live = await api.wheel_now()
    assert live["known"] is False
    assert live["rpmMilli"] is None and live["openRoundWorkUnits"] is None  # None, never 0


async def test_wheel_summary_reads_live_fields_and_big_numbers_as_strings():
    body = {
        "measurement": {"freshness": "online", "coreStatus": "ACTIVE", "rpmMilli": 77005, "speedMmPerSecond": 1127},
        "builder": {"status": "available", "queued": {"openRoundWorkUnits": "308"}},
    }
    live = await api_with(lambda r: httpx.Response(200, json=body)).wheel_now()
    assert live["known"] and live["coreStatus"] == "ACTIVE" and live["rpmMilli"] == 77005
    assert live["openRoundWorkUnits"] == "308"


async def test_builder_unavailable_leaves_pending_work_unknown():
    body = {"measurement": {"freshness": "stale", "coreStatus": "IDLE", "rpmMilli": 0},
            "builder": {"status": "unavailable", "reason": "counter_fault"}}
    live = await api_with(lambda r: httpx.Response(200, json=body)).wheel_now()
    assert live["known"] and live["openRoundWorkUnits"] is None


async def test_etag_is_sent_back_and_304_keeps_the_last_reading():
    seen = []

    def handler(request):
        seen.append(request.headers.get("If-None-Match"))
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json={"status": "available", "core": {"settledThroughRound": "954"}}, headers={"ETag": '"v1"'})

    api = api_with(handler)
    first = await api.get("/core", min_interval=0)
    second = await api.get("/core", min_interval=0)
    assert seen == [None, '"v1"']
    assert second.known and second.data == first.data


async def test_rate_limit_is_respected_and_reported_as_unknown():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "30"}, json={"error": "rate_limited"})

    api = api_with(handler)
    assert (await api.get("/status", min_interval=0)).known is False
    await api.get("/status", min_interval=0)  # within Retry-After: served from memory
    assert len(calls) == 1


async def test_min_interval_prevents_hammering():
    calls = []
    api = api_with(lambda r: (calls.append(1), httpx.Response(200, json={"status": "available"}))[1])
    for _ in range(5):
        await api.get("/status", min_interval=60)
    assert len(calls) == 1


async def test_unparseable_response_is_an_error_not_empty_data():
    reading = await api_with(lambda r: httpx.Response(200, text="<html>oops</html>")).get("/core")
    assert reading.known is False and "unparseable" in reading.reason


# -- website -----------------------------------------------------------------------------------------


@pytest.fixture
def site(world, tmp_path):
    def build(**settings):
        if settings:
            world.engine.settings = make_settings(tmp_path, **settings)
        api = api_with(lambda r: httpx.Response(200, json={"status": "unavailable", "reason": "indexer_unavailable"}))
        return TestClient(create_app(world.engine, api))

    return build


async def write_chapter(world):
    world.chain.settle_hour(1)
    world.at_round_end(6)
    await world.engine.tick()
    world.chain.settle_hour(7, {7: 500, 8: 300})
    world.at_round_end(12)
    world.clock.advance(3600)
    world.chain.time = world.clock.now
    return await world.engine.tick()


def test_home_renders_before_any_chapter(site):
    page = site().get("/")
    assert page.status_code == 200
    assert "Testnet only" in page.text and "Unofficial community project" in page.text
    assert "Before the first chapter" in page.text
    assert "discord.com/oauth2/authorize?client_id=123" in page.text


def test_verification_tag_only_on_the_home_page(site):
    client = site(genesis_meta_name="squeekchain-genesis", genesis_meta_content="abc123")
    tag = '<meta name="squeekchain-genesis" content="abc123">'
    assert tag in client.get("/").text
    assert tag not in client.get("/saga").text
    assert "genesis-verification: not set" in site(genesis_meta_name="", genesis_meta_content="").get("/").text


async def test_chapter_pages_link_to_the_settlement_transaction(world, site):
    chapter_id = await write_chapter(world)
    chapter = world.store.get_chapter(chapter_id)
    client = site()
    for path in ("/", "/saga", "/chapter/1"):
        page = client.get(path)
        assert page.status_code == 200
        assert f"https://explorer.example/tx/{chapter['settlement_tx']}" in page.text
        assert "800 work units settled over rounds 7–12" in page.text
    assert client.get("/chapter/2").status_code == 404
    assert client.get("/chapter/0").status_code == 404


async def test_live_state_reports_unknown_as_unknown(world, site):
    await write_chapter(world)
    state = site().get("/api/state").json()
    assert state["wheelLive"]["known"] is False  # API said unavailable
    assert state["saga"]["settledThroughRound"] == "12"  # decimal string, from the chain
    assert state["raid"]["damage"] == "800" and state["raid"]["roundsUnknown"] == 996
    assert state["votes"] == {"chapter": 1, "counts": [0, 0, 0]}


async def test_player_text_is_escaped(world, site):
    await write_chapter(world)
    world.store.upsert_guild("42", "<script>alert(1)</script>")
    world.engine.ensure_player("1", "Ann", "42")
    world.store.replace_week_glory(0, {"1": 10})
    page = site().get("/leaderboard").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_other_pages_render(site):
    client = site()
    for path in ("/saga", "/raid", "/leaderboard", "/healthz", "/static/style.css", "/static/app.js"):
        assert client.get(path).status_code == 200
    assert client.get("/leaderboard/999").status_code == 404
    assert "default-src 'self'" in client.get("/").headers["content-security-policy"]


def test_demo_server_button_appears_only_when_configured(site):
    assert "Play now in the demo server" not in site().get("/").text
    page = site(demo_invite_url="https://discord.gg/abc123").get("/").text
    assert 'href="https://discord.gg/abc123"' in page and "Play now in the demo server" in page
    assert "Add Squeek Saga to your server" in page  # the bot invite stays available


def test_missing_bot_link_reads_as_coming_soon_not_broken(site):
    page = site(discord_client_id="").get("/").text
    assert "coming soon" in page and "not configured)" not in page


def test_only_real_discord_invites_are_accepted():
    from squeek_saga.config import demo_invite

    assert demo_invite("https://discord.gg/abc123") == "https://discord.gg/abc123"
    assert demo_invite("https://discord.com/invite/Squeek-Saga") == "https://discord.com/invite/Squeek-Saga"
    for bad in ("", "http://discord.gg/abc123", "https://evil.example/discord.gg/abc", "javascript:alert(1)",
                "https://discord.gg/abc123?x=<script>", "https://discord.gg.evil.example/abc"):
        assert demo_invite(bad) == ""


def test_home_explains_how_to_play_without_joining_discord(site):
    page = site().get("/").text
    for command in ("/join", "/prep", "/raid", "/me", "/leaderboard", "/wheel", "/setup"):
        assert f"<code>{command}</code>" in page
    assert "No wallet, no sign-up, nothing to pay" in page
