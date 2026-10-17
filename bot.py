from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
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
SKIP_RATIO = float(os.getenv("SKIP_VOTE_RATIO", "0.25"))  # share of listeners that must want to skip

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


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "??:??"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class TimedSource(discord.FFmpegPCMAudio):
    """FFmpegPCMAudio that counts the 20 ms frames it has delivered (= elapsed play time)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.frames = 0

    def read(self) -> bytes:
        data = super().read()
        if data:
            self.frames += 1
        return data

    @property
    def elapsed(self) -> float:
        return self.frames * 0.02


class GuildPlayer:
    def __init__(self, guild: discord.Guild, text: discord.abc.Messageable):
        self.guild = guild
        self.text = text
        self.tracks: list[Track] = []   # the whole queue, including already-played tracks
        self.index = 0                  # position of the current track in `tracks`
        self.jump: int | None = None    # where to go when the current track ends (None = next)
        self.source: TimedSource | None = None
        self.signal: asyncio.Event | None = None
        self.task: asyncio.Task | None = None
        self.leave_timer: asyncio.Task | None = None
        self.prefetch: asyncio.Task | None = None
        self.skip_votes: set[int] = set()
        self.panel: discord.Message | None = None  # the 'now playing' message with buttons

    @property
    def vc(self) -> discord.VoiceClient | None:
        return self.guild.voice_client

    @property
    def current(self) -> Track | None:
        return self.tracks[self.index] if 0 <= self.index < len(self.tracks) else None

    @property
    def upcoming(self) -> list[Track]:
        return self.tracks[self.index + 1:]

    async def say(self, text: str) -> None:
        try:
            await self.text.send(text)
        except discord.HTTPException:
            pass

    # ---- queue control
    def add(self, new: list[Track]) -> bool:
        """Add tracks. Returns True if this started playback, False if mixed into a running queue."""
        if not self.tracks:
            self.tracks = list(new)
            self.index = 0
            if not self.task or self.task.done():
                self.task = asyncio.create_task(self._run())
            return True
        head = self.tracks[: self.index + 1]
        self.tracks = head + sources.interleave(self.tracks[self.index + 1:], new, SPREAD)
        return False

    def _interrupt(self) -> None:
        """End the current track (also works while it is still being resolved)."""
        vc = self.vc
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()  # triggers `after`, which wakes the loop
        elif self.signal:
            self.signal.set()

    def skip(self) -> None:
        self.jump = None
        self._interrupt()

    def previous(self) -> None:
        self.jump = max(self.index - 1, 0)
        self._interrupt()

    def remove(self, pos: int) -> Track:
        """Remove the track at 0-based position `pos`."""
        track = self.tracks.pop(pos)
        if pos < self.index:
            self.index -= 1
        elif pos == self.index:
            self.jump = self.index  # the next track slid into this slot
            self._interrupt()
        return track

    def _advance(self) -> None:
        if self.jump is not None:
            self.index, self.jump = self.jump, None
        else:
            self.index += 1

    # ---- playback loop
    async def _run(self) -> None:
        try:
            await self._play_all()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("player loop crashed")
        await self.shutdown()  # queue finished (or crashed): leave

    async def _play_all(self) -> None:
        loop = asyncio.get_running_loop()
        while self.index < len(self.tracks):
            track = self.tracks[self.index]
            self.jump = None
            self.source = None
            self.skip_votes.clear()
            signal = self.signal = asyncio.Event()
            self._prefetch_next()
            try:
                await track.resolve()
            except Exception as e:  # noqa: BLE001
                log.warning("could not resolve %s: %s", track.label, e)
                await self.say(f"⚠️ Skipping **{track.label}** ({sources.short(e)})")
                self._advance()
                continue
            if signal.is_set():  # skipped / removed / previous while resolving
                self._advance()
                continue
            vc = self.vc
            if not vc or not vc.is_connected():
                return

            def after(err, signal=signal):
                if err:
                    log.error("playback error: %s", err)
                loop.call_soon_threadsafe(signal.set)

            self.source = TimedSource(track.stream_url, before_options=FFMPEG_BEFORE, options="-vn")
            vc.play(self.source, after=after)
            await self.announce(track)
            await signal.wait()
            self._advance()

    async def announce(self, track: Track) -> None:
        await self._drop_panel()
        try:
            self.panel = await self.text.send(embed=now_playing_embed(track), view=PlayerView(self))
        except discord.HTTPException:
            self.panel = None

    async def _drop_panel(self) -> None:
        """Remove the buttons from the previous 'now playing' message."""
        panel, self.panel = self.panel, None
        if panel:
            try:
                await panel.edit(view=None)
            except discord.HTTPException:
                pass

    def _prefetch_next(self) -> None:
        """Resolve the next track while the current one plays, so there is no gap."""
        nxt = self.tracks[self.index + 1] if self.index + 1 < len(self.tracks) else None
        if nxt:
            self.prefetch = asyncio.create_task(self._safe_resolve(nxt))

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
        self.tracks.clear()
        self.index = 0
        me = asyncio.current_task()
        for t in (self.task, self.leave_timer, self.prefetch):
            if t and t is not me and not t.done():
                t.cancel()
        await self._drop_panel()
        vc = self.vc
        if vc:
            vc.stop()
            await vc.disconnect()


# --------------------------------------------------------------------------- helpers
async def ack(ctx: commands.Context, emoji: str) -> None:
    try:
        await ctx.message.add_reaction(emoji)
    except discord.HTTPException:
        pass


async def suppress_embeds(message: discord.Message) -> None:
    """Hide the link preview on the user's message (needs Manage Messages; silently skipped otherwise)."""
    try:
        await message.edit(suppress=True)
    except discord.HTTPException:
        pass


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
    await suppress_embeds(ctx.message)
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


