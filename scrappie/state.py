import asyncio
import datetime
from collections import deque
from datetime import timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .config import Config
from .log import logger

class BotState:
    def __init__(self):
        self.start_time = datetime.datetime.now(timezone.utc)
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.music_queues: Dict[str, deque] = {}
        self.request_counts: Dict[int, List] = {}
        self.now_playing: Dict[str, Optional[str]] = {}
        self.loop_modes: Dict[str, str] = {}  # "song" or "queue"; a missing key means off
        self.current_tracks: Dict[str, tuple] = {}  # The playing (url, title); a looped song comes back from here
        self.skip_requested: set = set()  # Guild keys whose next stop is a skip, so loop song doesn't replay it
        self.idle_tasks: Dict[str, asyncio.Task] = {}
        self.now_playing_messages: Dict[str, tuple] = {}  # (message, view)
        self.track_timing: Dict[str, dict] = {}
        self.active_trivia: set = set()  # User ids with a round running
        self.trivia_api_lock = asyncio.Lock()
        self.trivia_last_call = 0.0  # time.monotonic() of the last OpenTDB call
        self.queue_cleanup_task: Optional[Any] = None  # Set by music.py; stays None without voice support
        self.reminder_task: Optional[Any] = None  # Set by reminders.py
        self.weekly_reset_task: Optional[Any] = None  # Set by destiny.setup(); stays None without a key
        self.weekly_posted: Dict[int, datetime.datetime] = {}  # Channel id -> last reset posted
        self.weekly_log_reset: Optional[datetime.datetime] = None
        self.weekly_logged: set = set()
        self.weekly_raids_ready: Optional[tuple] = None  # (reset, first time raids were fresh), starts the dungeon wait

    def cancel_idle(self, guild_key: str):
        task = self.idle_tasks.pop(guild_key, None)
        if task is not None:
            task.cancel()

    def end_music_session(self, guild_key: str):
        """Synchronous on purpose: callers run it with no await after stopping, so a queued play_next_song finds nothing to loop."""
        self.cancel_idle(guild_key)
        self.now_playing.pop(guild_key, None)
        self.current_tracks.pop(guild_key, None)
        self.loop_modes.pop(guild_key, None)
        self.skip_requested.discard(guild_key)
        self.track_timing.pop(guild_key, None)

    async def retire_now_playing(self, guild_key: str):
        """Disable the buttons. Stopping the view matters: timeout=None views otherwise stay in discord.py's store forever."""
        entry = self.now_playing_messages.pop(guild_key, None)
        if entry is None:
            return
        message, view = entry
        view.stop()
        for child in view.children:
            child.disabled = True
        try:
            await message.edit(view=view)
        except Exception as e:
            logger.warning(f"Couldn't disable Now Playing buttons: {e}")

    async def initialize(self):
        await self.get_http_session()

    async def get_http_session(self) -> aiohttp.ClientSession:
        """Return the shared session, creating it if needed."""
        if self.http_session is None or self.http_session.closed:
            timeout = aiohttp.ClientTimeout(total=Config.REQUEST_TIMEOUT)
            self.http_session = aiohttp.ClientSession(
                timeout=timeout,
                headers={'User-Agent': Config.USER_AGENT}
            )
        return self.http_session

    async def cleanup(self):
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
            logger.info("HTTP session closed")

    def _prune_request_counts(self, now: datetime.datetime):
        logger.info(f"Cleaning up rate limit tracking (current size: {len(self.request_counts)})")
        cutoff = now - datetime.timedelta(hours=1)
        self.request_counts = {
            uid: [ts for ts in timestamps if ts > cutoff]
            for uid, timestamps in self.request_counts.items()
            if any(ts > cutoff for ts in timestamps)
        }
        logger.info(f"Rate limit cleanup complete (new size: {len(self.request_counts)})")

    def is_rate_limited(self, user_id: int) -> bool:
        """Record a request and return True if the user is over the limit."""
        now = datetime.datetime.now(timezone.utc)

        # Drop stale users once tracking passes the threshold
        if len(self.request_counts) > Config.RATE_LIMIT_CLEANUP_THRESHOLD:
            self._prune_request_counts(now)

        if user_id not in self.request_counts:
            self.request_counts[user_id] = []

        cutoff = now - datetime.timedelta(minutes=1)
        self.request_counts[user_id] = [ts for ts in self.request_counts[user_id] if ts > cutoff]

        if len(self.request_counts[user_id]) >= Config.MAX_REQUESTS_PER_MINUTE:
            return True

        self.request_counts[user_id].append(now)
        return False

bot_state = BotState()

def queue_key(guild_id) -> str:
    """music_queues and now_playing are keyed by str(guild_id)."""
    return str(guild_id)
