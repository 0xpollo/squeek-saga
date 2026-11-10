"""End to end: settled rounds in, chapters out. Covers the 'unknown data' cases."""

import json

from squeek_saga.rounds import Branch


def hour(first_round, **works):
    """{round: work} for a saga hour; keys are offsets r0..r5."""
    return {first_round + int(key[1:]): value for key, value in works.items()}


async def start(world):
    """Certify rounds 1-6 so the first tick has a frontier, and take the starting cursor."""
    world.chain.settle_hour(1)
    world.at_round_end(6)
    assert await world.engine.tick() is None  # first run starts at the frontier: no backfill chapter
    assert world.store.get_meta_int("saga_cursor") == 6


async def write_hour(world, first_round, works=None):
    world.chain.settle_hour(first_round, works)
    world.at_round_end(first_round + 5)
    world.clock.advance(3600)  # past the one-chapter-per-hour limit
    world.chain.time = world.clock.now
    return await world.engine.tick()


async def test_sleep_trot_and_sprint_each_write_their_chapter(world):
    await start(world)
    slept = await write_hour(world, 7)
    trotted = await write_hour(world, 13, hour(13, r0=300, r3=200))
    sprinted = await write_hour(world, 19, hour(19, r0=500, r1=500, r2=500))
    branches = [world.store.get_chapter(c)["branch"] for c in (slept, trotted, sprinted)]
    assert branches == ["slept", "trotted", "sprinted"]
    assert [r.branch for r in world.storyteller.requests] == [Branch.SLEPT, Branch.TROTTED, Branch.SPRINTED]
    assert world.storyteller.calls == 3  # exactly one storyteller call per chapter


async def test_a_sleeping_hamster_still_moves_the_story(world):
    await start(world)
    first = await write_hour(world, 7)
    second = await write_hour(world, 13)
    assert first is not None and second is not None
    chapter = world.store.get_chapter(second)
    assert chapter["branch"] == "slept" and chapter["work"] == 0
    assert world.store.chapter_count() == 2


async def test_uncertified_rounds_do_not_produce_a_chapter(world):
    await start(world)
    # Only 5 of the 6 rounds of the next hour are settled. Round 12 is unknown.
    for round_id in range(7, 12):
        world.chain.settle(round_id, {round_id: 400})
    world.at_round_end(12)
    world.clock.advance(7200)
    assert await world.engine.tick() is None
    assert world.store.chapter_count() == 0
    assert world.storyteller.calls == 0
    assert world.engine.status.state == "waiting"
    assert "unknown, not zero" in world.engine.status.message

    world.chain.settle(12)  # certified with zero work
    chapter_id = await world.engine.tick()
    chapter = world.store.get_chapter(chapter_id)
    assert chapter["work"] == 2000 and chapter["branch"] == "sprinted"


async def test_rpc_outage_leaves_state_unknown_and_writes_nothing(world):
    await start(world)
    world.chain.settle_hour(7, hour(7, r0=100))
    world.at_round_end(12)
    world.clock.advance(3600)
    world.chain.fail_reads = True
    assert await world.engine.tick() is None
    assert world.engine.status.state == "waiting"
    assert "unknown" in world.engine.status.message
    assert world.store.chapter_count() == 0

    world.chain.fail_reads = False
    assert await world.engine.tick() is not None  # nothing was lost, nothing was invented


async def test_deployment_mismatch_pauses_the_saga(world):
    await start(world)
    world.chain.settle_hour(7, hour(7, r0=100))
    world.at_round_end(12)
    world.clock.advance(3600)
    world.chain.verified = False
    assert await world.engine.tick() is None
    assert world.engine.status.state == "paused"
    assert world.store.chapter_count() == 0


async def test_chapter_links_to_the_settlement_that_certified_its_last_round(world):
    await start(world)
    chapter_id = await write_hour(world, 7, hour(7, r5=250))
    chapter = world.store.get_chapter(chapter_id)
    settlement = world.store.settlement_covering(12)
    assert chapter["settlement_tx"] == settlement["tx_hash"]
    assert chapter["settlement_block"] == settlement["block_number"]
    assert (chapter["first_round"], chapter["last_round"]) == (7, 12)
    assert chapter["read_block_hash"].startswith("0x")


async def test_at_most_one_chapter_per_hour_even_with_a_backlog(world):
    await start(world)
    world.chain.settle_hour(7, hour(7, r0=100))
    world.chain.settle_hour(13)
    world.at_round_end(18)
    world.clock.advance(3600)
    first = await world.engine.tick()
    assert first is not None
    assert await world.engine.tick() is None  # second hour is certified but the limit holds
    assert world.storyteller.calls == 1
    covered = world.store.get_chapter(first)
    assert (covered["first_round"], covered["last_round"]) == (7, 18)  # told as one chapter


# -- votes --------------------------------------------------------------------------------


