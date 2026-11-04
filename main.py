"""Entry point. `python main.py` runs the bot and Flask; importing this file only exposes the WSGI app."""
from scrappie.log import logger, setup_logging
from scrappie.web import app

application = app  # For WSGI servers

if __name__ == "__main__":
    setup_logging()  # Before the next import: loading voice logs
    from scrappie.lifecycle import run
    run()
elif __name__ == "main":  # A plain import. Spawned children re-run this file under another name and stay quiet
    setup_logging()
    logger.warning("⚠️ Module imported - Discord bot not started")
    logger.warning("⚠️ Run this file directly with 'python main.py' to start both services")
    logger.warning("⚠️ Or deploy Discord bot and Flask separately for production")
