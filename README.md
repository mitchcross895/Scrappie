# Scrappie - Discord Bot

This repository contains a Discord bot (and a small Flask health endpoint) with these primary features:

- **Music (voice)**
	- `/play <query|url>` — Play a song or add to the guild queue. Supports playlists.
	- `/queue` — View the current music queue (paged, 10 per page).
	- `/skip` — Skip the currently playing track.
	- `/stop` — Stop playback and clear the queue.
	- `/leave` — Disconnect the bot from voice.
	- `/shuffle` — Shuffle the current guild queue (randomizes upcoming tracks).
	- `/pause`, `/resume` — Pause and resume the current song (also buttons on the Now Playing message).
	- `/nowplaying` — Current song with elapsed time and loop mode.
	- `/remove <position>` — Remove one queued song.
	- `/loop <off|song|queue>` — Loop the current song or the whole queue. The bot leaves voice after 2 minutes of silence.
	- `/playlist save <name>`, `load <name>`, `list`, `delete <name>` — Your own saved playlists (up to 10, 100 songs each). `save` stores the current song and queue, `load` adds one to the queue. Names ignore case and extra spaces, and saving over an existing name is refused.

- **Trivia / Games**
	- `/trivia` — Trivia rounds of 1 to 10 questions with optional category and difficulty, time-limited buttons, an end summary, and a Play Again button that repeats the same settings.
	- `/leaderboard` — This server's trivia top 10 and your own rank. Points are one per correct answer; ties go to the longer best streak. Needs storage.
	- `/fact` — Return a random fact.
	- `/dadjoke` — Return a random dad joke.
	- `/coin` — Flip a coin (Heads/Tails).
	- `/roll [dice]` — Roll dice in NdS+M notation, like `2d6+1` (default `1d6`).
	- `/number <min> <max>` — Generate a random integer in the specified range.

- **Utilities**
	- `/weather <city> [units]` — Fetches current weather and a short forecast (uses `python_weather`). Units are Fahrenheit by default or Celsius.
	- `/remind <time> <message>` — Private reminder after 10m, 2h, 1d and so on (1 minute to 30 days, 25 active per person). It posts in the channel where you set it, falls back to a DM, and pings only you. A reminder that came due while the bot was offline arrives late with a note. Needs storage.
	- `/reminders` — List your reminders and cancel one from a dropdown.
	- `/ping` — Returns latency, uptime, guild/user counts.
	- `/help` — Shows a command summary with a dropdown to pick one section.

- **Destiny 2** (needs `BUNGIE_API_KEY`)
	- `/item <name> [type]` — Weapon or armor lookup with autocomplete and a light.gg link. The optional type limits the search to weapons or armor.
	- `/weekly` — Private preview of the weekly raid and dungeon post, with a Next week button for the predicted rotation.
	- `/raid <name>` and `/dungeon <name>` — Drops, armor set bonuses and the predicted rotation for one raid or dungeon. Local data only, so no Bungie call.
	- Automatic weekly post at Tuesday 17:00 UTC (11am CST, 12pm CDT) with the featured raids and dungeons, modifiers, drops, set bonuses and a labelled one-week prediction. Dungeons and the forecast are labelled predicted when Bungie has not confirmed them. Posts to every server that turned it on with `/weeklysetup`, plus the host default channel `WEEKLY_RESET_CHANNEL_ID` when that server has no setting.
	- `/weeklysetup [channel] [enabled]` — Needs Manage Server. Shows or changes this server's weekly post: pick a channel to turn it on, or set `enabled` to false to turn it off. A server turned off is never posted to, even when it holds the host default channel. Needs storage.

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
- `BUNGIE_API_KEY` (optional) — Enables `/item`, `/weekly`, `/raid`, `/dungeon` and the weekly post. Get one at bungie.net/en/Application.
- `WEEKLY_RESET_CHANNEL_ID` (optional) — Host default channel for the weekly post, used only for a server that has no `/weeklysetup` setting. A server that turned the post off wins over it. Without it the commands still work and only servers set up with `/weeklysetup` get the post.
- `SCRAPPIE_DB_PATH` (optional) — Full path of the SQLite file that holds the leaderboard, server settings, reminders and playlists. Fallback order: `SCRAPPIE_DB_PATH`, then `RAILWAY_VOLUME_MOUNT_PATH/scrappie.db`, then `data/scrappie.db` under the working directory. If the file can't be opened the bot still runs and the storage commands reply that storage is unavailable.
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
	- `db.py` — SQLite storage: trivia scores, weekly settings, reminders and playlists. The only module that uses `sqlite3`.
	- `bot.py` — The Discord bot object.
	- `utils.py` — Embed, HTTP, formatting and rate limit helpers.
	- `voice.py` — Opus and yt-dlp availability.
	- `music.py`, `trivia.py`, `weather.py`, `general.py` — Slash commands.
	- `reminders.py` — `/remind`, `/reminders` and the delivery loop.
	- `destiny.py` — Destiny commands and the weekly post loop.
	- `data/` — Generated Bungie data (raids, dungeons and items).
	- `events.py` — Discord events and the status rotation task.
	- `lifecycle.py` — Command registration order, startup and shutdown.
