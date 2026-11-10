"""The Discord side: slash commands, vote buttons, and posting each new chapter."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import discord
from discord import app_commands

from .characters import CLASSES, PREP_ACTIONS
from .engine import SagaEngine
from .squeek_api import SqueekApi
from .story import PREMISE

log = logging.getLogger(__name__)

DISCLAIMER = "Sepolia testnet only · Unofficial community project, not affiliated with Squeekchain"
BRANCH_LINE = {
    "slept": "😴 Squeektoshi slept. The Dreamtide is in.",
    "trotted": "🐹 Squeektoshi trotted. An ordinary waking hour.",
    "sprinted": "💨 Squeektoshi sprinted. The Wheelwind is roaring.",
}
COLOURS = {"slept": 0x5B6FB5, "trotted": 0xE0A458, "sprinted": 0xD9534F}
NO_MENTIONS = discord.AllowedMentions.none()


def chapter_embed(engine: SagaEngine, chapter: Any, *, with_choice: bool = True) -> discord.Embed:
    store, settings = engine.store, engine.settings
    number = store.chapter_number(chapter["id"])
    embed = discord.Embed(
        title=f"Chapter {number} · {chapter['title']}",
        description=chapter["body"],
        colour=COLOURS.get(chapter["branch"], 0x888888),
        url=f"{settings.site_url}/chapter/{number}",
    )
    hours = (chapter["last_round"] - chapter["first_round"] + 1) // 6
    embed.add_field(
        name="The wheel decided",
        value=(
            f"{BRANCH_LINE.get(chapter['branch'], chapter['branch'])}\n"
            f"{chapter['work']} work units settled on SqueekCore over rounds "
            f"{chapter['first_round']}–{chapter['last_round']} ({hours} h)."
        ),
        inline=False,
    )
    if chapter["vote_result"]:
        result = json.loads(chapter["vote_result"])
        how = {
            "votes": f"won the vote ({max(result['counts'])} of {sum(result['counts'])})",
            "wheel_tiebreak": "the vote was tied, so the wheel's settled work broke the tie",
            "wheel_no_votes": "nobody voted, so the wheel's settled work picked",
        }.get(result["decidedBy"], "chosen")
        embed.add_field(name="The company decided", value=f"**{result['label']}** — {how}.", inline=False)
    if with_choice:
        options = json.loads(chapter["options"])
        lines = [f"**{index}. {option['label']}** — {option['detail']}" for index, option in enumerate(options, 1)]
        embed.add_field(name=chapter["choice_prompt"], value="\n".join(lines), inline=False)
    embed.set_footer(text=DISCLAIMER)
    return embed


class VoteButton(discord.ui.DynamicItem[discord.ui.Button], template=r"saga:vote:(?P<chapter>\d+):(?P<idx>\d)"):
    """A persistent vote button. Its custom id carries the chapter and option, so it
    keeps working after a restart and on every server the chapter was posted to."""

    def __init__(self, chapter_id: int, idx: int, label: str):
        super().__init__(
            discord.ui.Button(
                label=label[:80], style=discord.ButtonStyle.primary, custom_id=f"saga:vote:{chapter_id}:{idx}"
            )
        )
        self.chapter_id, self.idx = chapter_id, idx

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["chapter"]), int(match["idx"]), item.label or "Vote")

    async def callback(self, interaction: discord.Interaction) -> None:
        engine: SagaEngine = interaction.client.engine  # type: ignore[attr-defined]
        if interaction.guild_id is None:
            await interaction.response.send_message("Votes are cast from a server.", ephemeral=True)
            return
        engine.ensure_player(str(interaction.user.id), interaction.user.display_name, str(interaction.guild_id))
        outcome = engine.vote(str(interaction.user.id), str(interaction.guild_id), self.chapter_id, self.idx)
        if outcome == "closed":
            message = "That vote has closed: the story has moved on. Use `/story` for the current chapter."
        else:
            verb = "Vote recorded" if outcome == "recorded" else "Vote changed"
            message = (
                f"{verb}: **{self.item.label}**. You can change it until the next chapter. "
                "Your character will remember this."
            )
        await interaction.response.send_message(message, ephemeral=True, allowed_mentions=NO_MENTIONS)


def chapter_view(engine: SagaEngine, chapter: Any, *, votable: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if votable:
        for index, option in enumerate(json.loads(chapter["options"])):
            view.add_item(VoteButton(chapter["id"], index, option["label"]))
    number = engine.store.chapter_number(chapter["id"])
    view.add_item(discord.ui.Button(label="Settlement tx", url=engine.deployment.tx_url(chapter["settlement_tx"])))
    view.add_item(discord.ui.Button(label="Chronicle", url=f"{engine.settings.site_url}/chapter/{number}"))
    return view


class SagaBot(discord.Client):
    def __init__(self, engine: SagaEngine, api: SqueekApi):
        super().__init__(intents=discord.Intents(guilds=True), allowed_mentions=NO_MENTIONS)
        self.engine = engine
        self.api = api
        self.tree = app_commands.CommandTree(self)
        register_commands(self)
        engine.listeners.append(self.post_chapter)

    async def setup_hook(self) -> None:
        self.add_dynamic_items(VoteButton)
        await self.tree.sync()

    async def on_ready(self) -> None:
        for guild in self.guilds:
            self.engine.store.upsert_guild(str(guild.id), guild.name)
        log.info("discord ready as %s in %s guilds", self.user, len(self.guilds))
        # A chapter written while the bot was offline is posted now (once per server).
        latest = self.engine.store.latest_chapter()
        if latest is not None and time.time() - latest["created_at"] < 3600:
            await self.post_chapter(latest["id"])

    async def on_guild_join(self, guild: discord.Guild) -> None:
        self.engine.store.upsert_guild(str(guild.id), guild.name)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self.engine.store.deactivate_guild(str(guild.id))

    async def post_chapter(self, chapter_id: int) -> None:
        """Send a new chapter to every server's saga channel, once each."""
        store = self.engine.store
        chapter = store.get_chapter(chapter_id)
        if chapter is None:
            return
        await self.wait_until_ready()
        already = store.posted_guilds(chapter_id)
        embed = chapter_embed(self.engine, chapter)
        for guild in store.guild_channels():
            if guild["guild_id"] in already:
                continue
            channel = self.get_channel(int(guild["channel_id"]))
            if channel is None or not isinstance(channel, discord.abc.Messageable):
                continue
            try:
                message = await channel.send(
                    embed=embed, view=chapter_view(self.engine, chapter, votable=True), allowed_mentions=NO_MENTIONS
                )
                store.record_post(chapter_id, guild["guild_id"], str(guild["channel_id"]), str(message.id))
            except discord.Forbidden:
                log.warning("no permission to post in guild %s", guild["guild_id"])
            except discord.HTTPException as exc:
                log.warning("could not post chapter %s to guild %s: %s", chapter_id, guild["guild_id"], exc)
            await asyncio.sleep(0.3)