async def test_winning_vote_shapes_the_next_chapter_and_is_remembered(world):
    await start(world)
    engine, store = world.engine, world.store
    first = await write_hour(world, 7)
    engine.ensure_player("1", "Ann", "g1", "seedblade")
    engine.ensure_player("2", "Bob", "g1", "crumbmage")
    engine.ensure_player("3", "Cy", "g2", "wanderer")
    assert engine.vote("1", "g1", first, 2) == "recorded"
    assert engine.vote("2", "g1", first, 2) == "recorded"
    assert engine.vote("3", "g2", first, 0) == "recorded"
    assert engine.vote("3", "g2", first, 1) == "changed"  # one vote per player, changeable

    second = await write_hour(world, 13)
    result = json.loads(store.get_chapter(second)["vote_result"])
    assert result["winner"] == 2 and result["counts"] == [0, 1, 2] and result["decidedBy"] == "votes"

    request = world.storyteller.requests[-1]
    assert request.previous_choice.winner == 2
    names = {companion.name for companion in request.featured}
    assert {"Ann", "Bob", "Cy"} <= names  # players who voted are named to the storyteller

    assert engine.vote("1", "g1", first, 0) == "closed"  # the old vote is over
    deeds = [deed["text"] for deed in store.recent_deeds("1", 10)]
    assert any("counselled" in text for text in deeds)  # vote history
    assert any("memorable in chapter 2" in text for text in deeds)  # cameo memory


async def test_cameo_memories_are_recalled_in_later_chapters(world):
    await start(world)
    engine = world.engine
    first = await write_hour(world, 7)
    engine.ensure_player("1", "Ann", "g1", "seedblade")
    engine.vote("1", "g1", first, 0)
    second = await write_hour(world, 13)
    engine.vote("1", "g1", second, 0)
    await write_hour(world, 19)
    ann = next(c for c in world.storyteller.requests[-1].featured if c.name == "Ann")
    assert any("memorable in chapter 2" in memory for memory in ann.memories)


async def test_tied_vote_is_broken_by_the_wheel(world):
    await start(world)
    engine = world.engine
    first = await write_hour(world, 7)
    engine.ensure_player("1", "Ann", "g1")
    engine.ensure_player("2", "Bob", "g1")
    engine.vote("1", "g1", first, 0)
    engine.vote("2", "g1", first, 2)
    second = await write_hour(world, 13, hour(13, r0=301))  # 301 % 2 == 1 -> second tied option
    result = json.loads(world.store.get_chapter(second)["vote_result"])
    assert result["decidedBy"] == "wheel_tiebreak" and result["winner"] == 2


async def test_vote_on_an_unknown_option_is_refused(world):
    await start(world)
    first = await write_hour(world, 7)
    world.engine.ensure_player("1", "Ann", "g1")
    assert world.engine.vote("1", "g1", first, 7) == "closed"
    assert world.engine.vote("1", "g1", first + 99, 0) == "closed"


# -- raid ---------------------------------------------------------------------------------


async def test_only_settled_work_damages_the_boss(world):
    await start(world)
    engine, store = world.engine, world.store
    engine.ensure_player("1", "Ann", "g1")
    store.upsert_guild("g1", "Guild One")
    world.at_round_end(6)
    assert engine.prepare("1", "sharpen")["ok"]
    assert engine.prepare("1", "brew")["ok"] is False  # once per UTC day
    await write_hour(world, 7)  # an hour of sleep
    assert store.get_raid(0)["damage"] == 0  # preparing dealt no damage

    await write_hour(world, 13, hour(13, r0=600, r1=400))
    raid = store.get_raid(0)
    assert raid["damage"] == 1000
    assert raid["certified_through"] == 18
    view = engine.raid_view()
    assert view["roundsCertified"] == 18 and view["roundsUnknown"] == 990  # the rest is unknown
    assert store.player_glory("1") == 1000  # she was ready when the work landed
    assert store.top_guilds()[0]["name"] == "Guild One"


async def test_prep_after_the_work_earns_nothing_for_it(world):
    await start(world)
    engine, store = world.engine, world.store
    engine.ensure_player("1", "Ann", "g1")
    await write_hour(world, 7, hour(7, r0=800))
    engine.prepare("1", "scout")  # too late for rounds 7-12
    await write_hour(world, 13)
    assert store.player_glory("1") == 0


async def test_boss_defeat_is_told_and_remembered(world):
    await start(world)
    engine, store = world.engine, world.store
    engine.ensure_player("1", "Ann", "g1")
    world.at_round_end(6)
    engine.prepare("1", "mend")
    store.run("UPDATE raids SET hp = 500 WHERE week = 0")
    await write_hour(world, 7, hour(7, r1=600))
    assert store.get_raid(0)["status"] == "defeated"
    assert world.storyteller.requests[-1].raid.event == "defeated"
    assert any("worn down" in deed["text"] for deed in store.recent_deeds("1", 10))


# -- reorg --------------------------------------------------------------------------------


async def test_reorged_chapter_is_withdrawn_and_retold(world):
    await start(world)
    engine, store = world.engine, world.store
    first = await write_hour(world, 7, hour(7, r0=2000))
    assert store.get_chapter(first)["branch"] == "sprinted"

    world.chain.reorg(6)  # the six blocks that settled rounds 7-12 vanish
    world.chain.settle_hour(7)  # the canonical chain says he slept
    world.clock.advance(3600)
    world.chain.time = world.clock.now
    second = await engine.tick()

    assert store.get_chapter(first) is None  # withdrawn
    retold = store.get_chapter(second)
    assert retold["branch"] == "slept" and (retold["first_round"], retold["last_round"]) == (7, 12)
    assert store.chapter_count() == 1 and store.chapter_number(second) == 1
    assert store.get_raid(0)["damage"] == 0  # derived state replayed, not left stale
