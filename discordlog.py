"""Mirror important logs into a Discord channel. Enabled by setting LOG_CHANNEL_ID.

What is sent:
  * every WARNING / ERROR / CRITICAL record from any logger (timeouts, ffmpeg problems, failed lookups...)
  * "request" lines, i.e. records logged with discordlog.request(log, ...): lyrics, YouTube, r34
  * anything logged with a traceback (log.exception, crashes) is sent as a whole code block

Line format:
  `[2026-10-23 ; 14:05:09]  -  ⚠️@🎤 text`
        date       time        level module

Logs can be produced from any thread (ffmpeg's `after` callback, yt-dlp in to_thread), so the handler
only appends to a deque; a task on the bot's loop does the actual sending.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import traceback
from collections import deque
from datetime import datetime

import discord

LIMIT = 2000
REQUEST_EMOJI = "📡"

# first matching prefix wins
MODULE_EMOJI = (
    ("tardbot.music.lyrics", "🎤"),
    ("tardbot.music.sources", "▶️"),
    ("tardbot.music", "🎵"),
    ("tardbot.r34", "🔞"),
    ("tardbot.general", "🧰"),
    ("discord", "🤖"),
    ("asyncio", "⚙️"),
    ("tardbot", "🤖"),
)


def request(logger: logging.Logger, msg: str, *args) -> None:
    """Log an outgoing request (shown in the log channel even though it is only INFO)."""
    logger.info(msg, *args, extra={"request": True})


def _module_emoji(name: str) -> str:
    for prefix, emoji in MODULE_EMOJI:
        if name == prefix or name.startswith(prefix + "."):
            return emoji
    return "🤖"


def _level_emoji(rec: logging.LogRecord, is_request: bool) -> str:
    if rec.levelno >= logging.CRITICAL:
        return "💀"
    if rec.levelno >= logging.ERROR:
        return "❌"
    if rec.levelno >= logging.WARNING:
        return "⚠️"
    return REQUEST_EMOJI if is_request else "ℹ️"


class DiscordLogHandler(logging.Handler):
    def __init__(self, channel_id: int) -> None:
        super().__init__(logging.INFO)
        self.channel_id = channel_id
        self.pending: deque[tuple[str, str | None]] = deque(maxlen=300)  # (line, traceback | None)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.wake: asyncio.Event | None = None
        self.bot: discord.Client | None = None
        self.channel = None
        self.task: asyncio.Task | None = None

    # ---- logging side (any thread)
    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith("discord.http"):  # never log about our own sending: would loop
                return
            is_request = bool(getattr(record, "request", False))
            if record.levelno < logging.WARNING and not is_request:
                return
            self.pending.append(self._build(record, is_request))
            loop, wake = self.loop, self.wake
            if loop and wake and not loop.is_closed():
                loop.call_soon_threadsafe(wake.set)
        except Exception:  # noqa: BLE001
            self.handleError(record)

    @staticmethod
    def _build(rec: logging.LogRecord, is_request: bool) -> tuple[str, str | None]:
        stamp = datetime.fromtimestamp(rec.created).strftime("%Y-%m-%d ; %H:%M:%S")
        msg = " ".join(rec.getMessage().split()).replace("`", "'")
        if len(msg) > 400:
            msg = msg[:397] + "..."
        line = f"`[{stamp}]  -  {_level_emoji(rec, is_request)}@{_module_emoji(rec.name)} {msg}`"
        tb = None
        if rec.exc_info and rec.exc_info[0] is not None:
            tb = "".join(traceback.format_exception(*rec.exc_info)).replace("```", "'''").strip()
        return line, tb

    # ---- sending side (bot loop)
    def start(self, bot: discord.Client) -> None:
        """Call from setup_hook (needs the running loop)."""
        self.bot = bot
        self.loop = asyncio.get_running_loop()
        self.wake = asyncio.Event()
        if self.pending:
            self.wake.set()
        self.task = self.loop.create_task(self._run())

    async def _resolve_channel(self):
        if self.channel is None:
            await self.bot.wait_until_ready()
            self.channel = self.bot.get_channel(self.channel_id) or await self.bot.fetch_channel(self.channel_id)
        return self.channel

    async def _run(self) -> None:
        try:
            await self._resolve_channel()
        except Exception as e:  # noqa: BLE001
            print(f"discordlog: cannot use LOG_CHANNEL_ID={self.channel_id}: {e}", file=sys.stderr)
            self.pending.clear()
            return
        while True:
            await self.wake.wait()
            self.wake.clear()
            await self._drain()
            await asyncio.sleep(1)  # gentle on rate limits; lines arriving meanwhile get batched

    async def _drain(self) -> None:
        batch = ""
        while self.pending:
            line, tb = self.pending.popleft()
            if tb is None:
                if batch and len(batch) + len(line) + 1 > 1900:
                    await self._send(batch)
                    batch = ""
                batch += line + "\n"
            else:
                if batch:
                    await self._send(batch)
                    batch = ""
                await self._send_crash(line, tb)
        if batch:
            await self._send(batch)

    async def _send(self, text: str, **kw) -> None:
        try:
            await self.channel.send(text[:LIMIT], **kw)
        except Exception as e:  # noqa: BLE001 - logging must never break the bot
            print(f"discordlog: send failed: {e}", file=sys.stderr)

    async def _send_crash(self, line: str, tb: str) -> None:
        box = f"{line}\n```py\n{tb}\n```"
        if len(box) <= LIMIT:
            await self._send(box)
        else:  # too long for one message: the whole traceback goes as a file
            await self._send(line, file=discord.File(io.BytesIO(tb.encode()), "traceback.txt"))

    async def flush_now(self, timeout: float = 10) -> None:
        """Send whatever is queued right now (use before the process exits)."""
        try:
            if self.bot is None:
                return
            await asyncio.wait_for(self._resolve_channel(), timeout)
            await asyncio.wait_for(self._drain(), timeout)
        except Exception as e:  # noqa: BLE001
            print(f"discordlog: flush failed: {e}", file=sys.stderr)


def install() -> DiscordLogHandler | None:
    """Attach the handler to the root logger if LOG_CHANNEL_ID is set."""
    raw = os.getenv("LOG_CHANNEL_ID", "").strip()
    if not raw.isdigit():
        return None
    handler = DiscordLogHandler(int(raw))
    logging.getLogger().addHandler(handler)
    return handler