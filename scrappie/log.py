import logging
import sys
from pathlib import Path

from .config import Config

logger = logging.getLogger("scrappie")

def setup_logging():
    if logging.getLogger().handlers:  # Already configured, e.g. in a forked child
        return
    sys.stdout.reconfigure(errors="replace")  # Piped Windows stdout is cp1252 and can't encode emoji
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(f"logs/{Config.LOG_FILE}", encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