async def enqueue(ctx: commands.Context, link: str) -> None:
    """Shared by $play <link> and $queue <link>: mix into the queue, or start playing if idle."""
    dest = await author_channel(ctx)
    if not dest:
        return
    player, vc = players.get(ctx.guild.id), ctx.guild.voice_client
    if player and vc and vc.channel != dest:
        await ctx.reply("Join my voice channel to add to the queue.")
        return
    loaded = await load_for(ctx, link)
    if not loaded:
        return
    player = await get_player(ctx, dest)
    if not player:
        return
    n = len(loaded.tracks)
    if player.add(loaded.tracks):
        msg = f"{'🔀' if loaded.shuffle else '🎵'} Playing {n} track(s) from {loaded.name}."
    else:
        msg = f"🔀 Mixed {n} track(s) from {loaded.name} into the queue."
    await ctx.reply(f"{msg}\n{loaded.note}".strip())


def queue_lines(player: GuildPlayer, limit: int | None) -> list[str]:
    lines = []
    cur = player.index
    start = cur if limit else 0
    end = len(player.tracks) if limit is None else min(len(player.tracks), cur + 1 + limit)
    for i in range(start, end):
        t = player.tracks[i]
        mark = "▶ " if i == cur else ""
        lines.append(f"{mark}{i + 1}. {t.label}")
    return lines


def now_playing_embed(track: Track) -> discord.Embed:
    title = track.title.replace("[", "(").replace("]", ")")
    who = f"{track.artist} - " if track.artist else ""
    link = f"[{title}]({track.url})" if track.url else title
    embed = discord.Embed(
        title="▶️ Now playing",
        description=f"{who}{link} at: {fmt_time(track.duration)}",
        color=discord.Color.blurple(),
    )
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


