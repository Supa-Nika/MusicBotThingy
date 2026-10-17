from __future__ import annotations

import asyncio
import logging
import os
import random

import discord
from discord.ext import commands
from dotenv import load_dotenv

import sources
from sources import SourceError, Track

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("musicbot")

if not discord.opus.is_loaded():
    opus_path = os.getenv("OPUS_LIB") or "libopus.so.0"
    try:
        discord.opus.load_opus(opus_path)
        log.info("Loaded Opus from %s", opus_path)
    except OSError:
        log.error("Could not load Opus from %s - voice playback will fail", opus_path)

PREFIX = "$"
EMPTY_GRACE = float(os.getenv("EMPTY_VC_GRACE_SECONDS", "10"))
SPREAD = os.getenv("INTERLEAVE_MODE", "alternate").lower() == "spread"
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

intents = discord.Intents.default()  # includes guilds + voice_states
intents.message_content = True       # privileged: enable it in the developer portal
bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None,
    allowed_mentions=discord.AllowedMentions.none(),
)

players: dict[int, "GuildPlayer"] = {}


def humans_in(channel: discord.VoiceChannel) -> int:
    """Non-bot users in a voice channel (users missing from the member cache count as humans)."""
    known = channel.members
    unknown = max(len(channel.voice_states) - len(known), 0)
    return sum(not m.bot for m in known) + unknown


class GuildPlayer:
    def __init__(self, guild: discord.Guild, text: discord.abc.Messageable):
        self.guild = guild
        self.text = text
        self.upcoming: list[Track] = []
        self.current: Track | None = None
        self.epoch = 0  # bumped whenever the queue is replaced, so stale work is dropped
        self.task: asyncio.Task | None = None
        self.leave_timer: asyncio.Task | None = None
        self.prefetch: asyncio.Task | None = None

    @property
    def vc(self) -> discord.VoiceClient | None:
        return self.guild.voice_client

    async def say(self, text: str) -> None:
        try:
            await self.text.send(text)
        except discord.HTTPException:
            pass

    # ---- queue control
    def replace(self, tracks: list[Track]) -> None:
        self.epoch += 1
        self.upcoming = tracks
        self.skip()
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self._run())

    def add(self, tracks: list[Track]) -> None:
        self.upcoming = sources.interleave(self.upcoming, tracks, SPREAD)

    def skip(self) -> None:
        vc = self.vc
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()  # triggers `after`, which lets the loop move on

    # ---- playback loop
    async def _run(self) -> None:
        try:
            await self._play_all()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("player loop crashed")
        self.current = None
        await self.shutdown()  # queue finished (or crashed): leave

    async def _play_all(self) -> None:
        loop = asyncio.get_running_loop()
        while self.upcoming:
            epoch = self.epoch
            track = self.upcoming.pop(0)
            self.current = track
            self._prefetch_next()
            try:
                await track.resolve()
            except Exception as e:  # noqa: BLE001
                log.warning("could not resolve %s: %s", track.label, e)
                await self.say(f"⚠️ Skipping **{track.label}** ({sources.short(e)})")
                continue
            if epoch != self.epoch:
                continue  # queue was replaced while we were resolving
            vc = self.vc
            if not vc or not vc.is_connected():
                return
            done = asyncio.Event()

            def after(err, done=done):
                if err:
                    log.error("playback error: %s", err)
                loop.call_soon_threadsafe(done.set)

            vc.play(
                discord.FFmpegPCMAudio(track.stream_url, before_options=FFMPEG_BEFORE, options="-vn"),
                after=after,
            )
            await self.say(f"▶️ **{track.label}**")
            await done.wait()

    def _prefetch_next(self) -> None:
        """Resolve the next track while the current one plays, so there is no gap."""
        if self.upcoming:
            self.prefetch = asyncio.create_task(self._safe_resolve(self.upcoming[0]))

    @staticmethod
    async def _safe_resolve(track: Track) -> None:
        try:
            await track.resolve()
        except Exception:  # noqa: BLE001 - the real attempt will report it
            pass

    # ---- leaving
    def schedule_leave(self) -> None:
        if self.leave_timer and not self.leave_timer.done():
            return
        self.leave_timer = asyncio.create_task(self._leave_if_still_empty())

    def cancel_leave(self) -> None:
        if self.leave_timer and not self.leave_timer.done():
            self.leave_timer.cancel()

    async def _leave_if_still_empty(self) -> None:
        await asyncio.sleep(EMPTY_GRACE)
        vc = self.vc
        if vc and vc.channel and humans_in(vc.channel) == 0:
            await self.say("👋 Voice channel is empty, leaving.")
            await self.shutdown()

    async def shutdown(self) -> None:
        players.pop(self.guild.id, None)
        self.upcoming.clear()
        self.epoch += 1
        me = asyncio.current_task()
        for t in (self.task, self.leave_timer, self.prefetch):
            if t and t is not me and not t.done():
                t.cancel()
        vc = self.vc
        if vc:
            vc.stop()
            await vc.disconnect()


