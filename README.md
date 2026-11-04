# Scrappie - Discord Bot

This repository contains a Discord bot (and a small Flask health endpoint) with these primary features:

- **Music (voice)**
	- `/play <query|url>` — Play a song or add to the guild queue. Supports playlists.
	- `/queue` — View the current music queue.
	- `/skip` — Skip the currently playing track.
	- `/stop` — Stop playback and clear the queue.
	- `/leave` — Disconnect the bot from voice.
	- `/shuffle` — Shuffle the current guild queue (randomizes upcoming tracks).

- **Trivia / Games**
	- `/trivia` — Interactive trivia with category/difficulty setup and time-limited buttons.
	- `/fact` — Return a random fact.
	- `/dadjoke` — Return a random dad joke.
	- `/coin` — Flip a coin (Heads/Tails).
	- `/number <min> <max>` — Generate a random integer in the specified range.

- **Utilities**
	- `/weather <city>` — Fetches current weather and a short forecast (uses `python_weather`).
	- `/ping` — Returns latency, uptime, guild/user counts.
	- `/help` — Shows a command summary.

- **Destiny 2** (needs `BUNGIE_API_KEY`)
	- `/item <name>` — Weapon or armor lookup with autocomplete.
	- `/weekly` — Private preview of the weekly raid and dungeon post.
	- Automatic weekly post at Tuesday 17:00 UTC (11am CST, 12pm CDT) with the featured raids and dungeons, modifiers, drops, set bonuses and a labelled one-week prediction. Dungeons and the forecast are labelled predicted when Bungie has not confirmed them. Needs `WEEKLY_RESET_CHANNEL_ID`.

- **Web health**
	- A small Flask app exposes health endpoints when running:
		- `GET /` — Basic status JSON
		- `GET /health` — Health + uptime

- **Other features**
	- Rate limiting per user (configurable via `Config.MAX_REQUESTS_PER_MINUTE` in `scrappie/config.py`).
	- Logging to `logs/bot.log` and console.
	- Optional voice support which depends on `yt-dlp`, Opus library availability and FFmpeg.

## Requirements

- Python 3.11+ (`yt-dlp` requires it; production runs 3.13)
- FFmpeg installed and available on PATH (for voice playback)
- A Discord bot token
- See `requirements.txt` for Python dependencies.

## Environment

- `DISCORD_TOKEN` (required) — Bot token used to connect to Discord.
- `PORT` (optional) — Port for the Flask health server (default: 5000).
- `BUNGIE_API_KEY` (optional) — Enables `/item`, `/weekly` and the weekly post. Get one at bungie.net/en/Application.
- `WEEKLY_RESET_CHANNEL_ID` (optional) — Channel for the automatic weekly post. Without it the commands still work and nothing is posted.
- `DESTINY_MEMBERSHIP_TYPE` and `DESTINY_MEMBERSHIP_ID` (optional, set both) — The account whose public activity data confirms the week's dungeons. Without them the post shows predicted dungeons, and `/item` and the raids still work. Set both on the host, because `.env` is not deployed.

## Quick start (development)

Open PowerShell and run:

```powershell
# install deps
python -m pip install -r requirements.txt

# set token for current session (Windows PowerShell)
$env:DISCORD_TOKEN = "your_bot_token_here"

# run the bot
python main.py
```

The bot will start a Flask health server on `0.0.0.0:$PORT` (default 5000) and connect to Discord.

## Voice / Music notes

- Voice support requires:
	- `yt-dlp` Python package
	- A working Opus library (libopus) — the bot tries common library names, then discord.py's default; on Windows that is the DLL bundled with discord.py
	- FFmpeg available on PATH
- If `yt-dlp` or Opus is unavailable, music commands are not registered and the bot logs why. If FFmpeg is missing, playback fails at runtime.

## Files of interest

