# tardBot

Modular Discord bot. `index.py` starts the bot and loads every module folder next to it.

```
tardBot/
├── index.py          entry point (bot, module loader, $help, error handler)
├── music/            Spotify/YouTube queue, votes, now-playing card, lyrics
│   ├── cog.py  sources.py  lyrics.py  card.py
├── general/          template module ($ping)
├── r34/              $r34 <tags> - post count on rule34.xxx
├── tardbot.nix       NixOS module (services.tardbot.*)
├── tardbot.service   plain systemd unit
├── flake.nix  requirements.txt  .env.example
```

## Adding a module
1. Copy `general/` to `yourmodule/`.
2. Add commands; keep `async def setup(bot)` (optionally `teardown`).
3. Register a help section: `bot.help_sections["Name"] = lambda: "..."`.
4. Restart. Use `TARDBOT_MODULES=music,yourmodule` to pick modules explicitly.

## Run
```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # set DISCORD_TOKEN
.venv/bin/python index.py
```
