"""SQLite storage for the bot process. One connection, used only under the lock from worker threads, so the event loop never blocks."""
import asyncio
import json
import os
import threading
from pathlib import Path
from typing import List, NamedTuple, Optional, Sequence, Tuple

try:
    import sqlite3
except ImportError:  # A Python built without it: the bot still runs, without storage
    sqlite3 = None

from .config import Config
from .log import logger

SCHEMA_VERSION = 1
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS trivia_scores (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, points INTEGER NOT NULL, answered INTEGER NOT NULL,
        current_streak INTEGER NOT NULL, best_streak INTEGER NOT NULL, PRIMARY KEY (guild_id, user_id))""",
    """CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id INTEGER PRIMARY KEY, weekly_channel_id INTEGER, weekly_enabled INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, channel_id INTEGER,
        due_at INTEGER NOT NULL, message TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS reminders_due ON reminders (due_at)",
    "CREATE INDEX IF NOT EXISTS reminders_user ON reminders (user_id)",
    """CREATE TABLE IF NOT EXISTS playlists (
        user_id INTEGER NOT NULL, name_key TEXT NOT NULL, name TEXT NOT NULL, track_count INTEGER NOT NULL,
        tracks TEXT NOT NULL, PRIMARY KEY (user_id, name_key))""",
)
SAVED, EXISTS, FULL, INVALID = "saved", "exists", "full", "invalid"

class StorageError(Exception):
    """Storage is closed, busy or failed. The message is a type name, never user data."""

class ScoreRow(NamedTuple):
    user_id: int
    points: int
    best_streak: int
    current_streak: int

class GuildSettings(NamedTuple):
    guild_id: int
    weekly_channel_id: Optional[int]
    weekly_enabled: int

class Reminder(NamedTuple):
    id: int
    user_id: int
    channel_id: Optional[int]  # None means deliver by DM
    due_at: int  # Unix seconds, always UTC
    message: str

class _Store:
    def __init__(self):
        self.conn = None
        self.lock = threading.Lock()

_store = _Store()

def resolve_path() -> Path:
    """Read lazily: the module imports before load_dotenv()."""
    explicit = (os.getenv("SCRAPPIE_DB_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    volume = (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    if volume:
        return Path(volume) / Config.DB_FILE
    return Path.cwd() / "data" / Config.DB_FILE

def _migrate(conn) -> None:
    with conn:
        for statement in SCHEMA:
            conn.execute(statement)
        found = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if found is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        elif found > SCHEMA_VERSION:
            raise sqlite3.DatabaseError("database is newer than this code")

def init() -> bool:
    """Open and prepare the database. Never raises: on failure the bot runs without storage."""
    if _store.conn is not None:
        return True
    if sqlite3 is None:
        logger.error("Storage disabled: this Python has no sqlite3 module")
        return False
    path = resolve_path()
    conn = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=Config.DB_BUSY_SECONDS, check_same_thread=False)
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]  # Stays on the default journal if the volume refuses
        _migrate(conn)
    except (OSError, sqlite3.Error) as e:
        if conn is not None:
            conn.close()
        logger.error(f"Storage disabled: can't open {path} ({type(e).__name__})")
        return False
    _store.conn = conn
    logger.info(f"Storage ready at {path} (journal: {mode}, schema {SCHEMA_VERSION})")
    return True

def available() -> bool:
    return _store.conn is not None

def close() -> None:
    """Idempotent. Runs on the loop at shutdown; the lock is held only for short statements."""
    locked = _store.lock.acquire(timeout=Config.DB_LOCK_SECONDS)
    conn, _store.conn = _store.conn, None
    try:
        if conn is not None and locked:
            conn.close()
            logger.info("Storage closed")
    finally:
        if locked:
            _store.lock.release()

def _locked(work, args):
    if not _store.lock.acquire(timeout=Config.DB_LOCK_SECONDS):
        raise StorageError("busy")
    try:
        conn = _store.conn
        if conn is None:
            raise StorageError("closed")
        with conn:  # Commits on success, rolls back on any exception
            return work(conn, *args)
    finally:
        _store.lock.release()

