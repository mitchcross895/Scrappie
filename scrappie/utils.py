import asyncio
from functools import lru_cache, wraps
from typing import Optional

import discord

from .log import logger
from .state import bot_state

def create_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)

async def safe_api_request(url: str, params: dict = None, headers: dict = None, allow_redirects: bool = True) -> Optional[dict]:
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
            logger.error(f"API request failed: {url} returned {response.status}")
    except asyncio.TimeoutError:
        logger.error(f"API request timeout: {url}")
    except Exception as e:
        logger.error(f"API request error for {url}: {e}")
    return None

@lru_cache(maxsize=128)
def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds//60}m {seconds%60}s"
    return f"{seconds//3600}h {(seconds%3600)//60}m {seconds%60}s"

def rate_limit(func):
    @wraps(func)
    async def wrapper(interaction: discord.Interaction, *args, **kwargs):
        if bot_state.is_rate_limited(interaction.user.id):
            embed = create_embed("Rate Limited", "You're making requests too quickly. Please wait.", discord.Color.red())
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        return await func(interaction, *args, **kwargs)
    return wrapper
