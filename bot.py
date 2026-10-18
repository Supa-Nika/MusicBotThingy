from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
import os
import random
import shlex
from collections import deque

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

import sources
from sources import SourceError, Track

try:
    import card  # needs pillow + numpy; the bot still works (with a plain embed) without them
except ImportError as _e:  # pragma: no cover
    card = None
    _card_error = _e

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("musicbot")
if card is None:
    log.warning("Now-playing card disabled (%s) - pip install pillow numpy", _card_error)

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
# Loudness normalisation so every song plays at a similar volume. Set AUDIO_FILTER= (empty) to disable,
# or e.g. AUDIO_FILTER=dynaudnorm=f=250:g=15 for a lighter-weight alternative.
AUDIO_FILTER = os.getenv("AUDIO_FILTER", "loudnorm=I=-16:TP=-1.5:LRA=11")
FFMPEG_OPTIONS = "-vn" + (f" -af {shlex.quote(AUDIO_FILTER)}" if AUDIO_FILTER else "")
CARD_REFRESH = float(os.getenv("CARD_REFRESH_SECONDS", "5"))

# Share of listeners (bots excluded) that must vote for an action.
VOTE_RULES = {
    "skip": float(os.getenv("VOTE_SKIP", "0.33")),
    "previous": float(os.getenv("VOTE_PREVIOUS", "0.33")),
    "clear": float(os.getenv("VOTE_CLEAR", "0.50")),
}
VOTE_LABELS = {"skip": "Skip", "previous": "Previous", "clear": "Clear"}
OVERRIDE_ROLE = os.getenv("OVERRIDE_ROLE", "slave master")  # holders skip all votes

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


def is_master(user) -> bool:
    name = OVERRIDE_ROLE.lower()
    return any(r.name.lower() == name for r in getattr(user, "roles", ()))


def votes_needed(humans: int, ratio: float) -> int:
    return max(1, math.ceil(humans * ratio - 1e-9))


