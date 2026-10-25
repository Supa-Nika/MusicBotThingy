"""Voice-channel helpers shared by the music and vcmanager modules.

  adjust()    change a voice channel's user limit by +-1, safely: fresh read from the API, one lock per
              channel, never crosses 0 (= unlimited) or Discord's cap of 99.
  bot_set()   keeps the bot's own seat paid for: +1 on the limit while the bot sits in a limited channel,
              -1 when it leaves / is moved / is kicked. Idempotent, so it can be called from anywhere.
  reconcile() on start-up, gives back seats a crashed/restarted bot never returned.
  state/save  tiny JSON file (TARDBOT_STATE, default ./tardbot_state.json) so a restart never leaves a raised
              limit or a temp channel behind. NixOS: the working directory is /var/lib/tardbot, so it just works.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import discord

log = logging.getLogger("tardbot.vc")

MAX_LIMIT = 99  # Discord's cap for a voice channel's user limit
STATE_FILE = Path(os.getenv("TARDBOT_STATE", "tardbot_state.json"))


def humans_in(channel) -> int:
    """Non-bot users in a voice channel (users missing from the member cache count as humans)."""
    known = channel.members
    unknown = max(len(channel.voice_states) - len(known), 0)
    return sum(not m.bot for m in known) + unknown


# --------------------------------------------------------------------------- state
def _load() -> dict:
    data: dict = {}
    try:
        data = json.loads(STATE_FILE.read_text("utf-8"))
        if not isinstance(data, dict):
            data = {}
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log.warning("cannot read %s (%s) - starting with empty state", STATE_FILE, e)
    data.setdefault("bot_slots", {})      # guild id -> {"ch": channel id, "bumped": bool}
    data.setdefault("temp_channels", {})  # channel id -> {"guild": id, "owner": user id}
    return data


state = _load()


def save() -> None:
    try:
        tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(state), "utf-8")
        os.replace(tmp, STATE_FILE)  # atomic: a crash mid-write cannot corrupt the file
    except OSError as e:
        log.warning("cannot write %s: %s", STATE_FILE, e)


_locks: dict[object, asyncio.Lock] = {}


def _lock(key: object) -> asyncio.Lock:
    return _locks.setdefault(key, asyncio.Lock())


# --------------------------------------------------------------------------- limits
@dataclass
class Change:
    changed: bool
    old: int
    new: int
    why: str  # "ok" | "unlimited" | "room" | "max" | "min" | "gone"


async def adjust(guild: discord.Guild, channel_id: int, delta: int, *, only_if_full: bool = False) -> Change:
    """Add `delta` to the channel's user limit.

    Nothing happens (changed=False, `why` says why) when the channel has no limit, would pass Discord's cap,
    would drop to 0 (which means *unlimited*), is gone, or - with only_if_full - still has a free seat.
    Raises discord.Forbidden / HTTPException when the edit itself fails.
    """
    async with _lock(("ch", channel_id)):
        try:
            ch = await guild.fetch_channel(channel_id)  # the cache may lag behind an edit made a moment ago
        except discord.NotFound:
            return Change(False, 0, 0, "gone")
        if not isinstance(ch, discord.VoiceChannel):
            return Change(False, 0, 0, "gone")
        old = ch.user_limit
        if old == 0:
            return Change(False, 0, 0, "unlimited")
        if only_if_full and len(ch.voice_states) < old:  # bots count towards the limit too
            return Change(False, old, old, "room")
        new = old + delta
        if new > MAX_LIMIT:
            return Change(False, old, old, "max")
        if new < 1:
            return Change(False, old, old, "min")
        await ch.edit(user_limit=new, reason=f"tardbot: voice limit {delta:+d}")
        return Change(True, old, new, "ok")


async def undo(guild: discord.Guild, channel_id: int) -> None:
    """Give one seat back (-1). Never raises: the channel may be gone or the permission taken away."""
    try:
        await adjust(guild, channel_id, -1)
    except discord.HTTPException as e:
        log.warning("could not lower the limit of channel %s: %s", channel_id, e)


# --------------------------------------------------------------------------- the bot's own seat
async def bot_set(guild: discord.Guild, channel) -> None:
    """Make the bot's paid seat sit on `channel` (None = the bot is not in voice). Idempotent."""
    async with _lock(("bot", guild.id)):
        slots = state["bot_slots"]
        gid = str(guild.id)
        held = slots.get(gid)
        want = channel.id if channel is not None else None
        if (held["ch"] if held else None) == want:
            return
        entry = None
        if channel is not None:
            bumped = False
            try:
                bumped = (await adjust(guild, channel.id, +1)).changed  # False: unlimited / already 99
            except discord.HTTPException as e:
                log.warning("could not raise the limit of %s for the bot (needs Manage Channels): %s", channel.name, e)
            entry = {"ch": channel.id, "bumped": bumped}
        slots.pop(gid, None)
        if entry:
            slots[gid] = entry
        save()
        if held and held.get("bumped"):
            await undo(guild, held["ch"])


async def reconcile(bot: discord.Client) -> None:
    """After a restart the bot is in no voice channel: return every seat it still holds."""
    await bot.wait_until_ready()
    for gid, entry in list(state["bot_slots"].items()):
        guild = bot.get_guild(int(gid))
        vc = guild.voice_client if guild else None
        if vc and vc.channel and vc.channel.id == entry["ch"]:
            continue  # still connected (e.g. the module was reloaded)
        state["bot_slots"].pop(gid, None)
        save()
        if guild and entry.get("bumped"):
            await undo(guild, entry["ch"])
