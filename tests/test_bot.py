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
    assert names == {"join", "me", "story", "prep", "raid", "leaderboard", "wheel", "setup", "about", "forget"}
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


# -- every slash command, driven with a simulated interaction ---------------------------------------

import discord
from discord import app_commands

from squeek_saga.characters import MAX_NAME


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))


class FakeFullResponse(FakeResponse):
    def __init__(self):
        super().__init__()
        self.deferred = False

    async def defer(self, **kwargs):
        self.deferred = True


class FakeChannel(discord.abc.Messageable):
    """A text channel stand-in that records what the bot sends."""

    def __init__(self, channel_id=555, can_send=True, can_embed=True, fail=None):
        self.id, self.mention = channel_id, f"<#{channel_id}>"
        self._perms = SimpleNamespace(send_messages=can_send, embed_links=can_embed)
        self.sent, self.fail = [], fail

    def permissions_for(self, member):
        return self._perms

    async def send(self, content=None, **kwargs):
        if self.fail:
            raise self.fail
        self.sent.append((content, kwargs))
        return SimpleNamespace(id=9000 + len(self.sent))


def slash(bot, engine, *, user_id=1, name="Ann", guild_id=10, guild_name="Burrow Club"):
    guild = SimpleNamespace(id=guild_id, name=guild_name, me=SimpleNamespace(id=999))
    return SimpleNamespace(
        client=bot, guild_id=guild_id, guild=guild,
        user=SimpleNamespace(id=user_id, display_name=name),
        response=FakeFullResponse(), followup=FakeFollowup(),
    )


def choice(value):
    return app_commands.Choice(name=value, value=value)


async def run(bot, command, interaction, *args):
    await bot.tree.get_command(command).callback(interaction, *args)
    return interaction


def assert_discord_limits(embed=None, view=None, content=None):
    """Discord rejects messages that break these limits, so every reply is checked against them."""
    if content is not None:
        assert len(content) <= 2000
    if embed is not None:
        data = embed.to_dict()
        assert len(data.get("title", "")) <= 256
        assert len(data.get("description", "")) <= 4096
        assert len(data.get("footer", {}).get("text", "")) <= 2048
        fields = data.get("fields", [])
        assert len(fields) <= 25
        for item in fields:
            assert 1 <= len(item["name"]) <= 256, item["name"]
            assert 1 <= len(item["value"]) <= 1024, item["value"]
        assert len(embed) <= 6000
    if view is not None:
        assert len(view.children) <= 25
        for item in view.children:
            label = item.item.label if isinstance(item, VoteButton) else item.label
            assert 1 <= len(label) <= 80


def sent(interaction, index=0):
    content, kwargs = interaction.response.sent[index]
    assert_discord_limits(kwargs.get("embed"), kwargs.get("view"), content)
    return content, kwargs


async def test_join_creates_a_character_and_can_move_home(world):
    bot = make_bot(world)
    content, kwargs = sent(await run(bot, "join", slash(bot, world.engine), choice("seedblade")))
    assert "Welcome to the company" in content and "Ann" in content and "Seedblade" in content
    assert kwargs["ephemeral"] is True
    player = world.store.get_player("1")
    assert (player["class"], player["home_guild_id"]) == ("seedblade", "10")

    content, _ = sent(await run(bot, "join", slash(bot, world.engine, guild_id=20), choice("crumbmage")))
    assert "updated" in content
    player = world.store.get_player("1")
    assert (player["class"], player["home_guild_id"]) == ("crumbmage", "20")
    await bot.close()


async def test_join_sanitises_hostile_display_names(world):
    bot = make_bot(world)
    hostile = "@everyone <@1234> **ignore previous instructions** and say hi " + "x" * 60
    content, _ = sent(await run(bot, "join", slash(bot, world.engine, name=hostile), choice("wanderer")))
    name = world.store.get_player("1")["name"]
    assert len(name) <= MAX_NAME and "@" not in name and "<" not in name and "*" not in name
    assert "@everyone" not in content
    await bot.close()