def cast_skip_vote(player: GuildPlayer, user: discord.abc.User) -> tuple[bool, str]:
    """Register a skip vote. -> (skipped, message). Bots are never counted as listeners."""
    channel = player.vc.channel
    listeners = {m.id for m in channel.members if not m.bot}
    player.skip_votes &= listeners  # votes from people who left no longer count
    if user.id in player.skip_votes:
        return False, "You already voted to skip this track."
    player.skip_votes.add(user.id)
    votes = len(player.skip_votes)
    need = max(1, math.ceil(humans_in(channel) * SKIP_RATIO))
    if votes >= need:
        player.skip()
        return True, ""
    return False, f"⏭️ Skip vote {votes}/{need} - {need - votes} more needed."


def help_text() -> str:
    p = PREFIX
    return (
        f"`{p}play <link>` - add a Spotify / YouTube / YouTube Music track or playlist to the mix (starts playing if idle)\n"
        f"`{p}play` - resume\n"
        f"`{p}pause` / `{p}stop` - pause\n"
        f"`{p}toggle` - play/pause\n"
        f"`{p}skip` - vote to skip (needs {int(SKIP_RATIO * 100)}% of listeners)\n"
        f"`{p}previous` - previous track\n"
        f"`{p}queue <link>` - add to the mix\n"
        f"`{p}queue` / `{p}list` - next 10 tracks\n"
        f"`{p}queue full` / `{p}queue all` - whole queue as a CSV file\n"
        f"`{p}remove <number>` - remove a track (numbers from `{p}queue`)\n"
        f"`{p}clear` - empty the queue and leave\n"
        f"`{p}np` / `{p}cp` / `{p}link` - current track with time and link"
    )


