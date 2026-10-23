from __future__ import annotations

from discord.ext import commands


class General(commands.Cog):
    """Small everyday commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command()
    async def ping(self, ctx: commands.Context):
        """Gateway latency."""
        await ctx.reply(f"🏓 {self.bot.latency * 1000:.0f} ms")


def help_text(prefix: str) -> str:
    return f"`{prefix}ping` - latency"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
    sections = getattr(bot, "help_sections", None)
    if sections is not None:
        sections["General"] = lambda: help_text(str(bot.command_prefix))
