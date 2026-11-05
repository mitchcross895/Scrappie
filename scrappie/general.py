import asyncio
import datetime
import random
from datetime import timezone

import discord
import randfacts
from discord import app_commands

from .bot import bot
from .config import Config
from .log import logger
from .state import bot_state
from .utils import create_embed, format_duration, rate_limit, safe_api_request
from .voice import VOICE_AVAILABLE

@bot.tree.command(name="help", description="Show available commands.")
async def help_command(interaction: discord.Interaction):
    embed = create_embed("🤖 Bot Commands", "All available commands:", discord.Color.blue())

    embed.add_field(
        name="🎲 Fun Commands",
        value="`/trivia [category] [difficulty] [questions]` - Trivia round\n`/fact` - Random fact\n`/dadjoke` - Random dad joke\n`/coin` - Flip coin\n`/number <min> <max>` - Random number",
        inline=False
    )

    embed.add_field(
        name="🔧 Utility",
        value="`/weather <city>` - Weather info\n`/ping` - Bot latency\n`/help` - This message",
        inline=False
    )

    if VOICE_AVAILABLE:
        embed.add_field(
            name="🎵 Music",
            value="`/play <song>` - Play music\n`/queue` - View queue\n`/skip` - Skip song\n`/shuffle` - Shuffle queue\n`/stop` - Stop & clear\n`/leave` - Leave voice",
            inline=False
        )

    if bot.tree.get_command("item"):
        embed.add_field(
            name="\U0001F30C Destiny 2",
            value="`/item <name>` - Weapon or armor lookup\n`/weekly` - Preview this week's raid and dungeon post",
            inline=False
        )

    embed.set_footer(text="Use slash commands (/) to interact!")
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="fact", description="Get a random fact.")
@rate_limit
async def fact_command(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        fact = randfacts.get_fact()
        embed = create_embed("🧠 Random Fact", fact, discord.Color.green())
        embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"Error getting fact: {e}")
        embed = create_embed("❌ Error", "Couldn't fetch fact. Try again later.", discord.Color.red())
        await interaction.followup.send(embed=embed)

def _dad_joke_embed(data: dict, user: discord.abc.User) -> discord.Embed:
    embed = create_embed("😄 Dad Joke", data['joke'], discord.Color.gold())
    footer_emoji = random.choice(["😂", "🤣", "😅", "😆", "🙃", "😏"])
    embed.set_footer(
        text=f"{footer_emoji} Requested by {user.display_name} • ID: {data.get('id', 'unknown')}",
        icon_url=user.display_avatar.url
    )
    embed.set_thumbnail(url="https://icanhazdadjoke.com/static/smile.svg")
    return embed

@bot.tree.command(name="dadjoke", description="Get a random dad joke!")
@rate_limit
async def dadjoke_command(interaction: discord.Interaction):
    """Fetch a random dad joke from icanhazdadjoke.com API"""
    await interaction.response.defer()

    try:
        # API requires Accept: application/json header and a custom User-Agent
        headers = {
            'Accept': 'application/json',
            'User-Agent': 'Discord Bot (https://github.com/yourbot)'
        }

        data = await safe_api_request(Config.DAD_JOKE_API, headers=headers)

        if data and 'joke' in data:
            embed = _dad_joke_embed(data, interaction.user)
        else:
            embed = create_embed("❌ Error", "Couldn't fetch a dad joke right now. Try again!", discord.Color.red())
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"Error getting dad joke: {e}")
        embed = create_embed(
            "❌ Error",
            "Something went wrong fetching the joke. Try again later!",
            discord.Color.red()
        )
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="ping", description="Check bot latency.")
async def ping_command(interaction: discord.Interaction):
    start = datetime.datetime.now(timezone.utc)
    embed = create_embed("🏓 Pong!", "Checking...", discord.Color.yellow())
    await interaction.response.send_message(embed=embed)

    response_time = (datetime.datetime.now(timezone.utc) - start).total_seconds() * 1000
    ws_latency = round(bot.latency * 1000)

    embed = create_embed("🏓 Pong!", "Connection status:", discord.Color.green())
    embed.add_field(name="WebSocket", value=f"{ws_latency}ms", inline=True)
    embed.add_field(name="Response", value=f"{response_time:.1f}ms", inline=True)
    embed.add_field(name="Status", value="✅ Online", inline=True)

    uptime = datetime.datetime.now(timezone.utc) - bot_state.start_time
    embed.add_field(name="Uptime", value=format_duration(int(uptime.total_seconds())), inline=True)
    embed.add_field(name="Guilds", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Users", value=str(len(bot.users)), inline=True)

    await interaction.edit_original_response(embed=embed)

@bot.tree.command(name="number", description="Generate random number.")
@app_commands.describe(min_num="Minimum", max_num="Maximum")
async def number_command(interaction: discord.Interaction, min_num: int, max_num: int):
    if min_num > max_num:
        embed = create_embed("❌ Invalid Range", f"Min ({min_num}) must be ≤ max ({max_num}).", discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    if max_num - min_num > 1000000:
        embed = create_embed("❌ Range Too Large", "Max range: 1,000,000.", discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    result = random.randint(min_num, max_num)
    embed = create_embed("🎲 Random Number", f"Between **{min_num}** and **{max_num}**:", discord.Color.blue())
    embed.add_field(name="Result", value=f"**{result}**", inline=False)
    embed.set_footer(text=f"Generated for {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="coin", description="Flip a coin.")
async def coin_command(interaction: discord.Interaction):
    embed = create_embed("🪙 Flipping...", "Coin spinning...", discord.Color.yellow())
    await interaction.response.send_message(embed=embed)
    await asyncio.sleep(1)

    result = "Heads" if random.randint(0, 1) == 0 else "Tails"
    emoji = "👑" if result == "Heads" else "🎯"

    embed = create_embed("🪙 Coin Flip", f"Landed on **{result}**! {emoji}", discord.Color.green())
    embed.set_footer(text=f"Flipped by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    await interaction.edit_original_response(embed=embed)
