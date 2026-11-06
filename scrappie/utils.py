import asyncio
from functools import lru_cache, wraps
from typing import Optional

import discord

from . import db
from .config import Config
from .log import logger
from .state import bot_state

def create_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)

async def safe_api_request(url: str, params: dict = None, headers: dict = None, allow_redirects: bool = True, quiet: bool = False) -> Optional[dict]:
    try:
        session = await bot_state.get_http_session()
        async with session.get(url, params=params, headers=headers or {}, allow_redirects=allow_redirects) as response:
            if response.status == 200:
                # Check if response is JSON
                content_type = response.headers.get('Content-Type', '')
                if 'application/json' in content_type:
                    return await response.json()
                else:
                    # Return text content wrapped in dict for non-JSON responses
                    text = await response.text()
                    return {"text": text, "status": response.status}
            if not quiet:
                logger.error(f"API request failed: {url} returned {response.status}")
    except asyncio.TimeoutError:
        if not quiet:
            logger.error(f"API request timeout: {url}")
    except Exception as e:
        if not quiet:
            logger.error(f"API request error for {url}: {e}")
    return None

@lru_cache(maxsize=128)
def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds//60}m {seconds%60}s"
    return f"{seconds//3600}h {(seconds%3600)//60}m {seconds%60}s"

class BotView(discord.ui.View):
    """Base for the bot's views: View errors never reach bot.tree.error."""
    async def on_error(self, interaction: discord.Interaction, error: Exception, item, /):
        logger.error(f"View error in {type(self).__name__}: {error}", exc_info=error)
        embed = create_embed("💥 Error", "Unexpected error. Try later.", discord.Color.red())
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Couldn't send error message: {e}")

class RequesterView(BotView):
    """Only the person who ran the command can use it. Edits through the interaction, so it also works on ephemeral messages."""
    def __init__(self, interaction: discord.Interaction, timeout: float = Config.VIEW_TIMEOUT):
        super().__init__(timeout=timeout)
        self.interaction = interaction

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.interaction.user.id:
            return True
        await interaction.response.send_message("Only the person who ran this can use it.", ephemeral=True)
        return False

    async def close(self):
        self.stop()
        for child in self.children:
            child.disabled = True
        try:
            await self.interaction.edit_original_response(view=self)
        except Exception as e:
            logger.warning(f"Couldn't disable an expired view: {e}")

    async def on_timeout(self):
        await self.close()

def rate_limit(func):
    @wraps(func)
    async def wrapper(interaction: discord.Interaction, *args, **kwargs):
        if bot_state.is_rate_limited(interaction.user.id):
            embed = create_embed("Rate Limited", "You're making requests too quickly. Please wait.", discord.Color.red())
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        return await func(interaction, *args, **kwargs)
    return wrapper

def guild_required(func):
    """Runtime check, not guild_only: a DM gets a friendly reply."""
    @wraps(func)
    async def wrapper(interaction: discord.Interaction, *args, **kwargs):
        if interaction.guild is None:
            return await interaction.response.send_message("❌ This command only works in a server.", ephemeral=True)
        return await func(interaction, *args, **kwargs)
    return wrapper

STORAGE_DOWN = "Storage is unavailable right now. Everything else still works, so try this again later."

async def storage_down(interaction: discord.Interaction):
    embed = create_embed("\U0001F4BE Storage Unavailable", STORAGE_DOWN, discord.Color.orange())
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)

def storage_required(func):
    """Innermost decorator. Commands stay registered without storage and reply privately instead."""
    @wraps(func)
    async def wrapper(interaction: discord.Interaction, *args, **kwargs):
        if not db.available():
            return await storage_down(interaction)
        try:
            return await func(interaction, *args, **kwargs)
        except db.StorageError:
            return await storage_down(interaction)
    return wrapper