# --------------------------------------------------------------------------- helpers
async def author_channel(ctx: commands.Context) -> discord.VoiceChannel | None:
    voice = ctx.author.voice
    if not voice or not voice.channel:
        await ctx.reply("Join a voice channel first - I only join the channel of whoever asks.")
        return None
    return voice.channel


async def get_player(ctx: commands.Context, dest: discord.VoiceChannel) -> GuildPlayer | None:
    me = ctx.guild.me
    if not dest.permissions_for(me).connect or not dest.permissions_for(me).speak:
        await ctx.reply(f"I'm missing Connect/Speak permission in **{dest.name}**.")
        return None
    vc = ctx.guild.voice_client
    try:
        if vc and vc.channel != dest:
            if humans_in(vc.channel) > 0:
                await ctx.reply(f"I'm already playing for people in **{vc.channel.name}**.")
                return None
            await vc.move_to(dest)
        elif not vc:
            await dest.connect(self_deaf=True)
    except (asyncio.TimeoutError, discord.ClientException) as e:
        await ctx.reply(f"Couldn't join the voice channel: {e}")
        return None
    player = players.get(ctx.guild.id)
    if not player:
        player = players[ctx.guild.id] = GuildPlayer(ctx.guild, ctx.channel)
    player.text = ctx.channel
    if humans_in(dest) == 0:  # the requester left while we were connecting
        player.schedule_leave()
    return player


async def load_for(ctx: commands.Context, link: str) -> sources.Loaded | None:
    link = link.strip().strip("<>")
    async with ctx.typing():
        try:
            loaded = await sources.load(link)
        except SourceError as e:
            await ctx.reply(f"❌ {e}")
            return None
    if loaded.shuffle:
        random.shuffle(loaded.tracks)
    return loaded


async def controller(ctx: commands.Context) -> GuildPlayer | None:
    player, vc = players.get(ctx.guild.id), ctx.guild.voice_client
    if not player or not vc:
        await ctx.reply("Nothing is playing.")
        return None
    if not ctx.author.voice or ctx.author.voice.channel != vc.channel:
        await ctx.reply("Join my voice channel first.")
        return None
    return player


# --------------------------------------------------------------------------- commands
@bot.command()
@commands.guild_only()
async def play(ctx: commands.Context, *, link: str):
    """$play <link> - Spotify / YouTube / YouTube Music track or playlist. Replaces the current queue."""
    dest = await author_channel(ctx)
    if not dest:
        return
    loaded = await load_for(ctx, link)
    if not loaded:
        return
    player = await get_player(ctx, dest)
    if not player:
        return
    player.replace(loaded.tracks)
    icon = "🔀" if loaded.shuffle else "🎵"
    await ctx.reply(f"{icon} Playing {len(loaded.tracks)} track(s) from {loaded.name}.\n{loaded.note}".strip())


