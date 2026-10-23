"""tardBot entry point: creates the bot and loads every module (music, general, ...).

A module is a package next to this file whose __init__.py exposes `async def setup(bot)`
(and optionally `teardown(bot)`). Drop a new folder in and it is picked up on the next start.

  TARDBOT_MODULES=music,general   load only these (default: every module found)
  PREFIX=$                        command prefix
  LOG_CHANNEL_ID=123...           mirror warnings, errors, crashes and requests into this channel
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable

import discord
from discord.ext import commands
from dotenv import load_dotenv

import discordlog

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
load_dotenv()  # also honour a .env in the working directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tardbot")
log_handler = discordlog.install()  # None unless LOG_CHANNEL_ID is set

PREFIX = os.getenv("PREFIX", "$")


class TardBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()  # guilds + voice_states
        intents.message_content = True       # privileged: enable it in the developer portal
        super().__init__(
            command_prefix=PREFIX,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # modules register a section here: bot.help_sections["Music"] = lambda: "text"
        self.help_sections: dict[str, Callable[[], str]] = {}

    async def setup_hook(self) -> None:
        if log_handler:
            log_handler.start(self)
        for name in discover_modules():
            try:
                await self.load_extension(name)
                log.info("module loaded: %s", name)
            except Exception:  # noqa: BLE001 - one broken module must not take the others down
                log.exception("module %s failed to load - skipping it", name)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s guilds)", self.user, len(self.guilds))


def discover_modules() -> list[str]:
    wanted = os.getenv("TARDBOT_MODULES", "").replace(" ", "")
    if wanted:
        return [m for m in wanted.split(",") if m]
    return sorted(p.name for p in ROOT.iterdir()
                  if p.is_dir() and (p / "__init__.py").exists() and not p.name.startswith(("_", ".")))


bot = TardBot()


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    if not bot.help_sections:
        return await ctx.reply("No modules loaded.")
    parts = [f"**{name}**\n{text()}" for name, text in sorted(bot.help_sections.items())]
    await ctx.reply("\n\n".join(parts))


@bot.event
async def on_command_error(ctx: commands.Context, err: Exception):
    if isinstance(err, commands.CommandNotFound):
        return
    if isinstance(err, (commands.MissingRequiredArgument, commands.BadArgument)):
        usage = f"{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}".strip()
        return await ctx.reply(f"Usage: `{usage}`")
    if isinstance(err, commands.NoPrivateMessage):
        return await ctx.reply("Use me in a server.")
    log.error("command %s failed", ctx.command, exc_info=err)
    await ctx.reply("💥 Something went wrong - check the bot's log.")


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN in .env (see .env.example)")
    async with bot:
        try:
            await bot.start(token)
        except Exception:
            log.critical("bot crashed", exc_info=True)
            if log_handler:
                await log_handler.flush_now()
            raise


if __name__ == "__main__":
    asyncio.run(main())