class Config:
    REQUEST_TIMEOUT = 15
    TRIVIA_TIMEOUT = 30
    TRIVIA_REVEAL_SECONDS = 5  # Pause on the answer before the next question
    TRIVIA_DEFAULT_QUESTIONS = 5
    TRIVIA_MAX_QUESTIONS = 10
    TRIVIA_MAX_TIMEOUTS = 2  # Timeouts in a row that end a round
    TRIVIA_API_SPACING = 5.5  # Seconds between OpenTDB calls; the limit is per IP
    MAX_CITY_NAME_LENGTH = 100
    TRIVIA_API = "https://opentdb.com/api.php?type=multiple"
    DAD_JOKE_API = "https://icanhazdadjoke.com/"
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    LOG_FILE = "bot.log"
    MAX_REQUESTS_PER_MINUTE = 30
    RATE_LIMIT_CLEANUP_THRESHOLD = 10000  # Clean when this many users tracked
    QUEUE_CLEANUP_HOURS = 1  # How often to clean inactive queues
    BUNGIE_ROOT = "https://www.bungie.net"
    BUNGIE_API_ROOT = "https://www.bungie.net/Platform"
    LIGHT_GG_ITEM_URL = "https://www.light.gg/db/items/{}/"  # Unverified: light.gg blocks automated fetches
    WEEKLY_RESET_WEEKDAY = 1  # Tuesday
    WEEKLY_RESET_HOUR_UTC = 17
    WEEKLY_CHECK_MINUTES = 10
    WEEKLY_CATCHUP_HOURS = 24  # After this a missed post is skipped
    WEEKLY_HISTORY_LIMIT = 100  # Messages scanned for an existing post
    WEEKLY_DUNGEON_WAIT_MINUTES = 30  # Longest the post waits for confirmed dungeons once raids are fresh
    WEEKLY_EMBED_LIMIT = 5900  # Discord allows 6000 per message; this keeps a margin