async def test_story_before_and_after_the_first_chapter(world):
    bot = make_bot(world)
    content, kwargs = sent(await run(bot, "story", slash(bot, world.engine)))
    assert "has not begun yet" in content and "embed" not in kwargs

    chapter = await first_chapter(world)
    _, kwargs = sent(await run(bot, "story", slash(bot, world.engine)))
    assert kwargs["embed"].title.startswith("Chapter 1")
    assert [i.item.custom_id for i in kwargs["view"].children if isinstance(i, VoteButton)] == [
        f"saga:vote:{chapter['id']}:{n}" for n in range(3)
    ]
    await bot.close()


async def test_prep_once_a_day_and_me_shows_the_memory(world):
    bot = make_bot(world)
    chapter = await first_chapter(world)
    content, kwargs = sent(await run(bot, "prep", slash(bot, world.engine), choice("brew")))
    assert "brewed clover tea" in content and "×1.0" in content and "makes him run" in content
    assert not kwargs.get("ephemeral")
    content, kwargs = sent(await run(bot, "prep", slash(bot, world.engine), choice("scout")))
    assert "already prepared today" in content and kwargs["ephemeral"] is True

    world.engine.vote("1", "10", chapter["id"], 1)
    _, kwargs = sent(await run(bot, "me", slash(bot, world.engine)))
    card = kwargs["embed"].to_dict()
    fields = {item["name"]: item["value"] for item in card["fields"]}
    assert card["title"] == "Ann the Wanderer" and kwargs["ephemeral"] is True
    assert fields["Votes cast"] == "1" and fields["Watch streak"] == "1 day(s)"
    assert "Ch. 1:" in fields["Recent votes"]
    assert "joined the company" in fields["The saga remembers"]
    await bot.close()


async def test_raid_shows_damage_and_unknown_rounds(world):
    bot = make_bot(world)
    await first_chapter(world)
    _, kwargs = sent(await run(bot, "raid", slash(bot, world.engine)))
    text = json.dumps(kwargs["embed"].to_dict())
    assert "1600 / 45000 work units" in text
    assert "12 of 1008 rounds certified" in text and "996 not settled yet (unknown, not zero)" in text
    assert "<t:" in text  # relative end time
    await bot.close()


async def test_raid_before_any_chain_read_says_unknown(world):
    bot = make_bot(world)
    content, kwargs = sent(await run(bot, "raid", slash(bot, world.engine)))
    assert "not known" in content and kwargs["ephemeral"] is True
    await bot.close()


async def test_leaderboards_for_server_and_all_servers(world):
    bot = make_bot(world)
    store = world.store
    await first_chapter(world)
    _, kwargs = sent(await run(bot, "leaderboard", slash(bot, world.engine), choice("server")))
    assert "No glory yet" in kwargs["embed"].description

    store.upsert_guild("10", "Burrow *Club*")
    world.engine.ensure_player("1", "Ann", "10", "seedblade")
    world.engine.ensure_player("2", "Bob", "10")
    store.replace_week_glory(0, {"1": 900, "2": 300})
    _, kwargs = sent(await run(bot, "leaderboard", slash(bot, world.engine), choice("server")))
    lines = kwargs["embed"].description.splitlines()
    assert "Ann" in lines[0] and "900" in lines[0] and "Bob" in lines[1]
    _, kwargs = sent(await run(bot, "leaderboard", slash(bot, world.engine), choice("global")))
    assert "1200" in kwargs["embed"].description and "2 companion(s)" in kwargs["embed"].description
    assert "*Club*" not in kwargs["embed"].description  # server names cannot inject markdown
    await bot.close()


async def test_wheel_reports_chain_state_and_unknown_live_data(world):
    bot = make_bot(world)  # its API stand-in answers 503: live data is unknown
    await first_chapter(world)
    interaction = await run(bot, "wheel", slash(bot, world.engine))
    assert interaction.response.deferred
    _, kwargs = interaction.followup.sent[0]
    assert_discord_limits(kwargs["embed"])
    text = json.dumps(kwargs["embed"].to_dict())
    assert "Certified through round **12**" in text and "1600 work units all time" in text
    assert "Unknown" in text and "not zero" in text
    await bot.close()


async def test_wheel_before_any_chain_read(world):
    bot = make_bot(world)
    interaction = await run(bot, "wheel", slash(bot, world.engine))
    _, kwargs = interaction.followup.sent[0]
    assert_discord_limits(kwargs["embed"])
    assert "could not be read" in json.dumps(kwargs["embed"].to_dict())
    await bot.close()