class PlayerView(discord.ui.View):
    """Buttons under the 'now playing' message."""

    def __init__(self, player: GuildPlayer):
        super().__init__(timeout=None)
        self.player = player

    async def _check(self, interaction: discord.Interaction, need_voice: bool = True) -> bool:
        vc = self.player.vc
        if players.get(self.player.guild.id) is not self.player or not vc:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return False
        if need_voice:
            voice = getattr(interaction.user, "voice", None)
            if not voice or voice.channel != vc.channel:
                await interaction.response.send_message("Join my voice channel first.", ephemeral=True)
                return False
        return True

    @discord.ui.button(label="Play/Pause", emoji="⏯️", style=discord.ButtonStyle.primary)
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        vc = self.player.vc
        if vc.is_paused():
            vc.resume()
        elif vc.is_playing():
            vc.pause()
        await interaction.response.defer()

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        skipped, msg = cast_skip_vote(self.player, interaction.user)
        if skipped:
            await interaction.response.defer()
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="Next 3", emoji="📜", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction, need_voice=False):
            return
        lines = queue_lines(self.player, 3)
        if len(lines) <= 1:
            lines.append("Nothing up next.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="Help", emoji="❓", style=discord.ButtonStyle.secondary)
    async def help_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(help_text(), ephemeral=True)


# --------------------------------------------------------------------------- commands
@bot.command()
@commands.guild_only()
async def play(ctx: commands.Context, *, link: str = None):
    """$play - resume. $play <link> - add a track/playlist to the mix."""
    if link:
        return await enqueue(ctx, link)
    player = await controller(ctx)
    if not player:
        return
    if player.vc.is_paused():
        player.vc.resume()
        await ack(ctx, "▶️")
    else:
        await ctx.reply(f"Already playing. Use `{PREFIX}play <link>` to add something.")


@bot.command()
@commands.guild_only()
async def toggle(ctx: commands.Context):
    """$toggle - play/pause."""
    player = await controller(ctx)
    if not player:
        return
    vc = player.vc
    if vc.is_paused():
        vc.resume()
        await ack(ctx, "▶️")
    elif vc.is_playing():
        vc.pause()
        await ack(ctx, "⏸️")


@bot.command(aliases=["stop"])
@commands.guild_only()
async def pause(ctx: commands.Context):
    """$pause / $stop - pause playback."""
    player = await controller(ctx)
    if player and player.vc.is_playing():
        player.vc.pause()
        await ack(ctx, "⏸️")


@bot.command(aliases=["leave", "dc"])
@commands.guild_only()
async def clear(ctx: commands.Context):
    """$clear - empty the whole queue (the bot leaves the channel)."""
    player = await controller(ctx)
    if player:
        await player.shutdown()
        await ctx.reply("🗑️ Queue cleared.")


@bot.command(name="queue", aliases=["list", "q"])
@commands.guild_only()
async def queue_cmd(ctx: commands.Context, *, arg: str = None):
    """$queue <link> - add to mix. $queue - next 10. $queue full / all - whole queue as a file."""
    player = players.get(ctx.guild.id)
    word = (arg or "").strip().lower()

    if arg and word not in ("full", "all"):
        return await enqueue(ctx, arg)

    if not player or not player.tracks:
        return await ctx.reply("Nothing queued.")

    if word in ("full", "all"):
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\n")
        w.writerow(["nr", "artist", "name", "link"])
        for i, t in enumerate(player.tracks, 1):
            w.writerow([i, t.artist, t.title, t.url or ""])
        data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # BOM so Excel reads UTF-8 correctly
        return await ctx.reply(
            f"📄 {len(player.tracks)} tracks - currently playing: #{player.index + 1}",
            file=discord.File(data, filename="queue.csv"),
        )

    lines = queue_lines(player, 10)
    remaining = len(player.tracks) - (player.index + 11)
    if remaining > 0:
        lines.append(f"...and {remaining} more (`{PREFIX}queue full` for everything)")
    await ctx.reply("\n".join(lines))


@bot.command(aliases=["next"])
@commands.guild_only()
async def skip(ctx: commands.Context):
    """$skip - vote to skip; skips once enough listeners agree."""
    player = await controller(ctx)
    if not player:
        return
    skipped, msg = cast_skip_vote(player, ctx.author)
    if skipped:
        await ack(ctx, "⏭️")
    else:
        await ctx.reply(msg)


@bot.command(aliases=["prev", "back"])
@commands.guild_only()
async def previous(ctx: commands.Context):
    """$previous - go back one track (restarts the first track if you're at the start)."""
    player = await controller(ctx)
    if player:
        player.previous()
        await ack(ctx, "⏮️")


@bot.command(aliases=["rm"])
@commands.guild_only()
async def remove(ctx: commands.Context, number: int):
    """$remove <number> - remove that track (numbers as shown by $queue)."""
    player = await controller(ctx)
    if not player:
        return
    if not 1 <= number <= len(player.tracks):
        return await ctx.reply(f"Pick a number between 1 and {len(player.tracks)}.")
    track = player.remove(number - 1)
    await ctx.reply(f"🗑️ Removed **{track.label}**.")


@bot.command(aliases=["np", "nowplaying", "cp", "link"])
@commands.guild_only()
async def now(ctx: commands.Context):
    """$np / $cp / $link - what's playing, with time and link."""
    player = players.get(ctx.guild.id)
    track = player.current if player else None
    if not track:
        return await ctx.reply("Nothing is playing.")
    src = player.source
    elapsed = fmt_time(src.elapsed) if src else "00:00"
    paused = " ⏸️" if player.vc and player.vc.is_paused() else ""
    lines = [f"🎶 **{track.label}**", f"`{elapsed} / {fmt_time(track.duration)}`{paused}"]
    if track.url:
        lines.append(f"<{track.url}>")  # <> keeps Discord from embedding it
    lines.append(f"{len(player.upcoming)} more in queue")
    await ctx.reply("\n".join(lines))


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    await ctx.reply(help_text())

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
    if isinstance(err, (commands.MissingRequiredArgument, commands.BadArgument)):
        return await ctx.reply(f"Usage: `{PREFIX}{ctx.command.name} <{'number' if ctx.command.name == 'remove' else 'link'}>`")
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