class TimedSource(discord.FFmpegPCMAudio):
    """FFmpegPCMAudio that counts delivered 20 ms frames (= elapsed time) and keeps the latest audio."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.frames = 0
        self.recent: deque[bytes] = deque(maxlen=3)  # ~60 ms of PCM for the visualizer

    def read(self) -> bytes:
        data = super().read()
        if data:
            self.frames += 1
            self.recent.append(data)
        return data

    @property
    def elapsed(self) -> float:
        return self.frames * 0.02

    def pcm(self) -> bytes:
        return b"".join(self.recent)


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
        self.votes: dict[str, set[int]] = {k: set() for k in VOTE_RULES}
        # now-playing panel
        self.panel: discord.Message | None = None
        self.panel_track: Track | None = None
        self.theme = None
        self.refresh_task: asyncio.Task | None = None
        self.refresh_event = asyncio.Event()

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

    def poke(self) -> None:
        """Ask the card to refresh soon (votes, pause, queue changes)."""
        self.refresh_event.set()

    # ---- queue control
    def add(self, new: list[Track]) -> tuple[bool, int, int]:
        """Add tracks, skipping duplicates. -> (started_playback, added, duplicates_skipped)"""
        fresh, skipped = sources.dedupe(self.tracks, new)
        if not fresh:
            return False, 0, skipped
        if not self.tracks:
            self.tracks = fresh
            self.index = 0
            if not self.task or self.task.done():
                self.task = asyncio.create_task(self._run())
            return True, len(fresh), skipped
        head = self.tracks[: self.index + 1]
        self.tracks = head + sources.interleave(self.tracks[self.index + 1:], fresh, SPREAD)
        self.poke()
        return False, len(fresh), skipped

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
        self.poke()
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
            for voters in self.votes.values():
                voters.clear()
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

            self.source = TimedSource(track.stream_url, before_options=FFMPEG_BEFORE, options=FFMPEG_OPTIONS)
            vc.play(self.source, after=after)
            await self.announce(track)
            await signal.wait()
            self._advance()

    # ---- now-playing panel
    async def _make_theme(self, track: Track):
        data = None
        if track.thumbnail:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(track.thumbnail, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status == 200:
                            data = await r.read()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
        return await asyncio.to_thread(card.build_theme, data)

    def _card_data(self, track: Track):
        vc, src = self.vc, self.source
        paused = bool(vc and vc.is_paused())
        humans = max(humans_in(vc.channel), 1) if vc and vc.channel else 1
        votes = []
        for action, voters in self.votes.items():
            if voters:
                ratio = VOTE_RULES[action]
                votes.append((VOTE_LABELS[action], len(voters), votes_needed(humans, ratio),
                              len(voters) / humans * 100, ratio * 100))
        elapsed = src.elapsed if src else 0.0
        return card.CardData(
            artist=track.artist,
            title=track.title,
            elapsed=elapsed,
            duration=track.duration,
            elapsed_text=fmt_time(elapsed),
            total_text=fmt_time(track.duration),
            paused=paused,
            pcm=src.pcm() if src and not paused else b"",
            upcoming=[t.label for t in self.upcoming[:3]],
            votes=votes,
        )

    def _signature(self, track: Track):
        d = self._card_data(track)
        return (d.paused, None if d.paused else int(d.elapsed), tuple(d.votes), tuple(d.upcoming))

    async def _panel_payload(self, track: Track):
        embed = now_playing_embed(track)
        file = None
        if card and self.theme:
            try:
                png = await asyncio.to_thread(card.render, self.theme, self._card_data(track))
                file = discord.File(io.BytesIO(png), filename="card.png")
                embed.set_image(url="attachment://card.png")
            except Exception:  # noqa: BLE001
                log.exception("card render failed")
        if file is None and track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        return embed, file

    async def announce(self, track: Track) -> None:
        await self._drop_panel()
        self.theme = None
        if card:
            try:
                self.theme = await self._make_theme(track)
            except Exception:  # noqa: BLE001
                log.exception("could not build card theme")
        try:
            embed, file = await self._panel_payload(track)
            kwargs = {"file": file} if file else {}
            self.panel = await self.text.send(embed=embed, view=PlayerView(self), **kwargs)
            self.panel_track = track
        except discord.HTTPException:
            self.panel = self.panel_track = None
            return
        if card and self.theme:
            self.refresh_task = asyncio.create_task(self._refresh_loop(self.panel, track))

    async def _refresh_loop(self, panel: discord.Message, track: Track) -> None:
        last = None
        while self.panel is panel:
            try:
                await asyncio.wait_for(self.refresh_event.wait(), CARD_REFRESH)
            except asyncio.TimeoutError:
                pass
            self.refresh_event.clear()
            if self.panel is not panel:
                return
            if self.current is not track:  # between tracks
                continue
            sig = self._signature(track)
            if sig == last:
                continue
            last = sig
            embed, file = await self._panel_payload(track)
            kwargs = {"attachments": [file]} if file else {}
            try:
                await panel.edit(embed=embed, **kwargs)
            except discord.NotFound:
                return
            except discord.HTTPException as e:
                log.warning("card update failed: %s", e)
            await asyncio.sleep(1.5)  # keep clear of edit rate limits

    async def _drop_panel(self) -> None:
        """Turn the previous panel into a compact 'played' line without card or buttons."""
        panel, track = self.panel, self.panel_track
        self.panel = self.panel_track = None
        if self.refresh_task and self.refresh_task is not asyncio.current_task() and not self.refresh_task.done():
            self.refresh_task.cancel()
        if panel and track:
            try:
                await panel.edit(embed=now_playing_embed(track, "🎵 Played", thumb=True), attachments=[], view=None)
            except discord.HTTPException:
                pass

    # ---- prefetch
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
    started, added, skipped = player.add(loaded.tracks)
    dup = f" ({skipped} duplicate(s) skipped)" if skipped else ""
    if started:
        msg = f"{'🔀' if loaded.shuffle else '🎵'} Playing {added} track(s) from {loaded.name}.{dup}"
    elif added:
        msg = f"🔀 Mixed {added} new track(s) from {loaded.name} into the queue.{dup}"
    else:
        msg = f"ℹ️ Everything from {loaded.name} is already in the queue."
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


def now_playing_embed(track: Track, title: str = "▶️ Now playing", thumb: bool = False) -> discord.Embed:
    name = track.title.replace("[", "(").replace("]", ")")
    who = f"{track.artist} - " if track.artist else ""
    link = f"[{name}]({track.url})" if track.url else name
    embed = discord.Embed(
        title=title,
        description=f"{who}{link} at: {fmt_time(track.duration)}",
        color=discord.Color.blurple(),
    )
    if thumb and track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


async def run_vote(player: GuildPlayer, action: str, user: discord.abc.User) -> str | None:
    """Count a vote for skip / previous / clear.

    Returns None when the action was carried out, otherwise a message for the voter.
    Members with the override role act immediately. Bots are never counted as listeners.
    """
    channel = player.vc.channel
    ratio = VOTE_RULES[action]
    if not is_master(user):
        humans = max(humans_in(channel), 1)
        voters = player.votes[action]
        voters &= {m.id for m in channel.members if not m.bot}  # people who left no longer count
        if user.id in voters:
            return f"You already voted to {action}."
        voters.add(user.id)
        need = votes_needed(humans, ratio)
        if len(voters) < need:
            player.poke()
            return (f"🗳️ {VOTE_LABELS[action]} vote {len(voters)}/{need} "
                    f"({round(len(voters) / humans * 100)}% of listeners, {round(ratio * 100)}% needed).")
    for voters in player.votes.values():
        voters.clear()
    if action == "skip":
        player.skip()
    elif action == "previous":
        player.previous()
    elif action == "clear":
        await player.shutdown()
    return None


def help_text() -> str:
    p = PREFIX
    pct = {k: round(v * 100) for k, v in VOTE_RULES.items()}
    return (
        f"`{p}play <link>` - add a Spotify / YouTube / YouTube Music track or playlist to the mix (starts playing if idle)\n"
        f"`{p}play` - resume\n"
        f"`{p}pause` / `{p}stop` - pause\n"
        f"`{p}toggle` - play/pause\n"
        f"`{p}skip` - vote to skip ({pct['skip']}% of listeners)\n"
        f"`{p}previous` - vote for the previous track ({pct['previous']}%)\n"
        f"`{p}clear` - vote to empty the queue and leave ({pct['clear']}%)\n"
        f"`{p}queue <link>` - add to the mix\n"
        f"`{p}queue` / `{p}list` - next 10 tracks\n"
        f"`{p}queue full` / `{p}queue all` - whole queue as a CSV file\n"
        f"`{p}remove <number>` - remove a track (numbers from `{p}queue`)\n"
        f"`{p}np` / `{p}cp` / `{p}link` - current track with time and link\n"
        f"Members with the **{OVERRIDE_ROLE}** role skip all votes."
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

    async def _vote(self, interaction: discord.Interaction, action: str) -> None:
        if not await self._check(interaction):
            return
        msg = await run_vote(self.player, action, interaction.user)
        if msg:
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Prev", emoji="⏮️", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "previous")

    @discord.ui.button(label="Play/Pause", emoji="⏯️", style=discord.ButtonStyle.primary)
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check(interaction):
            return
        vc = self.player.vc
        if vc.is_paused():
            vc.resume()
        elif vc.is_playing():
            vc.pause()
        self.player.poke()
        await interaction.response.defer()

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "skip")

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
        player.poke()
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
    player.poke()


@bot.command(aliases=["stop"])
@commands.guild_only()
async def pause(ctx: commands.Context):
    """$pause / $stop - pause playback."""
    player = await controller(ctx)
    if player and player.vc.is_playing():
        player.vc.pause()
        player.poke()
        await ack(ctx, "⏸️")


@bot.command(aliases=["leave", "dc"])
@commands.guild_only()
async def clear(ctx: commands.Context):
    """$clear - vote to empty the whole queue (the bot leaves the channel)."""
    player = await controller(ctx)
    if not player:
        return
    msg = await run_vote(player, "clear", ctx.author)
    await ctx.reply(msg or "🗑️ Queue cleared.")


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
    msg = await run_vote(player, "skip", ctx.author)
    if msg:
        await ctx.reply(msg)
    else:
        await ack(ctx, "⏭️")


@bot.command(aliases=["prev", "back"])
@commands.guild_only()
async def previous(ctx: commands.Context):
    """$previous - vote to go back one track."""
    player = await controller(ctx)
    if not player:
        return
    msg = await run_vote(player, "previous", ctx.author)
    if msg:
        await ctx.reply(msg)
    else:
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