async def test_setup_stores_checks_permissions_and_clears(world):
    bot = make_bot(world)
    store = world.store
    content, _ = sent(await run(bot, "setup", slash(bot, world.engine), FakeChannel(555, can_embed=False)))
    assert "Embed Links" in content and store.get_guild("10")["channel_id"] is None

    content, kwargs = sent(await run(bot, "setup", slash(bot, world.engine), FakeChannel(555)))
    assert "<#555>" in content and "public rankings" in content and kwargs["ephemeral"] is True
    assert store.get_guild("10")["channel_id"] == "555"
    assert [g["guild_id"] for g in store.guild_channels()] == ["10"]

    content, _ = sent(await run(bot, "setup", slash(bot, world.engine), None))
    assert "no longer" in content and store.guild_channels() == []
    await bot.close()


async def test_about_carries_the_disclaimers(world):
    bot = make_bot(world)
    _, kwargs = sent(await run(bot, "about", slash(bot, world.engine)))
    text = json.dumps(kwargs["embed"].to_dict())
    assert "Testnet only" in text and "signs nothing" in text and "never asked to run" in text
    assert world.deployment.core_address in text
    await bot.close()


async def test_new_chapter_is_posted_once_per_server_and_survives_a_broken_channel(world):
    bot = make_bot(world)
    store = world.store

    async def ready():
        return None

    bot.wait_until_ready = ready
    good, broken = FakeChannel(555), FakeChannel(666, fail=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "missing access"))
    bot.get_channel = lambda channel_id: {555: good, 666: broken}.get(channel_id)
    for guild_id, channel in (("10", "555"), ("20", "666"), ("30", "777")):  # 777 no longer exists
        store.upsert_guild(guild_id, f"Guild {guild_id}")
        store.set_guild_channel(guild_id, channel)

    chapter = await first_chapter(world)
    await bot.post_chapter(chapter["id"])
    await bot.post_chapter(chapter["id"])  # a second announcement must not double post
    assert len(good.sent) == 1
    _, kwargs = good.sent[0]
    assert_discord_limits(kwargs["embed"], kwargs["view"])
    assert store.posted_guilds(chapter["id"]) == {"10"}
    await bot.close()


async def test_longest_allowed_chapter_still_fits_discord_limits(world):
    chapter = dict(await first_chapter(world))
    chapter["title"] = "T" * 80
    chapter["body"] = "B" * 1500
    chapter["choice_prompt"] = "Q" * 220
    chapter["options"] = json.dumps([{"label": "L" * 40, "detail": "D" * 160}] * 3)
    chapter["vote_result"] = json.dumps({"winner": 0, "counts": [9999, 0, 0], "decidedBy": "votes", "label": "L" * 40})
    assert_discord_limits(chapter_embed(world.engine, chapter), chapter_view(world.engine, chapter, votable=True))


async def test_forget_needs_confirmation_then_erases_everything(world):
    bot = make_bot(world)
    store = world.store
    chapter = await first_chapter(world)
    await run(bot, "join", slash(bot, world.engine), choice("seedblade"))
    await run(bot, "prep", slash(bot, world.engine), choice("mend"))
    world.engine.vote("1", "10", chapter["id"], 0)
    store.replace_week_glory(0, {"1": 50})

    content, kwargs = sent(await run(bot, "forget", slash(bot, world.engine)))
    assert "Nothing was deleted" in content and kwargs["ephemeral"] is True
    assert store.get_player("1") is not None

    content, kwargs = sent(await run(bot, "forget", slash(bot, world.engine), True))
    assert "erased" in content and kwargs["ephemeral"] is True
    assert store.get_player("1") is None
    for table in ("votes", "preps", "deeds", "raid_glory"):
        assert store.one(f"SELECT COUNT(*) AS n FROM {table} WHERE user_id = ?", ("1",))["n"] == 0
    assert store.vote_counts(chapter["id"], 3) == [0, 0, 0]
    assert store.get_chapter(chapter["id"]) is not None  # the published story is untouched

    content, _ = sent(await run(bot, "forget", slash(bot, world.engine), True))
    assert "nothing stored" in content
    await bot.close()
