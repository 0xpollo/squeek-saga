"""The Discord layer, exercised without a network connection."""

import json
from types import SimpleNamespace

import httpx

from squeek_saga.bot import SagaBot, VoteButton, chapter_embed, chapter_view
from squeek_saga.squeek_api import SqueekApi


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


def interaction(engine, user_id=1, name="Ann", guild_id=10):
    return SimpleNamespace(
        client=SimpleNamespace(engine=engine),
        guild_id=guild_id,
        user=SimpleNamespace(id=user_id, display_name=name),
        response=FakeResponse(),
    )


async def first_chapter(world):
    world.chain.settle_hour(1)
    world.at_round_end(6)
    await world.engine.tick()
    world.chain.settle_hour(7, {7: 900, 8: 700})
    world.at_round_end(12)
    world.clock.advance(3600)
    world.chain.time = world.clock.now
    return world.store.get_chapter(await world.engine.tick())


def make_bot(world):
    api = SqueekApi(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    return SagaBot(world.engine, api)


async def test_all_slash_commands_are_registered(world):
    bot = make_bot(world)
    names = {command.name for command in bot.tree.get_commands()}
    assert names == {"join", "me", "story", "prep", "raid", "leaderboard", "wheel", "setup", "about"}
    assert bot.intents.message_content is False  # no privileged intents needed
    await bot.close()


async def test_chapter_embed_shows_the_wheel_fact_and_disclaimer(world):
    chapter = await first_chapter(world)
    embed = chapter_embed(world.engine, chapter)
    assert embed.title.startswith("Chapter 1 · ")
    text = json.dumps(embed.to_dict())
    assert "1600 work units settled on SqueekCore over rounds 7" in text
    assert "sprinted" in text
    assert "Sepolia testnet only" in embed.footer.text and "Unofficial community project" in embed.footer.text
    assert embed.url == "https://saga.example/chapter/1"


async def test_chapter_view_has_three_vote_buttons_and_the_settlement_link(world):
    chapter = await first_chapter(world)
    view = chapter_view(world.engine, chapter, votable=True)
    buttons = [item for item in view.children if isinstance(item, VoteButton)]
    links = [item for item in view.children if getattr(item, "url", None)]
    assert [button.item.custom_id for button in buttons] == [f"saga:vote:{chapter['id']}:{i}" for i in range(3)]
    assert links[0].url == f"https://explorer.example/tx/{chapter['settlement_tx']}"
    assert view.timeout is None  # persistent across restarts


async def test_vote_button_records_changes_and_closes(world):
    chapter = await first_chapter(world)
    engine, store = world.engine, world.store

    click = interaction(engine)
    await VoteButton(chapter["id"], 1, "Option B").callback(click)
    assert "Vote recorded" in click.response.sent[0][0]
    assert click.response.sent[0][1]["ephemeral"] is True
    assert store.vote_counts(chapter["id"], 3) == [0, 1, 0]
    assert store.get_player("1")["name"] == "Ann"  # voting creates the character

    again = interaction(engine)
    await VoteButton(chapter["id"], 2, "Option C").callback(again)
    assert "Vote changed" in again.response.sent[0][0]
    assert store.vote_counts(chapter["id"], 3) == [0, 0, 1]  # still one vote per player

    stale = interaction(engine, user_id=2, name="Bob")
    await VoteButton(chapter["id"] + 50, 0, "Old").callback(stale)
    assert "closed" in stale.response.sent[0][0]


async def test_vote_outside_a_server_is_refused(world):
    chapter = await first_chapter(world)
    click = interaction(world.engine, guild_id=None)
    await VoteButton(chapter["id"], 0, "A").callback(click)
    assert "from a server" in click.response.sent[0][0]
    assert world.store.vote_counts(chapter["id"], 3) == [0, 0, 0]
