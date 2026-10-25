"""Voice channel manager.

  $join <vc name>          get into a voice channel, even a full one. If it is full its limit goes up by 1.
                           In a voice channel yourself: you are moved over. Not in one: only the limit goes up
                           and you click in yourself (the seat is NOT reserved for you).
  $vc <name> <max people>  temporary voice channel (0 = no limit). Deleted once everybody has left.

The bot needs Manage Channels (limits, create / delete) and Move Members (pulling people in).

  TEMP_VC_GRACE_SECONDS=5    wait this long after the last person left before deleting (absorbs reconnects)
  TEMP_VC_EMPTY_SECONDS=60   a temp channel nobody ever joined is deleted after this long
  TEMP_VC_PER_USER=2         temp channels one person may own at the same time
  TEMP_VC_PER_GUILD=20       temp channels per server
  TEMP_VC_CATEGORY_ID=...    category for temp channels (default: the creator's current category)
  VC_COMMAND_COOLDOWN=3      seconds between two uses of $join / $vc by the same person
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

import discord
from discord.ext import commands

import vcshared as shared
from discordlog import request

log = logging.getLogger("tardbot.vc")

GRACE = float(os.getenv("TEMP_VC_GRACE_SECONDS", "5"))
NEVER_JOINED = float(os.getenv("TEMP_VC_EMPTY_SECONDS", "60"))
PER_USER = int(os.getenv("TEMP_VC_PER_USER", "2"))
PER_GUILD = int(os.getenv("TEMP_VC_PER_GUILD", "20"))
CATEGORY_ID = os.getenv("TEMP_VC_CATEGORY_ID", "").strip()
COOLDOWN = float(os.getenv("VC_COMMAND_COOLDOWN", "3"))
MAX_LIMIT = shared.MAX_LIMIT

_ID = re.compile(r"<#(\d+)>|(\d{15,21})")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def find_voice(guild: discord.Guild, author: discord.Member, query: str):
    """-> (channel | None, ambiguous matches). Only channels the author can see are considered,
    so an ambiguity list never leaks the name of a private channel."""
    visible = [c for c in guild.voice_channels if c.permissions_for(author).view_channel]
    q = query.strip().lstrip("#").strip()
    if not q:
        return None, []
    m = _ID.fullmatch(q)
    if m:  # <#mention> or a raw id
        cid = int(m.group(1) or m.group(2))
        return next((c for c in visible if c.id == cid), None), []
    key = q.casefold()
    for test in (lambda n: n == key, lambda n: n.startswith(key), lambda n: key in n):
        hits = [c for c in visible if test(c.name.casefold())]
        if len(hits) == 1:
            return hits[0], []
        if hits:
            return None, hits
    return None, []


def _can_move(me: discord.Member, src: discord.abc.GuildChannel, dst: discord.VoiceChannel) -> bool:
    p_dst, p_src = dst.permissions_for(me), src.permissions_for(me)
    return p_dst.move_members and p_dst.connect and p_src.move_members


class VCManager(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.timers: dict[int, asyncio.Task] = {}   # temp channel id -> pending delete
        self.last_use: dict[int, float] = {}        # user id -> monotonic time of the last command
        self.boot_task: asyncio.Task | None = None

    @property
    def temps(self) -> dict[str, dict]:
        return shared.state["temp_channels"]

    async def cog_load(self) -> None:
        self.boot_task = asyncio.create_task(self._adopt())

    async def cog_unload(self) -> None:
        for t in (*self.timers.values(), self.boot_task):
            if t and not t.done():
                t.cancel()

    def _throttled(self, user_id: int) -> float:
        now = time.monotonic()
        wait = COOLDOWN - (now - self.last_use.get(user_id, -1e9))
        if wait > 0:
            return wait
        self.last_use[user_id] = now
        return 0.0

    # ------------------------------------------------------------------ $join
    @commands.command(name="join", usage="<vc name>")
    @commands.guild_only()
    async def join_cmd(self, ctx: commands.Context, *, name: str):
        """$join <vc name> - get into a (full) voice channel."""
        if wait := self._throttled(ctx.author.id):
            return await ctx.reply(f"⏳ Try again in {wait:.0f}s.")
        guild, me, author = ctx.guild, ctx.guild.me, ctx.author

        target, ambiguous = find_voice(guild, author, name)
        if ambiguous:
            shown = ", ".join(c.mention for c in ambiguous[:6])
            return await ctx.reply(f"Several channels match: {shown}. Use the exact name or a #mention.")
        if target is None:
            return await ctx.reply(f"No voice channel matching `{discord.utils.escape_markdown(name.strip()[:60])}`.")
        if not target.permissions_for(author).connect:
            return await ctx.reply(f"You're not allowed to connect to {target.mention}.")

        here = author.voice.channel if author.voice else None
        if here is not None and here.id == target.id:
            return await ctx.reply("You're already in there 🙂")
        can_move = here is not None and _can_move(me, here, target)

        # raise the limit by one - but only if the channel is actually full, so repeated $join never ratchets it up
        try:
            res = await shared.adjust(guild, target.id, +1, only_if_full=True)
        except discord.Forbidden:
            return await ctx.reply(f"{target.mention} is full and I'm missing **Manage Channels** to make room.")
        except discord.HTTPException as e:
            log.warning("$join: could not raise the limit of %s: %s", target.name, e)
            return await ctx.reply("❌ Couldn't change the limit - check the bot's log.")
        if res.why == "gone":
            return await ctx.reply("That channel just disappeared.")
        if res.why == "max":
            return await ctx.reply(f"{target.mention} is full and already at Discord's maximum of {MAX_LIMIT}.")
        bumped = res.changed
        extra = f" (limit {res.old} → {res.new})" if bumped else ""

        if can_move:
            try:
                await author.move_to(target, reason="tardbot: $join")
            except discord.HTTPException as e:
                if bumped:
                    await shared.undo(guild, target.id)  # nobody got in: take the seat back
                if isinstance(e, discord.Forbidden):
                    return await ctx.reply("I'm not allowed to move you there (Move Members).")
                return await ctx.reply("Couldn't move you - are you still in a voice channel?")
            request(log, "$join: %s -> %s%s", author, target.name, extra)
            return await ctx.reply(f"➡️ Moved you to {target.mention}{extra}")

        request(log, "$join: %s asked for a seat in %s%s", author, target.name, extra)
        why_not = ""
        if here is not None:
            why_not = " I can't move you (needs **Move Members**), so click it yourself."
        if bumped:
            return await ctx.reply(f"➕ {target.mention} was full{extra}. Hop in - the seat isn't reserved.{why_not}")
        if res.why == "unlimited":
            return await ctx.reply(f"{target.mention} has no limit - just join it.{why_not}")
        return await ctx.reply(f"{target.mention} still has room - just join it.{why_not}")

    # ------------------------------------------------------------------ $vc
    @commands.command(name="vc", usage="<name> <max people, 0 = no limit>")
    @commands.guild_only()
    async def vc_cmd(self, ctx: commands.Context, *, args: str):
        """$vc <name> <max people> - temporary voice channel, deleted when everyone has left."""
        if wait := self._throttled(ctx.author.id):
            return await ctx.reply(f"⏳ Try again in {wait:.0f}s.")
        guild, me, author = ctx.guild, ctx.guild.me, ctx.author
        usage = f"Usage: `{ctx.prefix}vc <name> <max people 0-{MAX_LIMIT}>` (0 = no limit), e.g. `{ctx.prefix}vc movie night 6`"

        parts = args.strip().rsplit(None, 1)
        if len(parts) != 2 or not re.fullmatch(r"\d{1,4}", parts[1]):
            return await ctx.reply(usage)
        limit = int(parts[1])
        if limit > MAX_LIMIT:
            return await ctx.reply(f"Discord allows at most {MAX_LIMIT} people per voice channel (0 = no limit).")
        name = " ".join(_CTRL.sub("", parts[0]).split())
        if not name:
            return await ctx.reply(usage)
        if len(name) > 100:
            return await ctx.reply("That name is too long (max 100 characters).")

        mine = [cid for cid, i in self.temps.items() if i["guild"] == guild.id and i["owner"] == author.id]
        if len(mine) >= PER_USER:
            links = " ".join(f"<#{c}>" for c in mine)
            return await ctx.reply(f"You already own {len(mine)} temp channel(s): {links}. Wait until they're gone.")
        if sum(i["guild"] == guild.id for i in self.temps.values()) >= PER_GUILD:
            return await ctx.reply("There are too many temp channels right now - try again later.")

        try:
            ch = await guild.create_voice_channel(
                name, category=self._category(guild, author), user_limit=limit,
                reason=f"tardbot: $vc by {author}")
        except discord.Forbidden:
            return await ctx.reply("I'm missing **Manage Channels** (in that category) to create it.")
        except discord.HTTPException as e:
            log.warning("$vc: could not create %r: %s", name, e)
            return await ctx.reply("❌ Couldn't create the channel - check the bot's log.")
        self.temps[str(ch.id)] = {"guild": guild.id, "owner": author.id}
        shared.save()
        self._schedule(ch.id, guild.id, NEVER_JOINED)  # nobody there yet: deleted if nobody ever shows up
        request(log, "$vc: %s created %r (max %s)", author, ch.name, limit or "unlimited")

        here = author.voice.channel if author.voice else None
        moved = False
        if here is not None and _can_move(me, here, ch):
            try:
                await author.move_to(ch, reason="tardbot: $vc")
                moved = True
            except discord.HTTPException:
                pass  # left voice in the meantime: they get the link instead
        cap = f"max {limit}" if limit else "no limit"
        if moved:
            return await ctx.reply(f"🔊 {ch.mention} created ({cap}) and you're in. It disappears when everyone has left.")
        await ctx.reply(f"🔊 {ch.mention} created ({cap}). Join within {NEVER_JOINED:.0f}s or it is deleted; "
                        f"it disappears when everyone has left.")

    def _category(self, guild: discord.Guild, author: discord.Member) -> discord.CategoryChannel | None:
        cat = None
        if CATEGORY_ID.isdigit():
            found = guild.get_channel(int(CATEGORY_ID))
            cat = found if isinstance(found, discord.CategoryChannel) else None
        if cat is None and author.voice and author.voice.channel:
            cat = author.voice.channel.category
        # the new channel inherits the category's permissions: skip it if the bot can't manage it
        # or if it would lock the creator out of their own channel
        if cat is not None and not (cat.permissions_for(guild.me).manage_channels and cat.permissions_for(author).connect):
            cat = None
        return cat

    # ------------------------------------------------------------------ temp channel lifecycle
    def _forget(self, cid: int) -> None:
        self.temps.pop(str(cid), None)
        shared.save()

    def _cancel(self, cid: int) -> None:
        t = self.timers.pop(cid, None)
        if t and not t.done() and t is not asyncio.current_task():
            t.cancel()

    def _schedule(self, cid: int, guild_id: int, delay: float) -> None:
        t = self.timers.get(cid)
        if t and not t.done():
            return
        self.timers[cid] = asyncio.create_task(self._delete_later(cid, guild_id, delay))

    def _check(self, ch: discord.VoiceChannel) -> None:
        """Someone joined / left a temp channel: delete it soon if no human is left, keep it otherwise."""
        if shared.humans_in(ch) > 0:
            self._cancel(ch.id)
        else:
            self._schedule(ch.id, ch.guild.id, GRACE)

    async def _delete_later(self, cid: int, guild_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            guild = self.bot.get_guild(guild_id)
            ch = guild.get_channel(cid) if guild else None
            if ch is None:  # deleted by hand while we waited
                return self._forget(cid)
            if shared.humans_in(ch) > 0:  # somebody (re)joined during the grace period
                return
            for _ in range(3):
                try:
                    await ch.delete(reason="tardbot: temp voice channel is empty")
                except discord.NotFound:
                    return self._forget(cid)
                except discord.Forbidden:
                    log.warning("cannot delete temp channel %r: missing Manage Channels", ch.name)
                    return self._forget(cid)  # retrying would never work
                except discord.HTTPException as e:
                    log.warning("deleting temp channel %r failed: %s - retrying", ch.name, e)
                    await asyncio.sleep(5)
                else:
                    request(log, "temp channel %r deleted (empty)", ch.name)
                    return self._forget(cid)
            # still tracked: the next voice event or the next start-up pass tries again
        finally:
            if self.timers.get(cid) is asyncio.current_task():
                self.timers.pop(cid, None)

    async def _adopt(self) -> None:
        """After a restart: empty temp channels are deleted, occupied ones are tracked again."""
        await self.bot.wait_until_ready()
        for cid, info in list(self.temps.items()):
            guild = self.bot.get_guild(info["guild"])
            if guild is None:
                continue
            ch = guild.get_channel(int(cid))
            if ch is None:
                self._forget(int(cid))
                continue
            self._check(ch)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        if before.channel == after.channel:  # mute / deafen / stream: nobody came or went
            return
        for ch in (before.channel, after.channel):
            if ch is not None and str(ch.id) in self.temps:
                self._check(ch)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if str(channel.id) in self.temps:
            self._cancel(channel.id)
            self._forget(channel.id)


def help_text(prefix: str) -> str:
    return (f"`{prefix}join <vc name>` - jump into a voice channel; if it is full its limit goes up by 1 "
            f"(not in voice yourself? only the limit goes up)\n"
            f"`{prefix}vc <name> <max people>` - temporary voice channel (0 = no limit), gone when everyone has left")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VCManager(bot))
    sections = getattr(bot, "help_sections", None)
    if sections is not None:
        sections["Voice"] = lambda: help_text(str(bot.command_prefix))


async def teardown(bot: commands.Bot) -> None:
    if getattr(bot, "help_sections", None) is not None:
        bot.help_sections.pop("Voice", None)
