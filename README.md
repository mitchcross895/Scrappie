# Discord Bot

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
	- Rate limiting per user (configurable via `Config.MAX_REQUESTS_PER_MINUTE`).
	- Logging to `logs/bot.log` and console.
	- Optional voice support which depends on `yt-dlp`, Opus library availability and FFmpeg.
	- Custom spellcheck words loaded from `addedwords.txt` (if present).

## Requirements

- Python 3.8+ (recommend 3.10+)
- FFmpeg installed and available on PATH (for voice playback)
- A Discord bot token
- See `requirements.txt` for Python dependencies.

## Environment

- `DISCORD_TOKEN` (required) — Bot token used to connect to Discord.
- `PORT` (optional) — Port for the Flask health server (default: 5000).
- `DEPLOYMENT` (optional) — Set to `true` in production or use `gunicorn` environment to change startup behavior.

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
	- A working Opus library (libopus) — the bot attempts to load common library names
	- FFmpeg available on PATH
- If any of the above are missing, music commands will be disabled and the bot logs a warning.

## Files of interest

- `main.py` — Bot entrypoint and all command/event logic.
- `requirements.txt` — Python dependencies.
- `logs/` — Directory created at runtime containing `bot.log`.

## Troubleshooting

- Invalid or missing `DISCORD_TOKEN` exits the process on startup.
- If music commands don't work, check for FFmpeg/Opus/yt-dlp installation and the bot logs.
- Use the Flask `GET /health` endpoint to check uptime when deployed.

## Contributing

Pull requests welcome. Keep changes focused and run linters/tests before submitting.

---

If you'd like, I can:
- Add more detailed operation examples for each command,
- Add instructions to run the bot in Docker/Gunicorn, or
- Add a CONTRIBUTING.md with local testing steps.
