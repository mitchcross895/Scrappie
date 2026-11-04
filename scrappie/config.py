class Config:
    REQUEST_TIMEOUT = 15
    TRIVIA_TIMEOUT = 30
    SETUP_TIMEOUT = 60
    MAX_CITY_NAME_LENGTH = 100
    TRIVIA_CATEGORIES_API = "https://opentdb.com/api_category.php"
    TRIVIA_API = "https://opentdb.com/api.php?amount=1&type=multiple"
    DAD_JOKE_API = "https://icanhazdadjoke.com/"
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    LOG_FILE = "bot.log"
    MAX_REQUESTS_PER_MINUTE = 30
    RATE_LIMIT_CLEANUP_THRESHOLD = 10000  # Clean when this many users tracked
    QUEUE_CLEANUP_HOURS = 1  # How often to clean inactive queues
    BUNGIE_ROOT = "https://www.bungie.net"
    BUNGIE_API_ROOT = "https://www.bungie.net/Platform"
    WEEKLY_RESET_WEEKDAY = 1  # Tuesday
    WEEKLY_RESET_HOUR_UTC = 17
    WEEKLY_CHECK_MINUTES = 10
    WEEKLY_CATCHUP_HOURS = 24  # After this a missed post is skipped
    WEEKLY_HISTORY_LIMIT = 100  # Messages scanned for an existing post
