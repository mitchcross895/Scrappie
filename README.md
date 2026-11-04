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
	- `events.py` — Discord events and the status rotation task.
	- `lifecycle.py` — Command registration order, startup and shutdown.
- `requirements.txt` — Python dependencies.
- `nixpacks.toml` and `Procfile` — Production start command (`python main.py`).
- `logs/` — Directory created at runtime containing `bot.log`.

## Troubleshooting

- Invalid or missing `DISCORD_TOKEN` exits the process on startup.
- If music commands don't work, check for FFmpeg/Opus/yt-dlp installation and the bot logs.
- Use the Flask `GET /health` endpoint to check uptime when deployed.

## Contributing

Pull requests welcome. Keep changes focused. There are no tests or linters; at minimum check that `python -m compileall -q main.py scrappie` passes.