- `tools/build_destiny_data.py` — Regenerates `scrappie/data/`.
- `requirements.txt` — Python dependencies.
- `nixpacks.toml` and `Procfile` — Production start command (`python main.py`).
- `logs/` — Directory created at runtime containing `bot.log`.
- `data/` — Local database folder created at runtime when no path is configured (`data/scrappie.db`). It is gitignored, as are `*.db` files. Not to be confused with the tracked `scrappie/data/`.

## Storage

The leaderboard, per-server weekly settings, reminders and playlists live in one SQLite file, opened with the Python standard library only. Where it goes is set by `SCRAPPIE_DB_PATH`, then `RAILWAY_VOLUME_MOUNT_PATH`, then `data/scrappie.db`.

- Run one instance only. Two bots on the same file would both deliver every reminder.
- Development: set `SCRAPPIE_DB_PATH` to a folder outside OneDrive or any synced folder, because file sync and the `-wal` / `-shm` side files don't mix.
- Backups: stop the service before copying the file, or use the `backup()` method of Python's `sqlite3` module. If you copy raw files, copy the `-wal` file too. Use Railway volume backups if your plan has them.
- Reminders are delivered at most once. A reminder is deleted just before it is sent, so a crash in that instant, or both the channel and the DM failing, loses it. Look for `Reminder ... dropped` in the log.

## Deployment on Railway

Do these in the Railway dashboard (labels may differ).

1. Open the project and create a Volume attached to the Scrappie service, with mount path `/data`.
2. In the service Variables, add `SCRAPPIE_DB_PATH=/data/scrappie.db`.
3. Keep the service at 1 replica. A service with a volume can't have overlapping deploys or replicas, so expect a short gap on each redeploy.
4. Deploy, then find `Storage ready at /data/scrappie.db (journal: wal, schema 1)` in the Deploy Logs. `journal: delete` is fine and only means WAL was refused. `Storage disabled:` means the path or volume needs fixing. A path outside `/data` means the variable did not apply and data would be lost at the next deploy.
5. Smoke test: run `/remind 2m test`, redeploy, and confirm `/reminders` or the delivery survives. Run `/playlist save` then `/playlist list` after a redeploy too.
6. To use server settings instead of the host channel, run `/weeklysetup channel:#x` in each server.

`logs/bot.log` is not on the volume and is lost on every redeploy. Railway's Deploy Logs show the same lines, because the logger also writes to stdout, so read those instead.

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
- No weekly post: run `/weeklysetup` in the server to see its state, check `WEEKLY_RESET_CHANNEL_ID` for the host default, the bot's View Channel, Send Messages, Embed Links and Read Message History permissions in the channel, and the `logs/bot.log` lines starting `Weekly post:`. Each channel is handled on its own, and the lines say which channel (`can't reach channel`, `missing ... in channel`, `send failed`, `catch-up window passed`). A `can't reach channel` line for a server the bot was removed from while offline is harmless. A missed post is caught up within 24 hours of the reset, then skipped. Bungie maintenance only delays the post.
- Dungeons marked "(predicted)" are not confirmed by Bungie. The `Weekly post:` log line says why: account not configured, couldn't read the account, no character data (privacy), no dungeon challenges (empty), only one dungeon visible, profile data older than the reset, live dungeons match last week's pair (stale), or live dungeons differ from the cycle (disagree, live wins).
- Dungeon weapon lists can be incomplete, because they come from Bungie's source strings (Pit of Heresy lists one weapon and The Shattered Throne none).
- Modifier warnings in the log mean the Destiny data needs regenerating.
- "Storage is unavailable" in a reply means look for a `Storage disabled:` line in the log from startup. Everything outside the storage commands keeps working.
- The leaderboard, settings, reminders or playlists reset after a deploy: the database is not on the volume. Check the `Storage ready at` path.
- A reminder never arrived: look for `Reminder ... dropped` in the log. It means both the channel and the DM failed (for example DMs closed).
- Use the Flask `GET /health` endpoint to check uptime when deployed.

## Contributing

Pull requests welcome. Keep changes focused. There are no tests or linters; at minimum check that `python -m compileall -q main.py scrappie` passes.