async def _run(work, *args):
    if _store.conn is None:
        raise StorageError("unavailable")
    try:
        return await asyncio.wait_for(asyncio.to_thread(_locked, work, args), Config.DB_TIMEOUT)
    except StorageError:
        raise
    except (TimeoutError, sqlite3.Error, ValueError, TypeError) as e:
        logger.warning(f"Storage call {work.__name__} failed ({type(e).__name__})")
        raise StorageError(type(e).__name__) from e

# Trivia

def _record_round(conn, guild_id, user_id, outcomes, answered):
    row = conn.execute("SELECT points, answered, current_streak, best_streak FROM trivia_scores WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)).fetchone()
    if row is None and not answered:
        return  # Only timeouts from a new player: nothing to rank
    points, total, current, best = row or (0, 0, 0, 0)
    for correct in outcomes:
        current = current + 1 if correct else 0
        best = max(best, current)
        points += 1 if correct else 0
    conn.execute("INSERT OR REPLACE INTO trivia_scores (guild_id, user_id, points, answered, current_streak, best_streak) VALUES (?, ?, ?, ?, ?, ?)",
                 (guild_id, user_id, points, total + answered, current, best))

async def record_trivia_round(guild_id: int, user_id: int, outcomes: Sequence[bool], answered: int) -> None:
    """outcomes has one entry per question played, timeouts included; answered excludes timeouts."""
    await _run(_record_round, guild_id, user_id, list(outcomes), answered)

def _leaderboard(conn, guild_id, user_id, limit):
    columns = "user_id, points, best_streak, current_streak"
    top = [ScoreRow(*r) for r in conn.execute(
        f"SELECT {columns} FROM trivia_scores WHERE guild_id = ? AND points > 0 ORDER BY points DESC, best_streak DESC, user_id ASC LIMIT ?", (guild_id, limit))]
    mine = conn.execute(f"SELECT {columns} FROM trivia_scores WHERE guild_id = ? AND user_id = ? AND points > 0", (guild_id, user_id)).fetchone()
    if mine is None:
        return top, None, None
    mine = ScoreRow(*mine)
    ahead = conn.execute(
        """SELECT COUNT(*) FROM trivia_scores WHERE guild_id = ? AND points > 0 AND (points > ?
           OR (points = ? AND best_streak > ?) OR (points = ? AND best_streak = ? AND user_id < ?))""",
        (guild_id, mine.points, mine.points, mine.best_streak, mine.points, mine.best_streak, user_id)).fetchone()[0]
    return top, mine, ahead + 1

async def leaderboard(guild_id: int, user_id: int) -> Tuple[List[ScoreRow], Optional[ScoreRow], Optional[int]]:
    """(top rows, caller's row, caller's rank); the last two are None if the caller has no points."""
    return await _run(_leaderboard, guild_id, user_id, Config.LEADERBOARD_SIZE)

# Guild settings

def _get_settings(conn, guild_id):
    row = conn.execute("SELECT guild_id, weekly_channel_id, weekly_enabled FROM guild_settings WHERE guild_id = ?", (guild_id,)).fetchone()
    return GuildSettings(*row) if row else None

async def get_settings(guild_id: int) -> Optional[GuildSettings]:
    return await _run(_get_settings, guild_id)

def _set_settings(conn, guild_id, channel_id, enabled):
    conn.execute("INSERT OR REPLACE INTO guild_settings (guild_id, weekly_channel_id, weekly_enabled) VALUES (?, ?, ?)", (guild_id, channel_id, 1 if enabled else 0))

async def set_settings(guild_id: int, channel_id: Optional[int], enabled: bool) -> None:
    await _run(_set_settings, guild_id, channel_id, enabled)

def _weekly_rows(conn):
    return [GuildSettings(*r) for r in conn.execute("SELECT guild_id, weekly_channel_id, weekly_enabled FROM guild_settings ORDER BY guild_id")]

async def weekly_rows() -> List[GuildSettings]:
    return await _run(_weekly_rows)

def _forget_guild(conn, guild_id):
    conn.execute("DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,))
    conn.execute("DELETE FROM trivia_scores WHERE guild_id = ?", (guild_id,))

async def forget_guild(guild_id: int) -> None:
    """Playlists belong to users and stay; reminders fall back to DM on delivery."""
    await _run(_forget_guild, guild_id)

# Reminders

def _add_reminder(conn, user_id, channel_id, due_at, message):
    count = conn.execute("SELECT COUNT(*) FROM reminders WHERE user_id = ?", (user_id,)).fetchone()[0]
    if count >= Config.REMINDER_MAX_ACTIVE:
        return None
    return conn.execute("INSERT INTO reminders (user_id, channel_id, due_at, message) VALUES (?, ?, ?, ?)", (user_id, channel_id, due_at, message)).lastrowid

async def add_reminder(user_id: int, channel_id: Optional[int], due_at: int, message: str) -> Optional[int]:
    """The new id, or None at the per-user cap. Count and insert share one transaction."""
    return await _run(_add_reminder, user_id, channel_id, due_at, message)

def _list_reminders(conn, user_id):
    return [Reminder(*r) for r in conn.execute("SELECT id, user_id, channel_id, due_at, message FROM reminders WHERE user_id = ? ORDER BY due_at, id", (user_id,))]

async def list_reminders(user_id: int) -> List[Reminder]:
    return await _run(_list_reminders, user_id)

def _cancel_reminder(conn, reminder_id, user_id):
    return conn.execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id)).rowcount == 1