- `main.py` — Entry point. Starts Flask and the bot; imported as a module it only exposes the WSGI `application`.
- `scrappie/` — Application package:
	- `config.py` — Tunable values and API URLs.
	- `log.py` — Logging setup.
	- `web.py` — Flask health server.
	- `state.py` — Shared runtime state and rate limit tracking.
	- `bot.py` — The Discord bot object.
	- `utils.py` — Embed, HTTP, formatting and rate limit helpers.
	- `voice.py` — Opus and yt-dlp availability.
	- `music.py`, `trivia.py`, `weather.py`, `general.py` — Slash commands.
	- `destiny.py` — Destiny commands and the weekly post loop.
	- `data/` — Generated Bungie data (raids, dungeons and items).
	- `events.py` — Discord events and the status rotation task.
	- `lifecycle.py` — Command registration order, startup and shutdown.
- `tools/build_destiny_data.py` — Regenerates `scrappie/data/`.
- `requirements.txt` — Python dependencies.
- `nixpacks.toml` and `Procfile` — Production start command (`python main.py`).
- `logs/` — Directory created at runtime containing `bot.log`.

## Updating Destiny data

After a Destiny 2 content patch, run `python tools/build_destiny_data.py` from the repo root. It needs about 1 GB of memory, downloads about 300 MB and needs no key. Commit `scrappie/data/`.

The dungeon rotation (`DUNGEON_CYCLE` and `DUNGEON_CYCLE_ANCHOR`) is an unofficial community pattern defined in the same script and written to `raids.json`. If a log line says live dungeons differ from the cycle, or Bungie confirms a rotation change, edit `DUNGEON_CYCLE` or its anchor in `tools/build_destiny_data.py` and regenerate.

## Changing the dungeon account

The dungeon section reads the activity data of one account. To change it, set `$env:BUNGIE_API_KEY`, then run this with your own Bungie name (the values below are placeholders):

```powershell
$body = @{ displayName = "Name"; displayNameCode = 1234 } | ConvertTo-Json
(Invoke-RestMethod -Method Post -Uri https://www.bungie.net/Platform/Destiny2/SearchDestinyPlayerByBungieName/-1/ -Headers @{ "X-API-Key" = $env:BUNGIE_API_KEY } -Body $body -ContentType application/json -MaximumRedirection 0).Response
```

Use the entry where `membershipType` equals `crossSaveOverride`, or the only entry. Put its `membershipType` and `membershipId` in `.env` or the host settings as `DESTINY_MEMBERSHIP_TYPE` and `DESTINY_MEMBERSHIP_ID`, then restart. The account needs a public activity setting. Never commit these values.

## Troubleshooting

- Invalid or missing `DISCORD_TOKEN` exits the process on startup.
- If music commands don't work, check for FFmpeg/Opus/yt-dlp installation and the bot logs.
- No Destiny commands means `BUNGIE_API_KEY` is unset (logged at startup).
- No weekly post: check `WEEKLY_RESET_CHANNEL_ID`, the bot's View Channel, Send Messages, Embed Links and Read Message History permissions there, and the `logs/bot.log` lines starting `Weekly post:`. A missed post is caught up within 24 hours of the reset, then skipped. Bungie maintenance only delays the post.
- Dungeons marked "(predicted)" are not confirmed by Bungie. The `Weekly post:` log line says why: account not configured, couldn't read the account, no character data (privacy), no dungeon challenges (empty), only one dungeon visible, profile data older than the reset, live dungeons match last week's pair (stale), or live dungeons differ from the cycle (disagree, live wins).
- Dungeon weapon lists can be incomplete, because they come from Bungie's source strings (Pit of Heresy lists one weapon and The Shattered Throne none).
- Modifier warnings in the log mean the Destiny data needs regenerating.
- Use the Flask `GET /health` endpoint to check uptime when deployed.

## Contributing

Pull requests welcome. Keep changes focused. There are no tests or linters; at minimum check that `python -m compileall -q main.py scrappie` passes.
