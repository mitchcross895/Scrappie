import importlib

import discord

from .log import logger

VOICE_AVAILABLE = False

try:
    importlib.import_module("yt_dlp")
    VOICE_AVAILABLE = True

    if not discord.opus.is_loaded():
        opus_libs = [
            'libopus.so.0', 'libopus.so', 'libopus.so.0.8.0', 'libopus.so.0.8',
            'opus', '/usr/lib/x86_64-linux-gnu/libopus.so.0',
            '/usr/lib/x86_64-linux-gnu/libopus.so',
            '/usr/lib/aarch64-linux-gnu/libopus.so.0',
            '/usr/lib/aarch64-linux-gnu/libopus.so',
            '/usr/local/lib/libopus.so.0', '/usr/local/lib/libopus.so',
        ]

        for opus_lib in opus_libs:
            try:
                discord.opus.load_opus(opus_lib)
                if discord.opus.is_loaded():
                    logger.info(f"Successfully loaded Opus from: {opus_lib}")
                    break
            except Exception:
                continue
        else:
            try:
                discord.opus.Encoder()  # Lazy-loads discord.py's default: its bundled DLL on Windows
                logger.info("Successfully loaded Opus from discord.py's default")
            except Exception:
                logger.error("Failed to load Opus library. Voice features disabled!")
                VOICE_AVAILABLE = False
except ImportError as e:
    logger.warning(f"yt-dlp or voice dependencies unavailable: {e}")