async def cancel_reminder(reminder_id: int, user_id: int) -> bool:
    return await _run(_cancel_reminder, reminder_id, user_id)

def _due_reminders(conn, now, limit):
    return [Reminder(*r) for r in conn.execute("SELECT id, user_id, channel_id, due_at, message FROM reminders WHERE due_at <= ? ORDER BY due_at, id LIMIT ?", (now, limit))]

async def due_reminders(now: int, limit: int) -> List[Reminder]:
    return await _run(_due_reminders, now, limit)

def _take_reminder(conn, reminder_id):
    return conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,)).rowcount == 1

async def take_reminder(reminder_id: int) -> bool:
    """Delete before sending: at-most-once. False means it was cancelled meanwhile."""
    return await _run(_take_reminder, reminder_id)

# Playlists

def _clean_name(name: str) -> str:
    return " ".join(name.split())

def _save_playlist(conn, user_id, name, tracks):
    display = _clean_name(name)
    if not display or len(display) > Config.PLAYLIST_NAME_MAX:
        return INVALID
    key = display.casefold()
    if conn.execute("SELECT 1 FROM playlists WHERE user_id = ? AND name_key = ?", (user_id, key)).fetchone():
        return EXISTS
    if conn.execute("SELECT COUNT(*) FROM playlists WHERE user_id = ?", (user_id,)).fetchone()[0] >= Config.PLAYLIST_MAX_PER_USER:
        return FULL
    kept = tracks[:Config.PLAYLIST_MAX_TRACKS]
    conn.execute("INSERT INTO playlists (user_id, name_key, name, track_count, tracks) VALUES (?, ?, ?, ?, ?)",
                 (user_id, key, display, len(kept), json.dumps(kept)))
    return SAVED

async def save_playlist(user_id: int, name: str, tracks: List[tuple]) -> str:
    """SAVED, EXISTS, FULL or INVALID."""
    return await _run(_save_playlist, user_id, name, tracks)

def _get_playlist(conn, user_id, name):
    row = conn.execute("SELECT tracks FROM playlists WHERE user_id = ? AND name_key = ?", (user_id, _clean_name(name).casefold())).fetchone()
    return [(url, title) for url, title in json.loads(row[0])] if row else None  # Queue items must be tuples

async def get_playlist(user_id: int, name: str) -> Optional[List[tuple]]:
    return await _run(_get_playlist, user_id, name)

def _list_playlists(conn, user_id):
    return conn.execute("SELECT name, track_count FROM playlists WHERE user_id = ? ORDER BY name_key", (user_id,)).fetchall()

async def list_playlists(user_id: int) -> List[tuple]:
    """(name, track_count) rows. The autocomplete uses this too: indexed, and bounded by DB_TIMEOUT."""
    return await _run(_list_playlists, user_id)

def _delete_playlist(conn, user_id, name):
    return conn.execute("DELETE FROM playlists WHERE user_id = ? AND name_key = ?", (user_id, _clean_name(name).casefold())).rowcount == 1

async def delete_playlist(user_id: int, name: str) -> bool:
    return await _run(_delete_playlist, user_id, name)