def _player(interaction: discord.Interaction, klass: str | None = None) -> Any:
    engine: SagaEngine = interaction.client.engine  # type: ignore[attr-defined]
    return engine.ensure_player(
        str(interaction.user.id), interaction.user.display_name, str(interaction.guild_id), klass
    )


def register_commands(bot: SagaBot) -> None:
    tree, engine, store = bot.tree, bot.engine, bot.engine.store
    class_choices = [app_commands.Choice(name=title, value=key) for key, (title, _) in CLASSES.items()]
    prep_choices = [app_commands.Choice(name=label, value=key) for key, (label, _) in PREP_ACTIONS.items()]

    @tree.command(name="join", description="Create your character and join the shared saga.")
    @app_commands.describe(role="What kind of companion you are")
    @app_commands.choices(role=class_choices)
    @app_commands.guild_only()
    async def join(interaction: discord.Interaction, role: app_commands.Choice[str]) -> None:
        existed = store.get_player(str(interaction.user.id)) is not None
        player = _player(interaction, role.value)
        title, blurb = CLASSES[player["class"]]
        opening = "Your character is updated" if existed else "Welcome to the company"
        await interaction.response.send_message(
            f"{opening}, **{player['name']}** the {title}: {blurb}.\n"
            "Vote on each chapter's choice, `/prep` once a day for the weekly boss, and check `/me` to see "
            "what the saga remembers about you. The storyteller may name you in a chapter.",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @tree.command(name="me", description="Your character: role, votes, glory and what the saga remembers.")
    @app_commands.guild_only()
    async def me(interaction: discord.Interaction) -> None:
        player = _player(interaction)
        user_id = player["user_id"]
        title, blurb = CLASSES.get(player["class"], CLASSES["wanderer"])
        embed = discord.Embed(title=f"{player['name']} the {title}", description=blurb.capitalize() + ".", colour=0x7A9E7E)
        embed.add_field(name="Glory", value=str(store.player_glory(user_id)))
        embed.add_field(name="Votes cast", value=str(store.vote_total(user_id)))
        embed.add_field(name="Watch streak", value=f"{player['prep_streak']} day(s)")
        votes = store.player_votes(user_id, 5)
        if votes:
            lines = []
            for vote in votes:
                options = json.loads(vote["options"])
                label = options[vote["option_idx"]]["label"] if vote["option_idx"] < len(options) else "?"
                lines.append(f"Ch. {store.chapter_number(vote['chapter_id'])}: {label}")
            embed.add_field(name="Recent votes", value="\n".join(lines), inline=False)
        deeds = store.recent_deeds(user_id, 6, kinds=("cameo", "raid", "prep", "join"))
        if deeds:
            embed.add_field(name="The saga remembers", value="\n".join(f"• {deed['text']}" for deed in deeds), inline=False)
        embed.set_footer(text=DISCLAIMER)
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS)

    @tree.command(name="story", description="The latest chapter and the open vote.")
    @app_commands.guild_only()
    async def story(interaction: discord.Interaction) -> None:
        chapter = store.latest_chapter()
        if chapter is None:
            await interaction.response.send_message(
                f"**The saga has not begun yet.**\n{PREMISE}\n\n{engine.status.message}",
                allowed_mentions=NO_MENTIONS,
            )
            return
        await interaction.response.send_message(
            embed=chapter_embed(engine, chapter), view=chapter_view(engine, chapter, votable=True),
            allowed_mentions=NO_MENTIONS,
        )

    @tree.command(name="prep", description="Daily: get ready for the weekly boss. Only the hamster deals damage.")
    @app_commands.describe(action="How you prepare today")
    @app_commands.choices(action=prep_choices)
    @app_commands.guild_only()
    async def prep(interaction: discord.Interaction, action: app_commands.Choice[str]) -> None:
        player = _player(interaction)
        result = engine.prepare(player["user_id"], action.value)
        if not result["ok"]:
            await interaction.response.send_message(
                "You have already prepared today (days turn over at 00:00 UTC). Your readiness covers "
                "24 hours from when you prepared.",
                ephemeral=True,
            )
            return
        raid = engine.raid_view()
        boss = raid["bossName"] if raid else "the weekly boss"
        await interaction.response.send_message(
            f"**{player['name']}** {PREP_ACTIONS[action.value][1]}. For the next 24 hours, every work unit "
            f"Squeektoshi settles against **{boss}** earns you glory at ×{result['multiplier'] / 100:.1f} "
            f"(watch streak: {result['streak']} day(s)).\n"
            "Nothing you do makes him run. If he sleeps, the Dreamtide does its own work.",
            allowed_mentions=NO_MENTIONS,
        )

    @tree.command(name="raid", description="This week's boss and how far the hamster's real work has worn it down.")
    async def raid(interaction: discord.Interaction) -> None:
        view = engine.raid_view()
        if view is None:
            await interaction.response.send_message("No raid yet: the chain state is not known.", ephemeral=True)
            return
        filled = view["percent"] // 10
        bar = "█" * filled + "░" * (10 - filled)
        state = {
            "active": "Abroad in the Burrowlands",
            "defeated": "Worn down! It has slunk away.",
            "retreated": "Wandered off on its own. A peaceful week.",
        }[view["status"]]
        embed = discord.Embed(title=f"Weekly boss · {view['bossName']}", description=view["bossBlurb"], colour=0xB5651D)
        embed.add_field(name="Status", value=state, inline=False)
        embed.add_field(
            name="Worn down by settled work",
            value=f"`{bar}` {view['percent']}%\n{view['damage']} / {view['hp']} work units",
            inline=False,
        )
        embed.add_field(
            name="What is known",
            value=(
                f"{view['roundsCertified']} of {view['roundsTotal']} rounds certified on SqueekCore; "
                f"{view['roundsUnknown']} not settled yet (unknown, not zero).\n"
                f"Week ends <t:{view['endsAt']}:R>. Ready right now: {view['readyNow']} companion(s)."
            ),
            inline=False,
        )
        embed.add_field(name="Boss strength", value=view["hpBasis"], inline=False)
        embed.set_footer(text=DISCLAIMER)
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_MENTIONS)

    @tree.command(name="leaderboard", description="Glory rankings for this server or across all servers.")
    @app_commands.describe(scope="Which ranking to show")
    @app_commands.choices(scope=[
        app_commands.Choice(name="This server", value="server"),
        app_commands.Choice(name="All servers", value="global"),
    ])
    @app_commands.guild_only()
    async def leaderboard(interaction: discord.Interaction, scope: app_commands.Choice[str]) -> None:
        medals = ["🥇", "🥈", "🥉"]

        def rank(index: int) -> str:
            return medals[index] if index < 3 else f"`{index + 1}.`"

        if scope.value == "server":
            rows = store.top_players(guild_id=str(interaction.guild_id), limit=10)
            title = f"Glory · {interaction.guild.name if interaction.guild else 'this server'}"
            lines = [f"{rank(i)} **{row['name']}** the {CLASSES.get(row['class'], CLASSES['wanderer'])[0]} — {row['glory']}"
                     for i, row in enumerate(rows)]
        else:
            rows = store.top_guilds(limit=10)
            title = "Glory · all servers"
            lines = [f"{rank(i)} **{discord.utils.escape_markdown(row['name'])}** — {row['glory']} "
                     f"({row['heroes']} companion(s))" for i, row in enumerate(rows)]
        embed = discord.Embed(
            title=title,
            description="\n".join(lines) or "No glory yet. `/prep` before the hamster's next run to earn some.",
            colour=0xC9A227,
        )
        embed.set_footer(text=f"Full rankings: {engine.settings.site_url}/leaderboard · {DISCLAIMER}")
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_MENTIONS)

    @tree.command(name="wheel", description="What the chain knows about the hamster's work, and when the next chapter lands.")
    async def wheel(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        status = engine.status
        embed = discord.Embed(title="The Great Wheel", colour=0x4C956C)
        if status.frontier is None:
            settled = "Unknown right now: the chain could not be read."
        else:
            settled = (
                f"Certified through round **{status.frontier}** · {status.cumulative_work} work units all time · "
                f"Squeek block {status.block_height}\nRead at Ethereum block {status.block_number} (Sepolia)."
            )
        embed.add_field(name="Settled on SqueekCore (decides the story)", value=settled, inline=False)
        eta = engine.next_chapter_eta()
        next_line = status.message
        if eta:
            next_line += f"\nThe next hour's last round ends <t:{eta}:R>; settlement follows a few minutes later."
        embed.add_field(name="Next chapter", value=next_line, inline=False)
        live = await bot.api.wheel_now()
        if live["known"]:
            rpm = "?" if live["rpmMilli"] is None else f"{live['rpmMilli'] / 1000:.1f}"
            pending = live["openRoundWorkUnits"]
            live_line = (
                f"Device says: {live['coreStatus'] or '?'} · {rpm} rpm · reading is {live['freshness'] or '?'}"
                + (f" · {pending} unsettled work units in the open round" if pending is not None else "")
            )
        else:
            live_line = f"Unknown ({live['reason'] or 'unavailable'}). Unknown is not zero."
        embed.add_field(name="Live from the Squeekchain API (display only, not settled)", value=live_line, inline=False)
        embed.set_footer(text=DISCLAIMER)
        await interaction.followup.send(embed=embed, allowed_mentions=NO_MENTIONS)

    @tree.command(name="setup", description="Admin: choose the channel where new chapters are posted.")
    @app_commands.describe(channel="Channel for hourly chapters (leave empty to stop posting)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setup(interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        guild = interaction.guild
        if guild is None:
            return
        store.upsert_guild(str(guild.id), guild.name)
        if channel is None:
            store.set_guild_channel(str(guild.id), None)
            await interaction.response.send_message("Chapters will no longer be posted here.", ephemeral=True)
            return
        permissions = channel.permissions_for(guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            await interaction.response.send_message(
                f"I need **Send Messages** and **Embed Links** in {channel.mention} first.", ephemeral=True
            )
            return
        store.set_guild_channel(str(guild.id), str(channel.id))
        await interaction.response.send_message(
            f"New chapters will be posted in {channel.mention}, about once an hour, whenever SqueekCore "
            "has settled a full hour of rounds.\nNote: this server's name and its companions' display names "
            "appear on the public rankings page.",
            ephemeral=True,
        )

    @tree.command(name="about", description="What Squeek Saga is and how it uses Squeekchain.")
    async def about(interaction: discord.Interaction) -> None:
        d = engine.deployment
        embed = discord.Embed(
            title="Squeek Saga",
            description=(
                "One story shared by every server, moved forward by a real hamster.\n\n"
                "Each hour, the work Squeektoshi's wheel settled on **SqueekCore** (Ethereum Sepolia) decides "
                "whether the chapter is a sleep, trot or sprint chapter. Your votes pick what the hero does. "
                "A weekly boss is worn down only by his real settled work. He is never asked to run: sleep "
                "moves the story too."
            ),
            colour=0x7A9E7E,
            url=engine.settings.site_url,
        )
        embed.add_field(name="Commands", value="`/join` `/me` `/story` `/prep` `/raid` `/leaderboard` `/wheel` `/setup`", inline=False)
        embed.add_field(name="SqueekCore", value=f"[{d.core_address}]({d.address_url(d.core_address)})", inline=False)
        if engine.settings.demo_invite_url:
            embed.add_field(name="Demo server", value=engine.settings.demo_invite_url, inline=False)
        embed.add_field(
            name="Good to know",
            value="Testnet only: nothing here has monetary value. The bot never asks for a wallet or a key and signs nothing.",
            inline=False,
        )
        embed.set_footer(text=DISCLAIMER)
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_MENTIONS)
