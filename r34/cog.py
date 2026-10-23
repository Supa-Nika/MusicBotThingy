"""$r34 <tags> -> number of posts on rule34.xxx for that tag search (nothing else).

Uses the site's dapi, which reports the total in `<posts count="N">`. rule34.xxx requires API
credentials for it: R34_USER_ID and R34_API_KEY (rule34.xxx > My Account > Options > API Access Credentials).
"""
from __future__ import annotations

import logging
import os
import re

import aiohttp
from discord.ext import commands

log = logging.getLogger("tardbot.r34")

API = "https://api.rule34.xxx/index.php"
TIMEOUT = aiohttp.ClientTimeout(total=15)
UA = "tardbot (private discord bot)"
_COUNT = re.compile(r'<posts[^>]*\bcount="(\d+)"')


class R34Error(Exception):
    pass


async def post_count(session: aiohttp.ClientSession, tags: str) -> int:
    user, key = os.getenv("R34_USER_ID"), os.getenv("R34_API_KEY")
    if not (user and key):
        raise R34Error("R34_USER_ID / R34_API_KEY not set")
    params = {"page": "dapi", "s": "post", "q": "index", "limit": 1, "pid": 0,
              "tags": tags, "user_id": user, "api_key": key}
    async with session.get(API, params=params) as r:
        text = await r.text()
        if r.status != 200:
            raise R34Error(f"HTTP {r.status}")
    m = _COUNT.search(text)
    if not m:  # the API answers 200 with a plain message on bad credentials / errors
        raise R34Error(text.strip()[:120] or "empty response")
    return int(m.group(1))


class R34(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="r34")
    async def r34(self, ctx: commands.Context, *, tags: str):
        async with ctx.typing():
            try:
                async with aiohttp.ClientSession(headers={"User-Agent": UA}, timeout=TIMEOUT) as s:
                    count = await post_count(s, tags.strip())
            except (R34Error, aiohttp.ClientError, TimeoutError) as e:
                log.warning("r34 lookup failed for %r: %s", tags, e)
                return await ctx.reply("❌ lookup failed - check the bot's log.")
        await ctx.reply(str(count))


def help_text(prefix: str) -> str:
    return f"`{prefix}r34 <tags>` - number of posts for a tag search (site syntax: `tag1 -tag2 ~tag3 rating:safe`)"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(R34(bot))
    sections = getattr(bot, "help_sections", None)
    if sections is not None:
        sections["R34"] = lambda: help_text(str(bot.command_prefix))