@bot.command(name="queue")
@commands.guild_only()
async def queue_cmd(ctx: commands.Context, *, link: str = None):
    """$queue <link> - mix another playlist into the current one. $queue alone shows what's next."""
    player = players.get(ctx.guild.id)
    if not link:
        if not player or not (player.current or player.upcoming):
            return await ctx.reply("Nothing queued.")
        lines = [f"Now: **{player.current.label}**"] if player.current else []
        lines += [f"{i}. {t.label}" for i, t in enumerate(player.upcoming[:10], 1)]
        if len(player.upcoming) > 10:
            lines.append(f"...and {len(player.upcoming) - 10} more")
        return await ctx.reply("\n".join(lines))

    dest = await author_channel(ctx)
    if not dest:
        return
    vc = ctx.guild.voice_client
    if player and vc and vc.channel != dest:
        return await ctx.reply("Join my voice channel to add to the queue.")
    loaded = await load_for(ctx, link)
    if not loaded:
        return
    player = await get_player(ctx, dest)
    if not player:
        return
    if player.current or player.upcoming:
        player.add(loaded.tracks)
        await ctx.reply(f"🔀 Mixed {len(loaded.tracks)} track(s) from {loaded.name} into the queue.\n{loaded.note}".strip())
    else:  # nothing playing yet: behaves like $play
        player.replace(loaded.tracks)
        await ctx.reply(f"🔀 Playing {len(loaded.tracks)} track(s) from {loaded.name}.\n{loaded.note}".strip())


@bot.command(aliases=["next"])
@commands.guild_only()
async def skip(ctx: commands.Context):
    """$skip - next track."""
    player = await controller(ctx)
    if player:
        player.skip()


@bot.command(aliases=["leave", "dc"])
@commands.guild_only()
async def stop(ctx: commands.Context):
    """$stop - clear the queue and leave."""
    player = await controller(ctx)
    if player:
        await player.shutdown()
        await ctx.reply("⏹️ Stopped.")


@bot.command()
@commands.guild_only()
async def pause(ctx: commands.Context):
    player = await controller(ctx)
    if player and player.vc.is_playing():
        player.vc.pause()


@bot.command()
@commands.guild_only()
async def resume(ctx: commands.Context):
    player = await controller(ctx)
    if player and player.vc.is_paused():
        player.vc.resume()


@bot.command(aliases=["np", "nowplaying"])
@commands.guild_only()
async def now(ctx: commands.Context):
    """$np - what's playing."""
    player = players.get(ctx.guild.id)
    if player and player.current:
        await ctx.reply(f"🎶 **{player.current.label}** ({len(player.upcoming)} more in queue)")
    else:
        await ctx.reply("Nothing is playing.")


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    await ctx.reply(
        f"`{PREFIX}play <link>` - play a Spotify / YouTube / YouTube Music track or playlist (playlists shuffle; replaces the queue)\n"
        f"`{PREFIX}queue <link>` - mix another playlist into the current one\n"
        f"`{PREFIX}queue` - show what's next\n"
        f"`{PREFIX}skip` `{PREFIX}pause` `{PREFIX}resume` `{PREFIX}np` `{PREFIX}stop`"
    )


# --------------------------------------------------------------------------- events
@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    player = players.get(member.guild.id)
    if not player:
        return
    if member.id == bot.user.id:
        if after.channel is None:  # kicked or disconnected from outside
            await player.shutdown()
        return
    vc = member.guild.voice_client
    if not vc or not vc.channel:
        return
    if humans_in(vc.channel) > 0:
        player.cancel_leave()
    else:
        player.schedule_leave()


@bot.event
async def on_command_error(ctx: commands.Context, err: Exception):
    if isinstance(err, commands.CommandNotFound):
        return
    if isinstance(err, commands.MissingRequiredArgument):
        return await ctx.reply(f"Usage: `{PREFIX}{ctx.command.name} <link>`")
    if isinstance(err, commands.NoPrivateMessage):
        return await ctx.reply("Use me in a server.")
    log.error("command %s failed", ctx.command, exc_info=err)
    await ctx.reply("💥 Something went wrong - check the bot's log.")


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s guilds)", bot.user, len(bot.guilds))


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN in .env (see .env.example)")
    bot.run(token, log_handler=None)
