# tardBot

Modular Discord bot. `index.py` starts the bot and loads every module folder next to it.

```
tardBot/
├── index.py          entry point (bot, module loader, $help, error handler)
├── music/            Spotify/YouTube queue, votes, now-playing card, lyrics
│   ├── cog.py  sources.py  lyrics.py  card.py
├── general/          template module ($ping)
├── r34/              $r34 <tags> - post count on rule34.xxx
├── vcmanager/        $join <vc name>, $vc <name> <max> (temp voice channels)
├── vcshared.py       voice-limit helpers + tardbot_state.json (shared by music and vcmanager)
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

## Voice limits
- music: while the bot sits in a voice channel with a user limit, that limit is raised by 1 (and lowered again when it leaves).
- `$join <vc name>`: moves you into that channel; if it is full its limit goes up by 1. Not in voice? Only the limit goes up.
- `$vc <name> <max>`: temporary channel, deleted when everyone has left (0 = no limit).
- Bot permissions: Manage Channels, Move Members. State lives in `tardbot_state.json` (`TARDBOT_STATE` to relocate; add it to .gitignore).
- Settings: `TEMP_VC_GRACE_SECONDS`, `TEMP_VC_EMPTY_SECONDS`, `TEMP_VC_PER_USER`, `TEMP_VC_PER_GUILD`, `TEMP_VC_CATEGORY_ID`, `VC_COMMAND_COOLDOWN`.