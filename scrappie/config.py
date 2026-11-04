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
