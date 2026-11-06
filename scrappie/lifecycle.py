import asyncio
import multiprocessing
import os
import re
import signal
import sys

import discord
from dotenv import load_dotenv

from .bot import bot
from .log import logger
from .state import bot_state
from .voice import VOICE_AVAILABLE
from .web import start_flask_server

# Commands register on import, and this order is the command tree order
if VOICE_AVAILABLE:
    from . import music
from . import general, trivia, weather
from .events import status_update_task

__all__ = ["general", "music", "trivia", "weather"]  # Imported for their registration side effects

def validate_environment():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.critical("Missing DISCORD_TOKEN environment variable.")
        sys.exit(1)
    if not re.match(r'^[A-Za-z0-9._-]+$', token):
        logger.critical("Invalid Discord token format.")
        sys.exit(1)
    return token

async def cleanup_resources():
    """Runs before bot.close(): queues and loop state must be gone before voice disconnects, or after_play starts the next song."""
    logger.info("🧹 Cleaning up resources...")

    bot_state.music_queues.clear()
    bot_state.request_counts.clear()
    bot_state.now_playing.clear()
    for guild_key in list(bot_state.idle_tasks): bot_state.cancel_idle(guild_key)
    bot_state.loop_modes.clear()
    bot_state.current_tracks.clear()
    bot_state.skip_requested.clear()
    bot_state.now_playing_messages.clear()
    bot_state.track_timing.clear()

    if status_update_task.is_running():
        status_update_task.cancel()

    queue_cleanup = bot_state.queue_cleanup_task
    if queue_cleanup is not None and queue_cleanup.is_running():
        queue_cleanup.cancel()

    weekly_reset = bot_state.weekly_reset_task
    if weekly_reset is not None and weekly_reset.is_running():
        weekly_reset.cancel()

    await bot_state.cleanup()

    logger.info("✅ Cleanup complete")

async def run_bot(token):
    """asyncio.run cancels this task on SIGINT; SIGTERM is wired to do the same."""
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    except NotImplementedError:
        pass  # Windows: no loop signal handlers, and TerminateProcess can't be caught anyway

    try:
        async with bot:
            try:
                await bot.start(token)
            finally:
                await cleanup_resources()
    except asyncio.CancelledError:
        logger.info("👋 Bot shutdown complete")

def start_discord_bot(token):
    """Start the bot in the main process and exit non-zero on startup failure."""
    try:
        logger.info("🚀 Starting Discord bot...")
        asyncio.run(run_bot(token))
    except discord.LoginFailure:
        logger.critical("❌ Invalid Discord token!")
        sys.exit(1)
    except discord.HTTPException as e:
        logger.critical(f"❌ Discord HTTP error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("⏹️ Keyboard interrupt received")
    except Exception as e:
        logger.critical(f"💥 Bot startup failed: {e}", exc_info=True)
        sys.exit(1)

def setup_destiny():
    """After load_dotenv(): commands register at import, before the key is readable, so this one module registers late."""
    if not os.getenv("BUNGIE_API_KEY"):
        logger.info("Destiny features disabled: BUNGIE_API_KEY is not set")
        return
    from . import destiny
    destiny.setup()

def run():
    """`python main.py`: check the token, start Flask, then run the bot in this process."""
    load_dotenv()  # Before Process.start(): the Flask child reads PORT from the inherited environment
    token = validate_environment()
    setup_destiny()

    logger.info("🚀 Starting bot with separate processes...")

    # Start Flask in a separate process
    flask_process = multiprocessing.Process(target=start_flask_server, daemon=True)
    flask_process.start()

    # Start Discord bot in main process (so signal handling works properly)
    try:
        start_discord_bot(token)
    finally:
        # Cleanup Flask process
        if flask_process.is_alive():
            logger.info("Terminating Flask process...")
            flask_process.terminate()
            flask_process.join(timeout=5)
            if flask_process.is_alive():
                flask_process.kill()
