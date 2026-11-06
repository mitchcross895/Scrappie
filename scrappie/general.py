import asyncio
import datetime
import random
import re
from datetime import timezone
from typing import Dict, Optional, Tuple

import discord
import randfacts
from discord import app_commands

from .bot import bot
from .config import Config
from .log import logger
from .state import bot_state
from .utils import RequesterView, create_embed, format_duration, rate_limit, safe_api_request
from .voice import VOICE_AVAILABLE

FUN_HELP = "`/trivia [category] [difficulty] [questions]` - Trivia round\n`/leaderboard` - Server trivia top 10\n`/fact` - Random fact\n`/dadjoke` - Random dad joke\n`/coin` - Flip coin\n`/roll [dice]` - Roll dice\n`/number <min> <max>` - Random number"
UTILITY_HELP = "`/weather <city> [units]` - Weather info\n`/remind <time> <message>` - Set a reminder\n`/reminders` - List or cancel reminders\n`/ping` - Bot latency\n`/help` - This message"
MUSIC_HELP = "`/play <song>` - Play music\n`/queue` - View queue\n`/skip` - Skip song\n`/shuffle` - Shuffle queue\n`/loop <mode>` - Loop off, song or queue\n`/pause` - Pause\n`/resume` - Resume\n`/nowplaying` - Current song\n`/remove <position>` - Remove a queued song\n`/stop` - Stop & clear\n`/leave` - Leave voice\n`/playlist save|load|list|delete` - Your saved playlists"
DESTINY_HELP = "`/item <name> [type]` - Weapon or armor lookup\n`/weekly` - Preview this week's raid and dungeon post\n`/raid <name>` - Raid drops and rotation\n`/dungeon <name>` - Dungeon drops and rotation\n`/weeklysetup [channel] [enabled]` - Admin: weekly post settings"
ALL_SECTIONS = "all"

def _help_sections() -> Dict[str, str]:
    """Built at call time: Destiny registers after import, and Music depends on voice."""
    sections = {"\U0001F3B2 Fun Commands": FUN_HELP, "\U0001F527 Utility": UTILITY_HELP}
    if VOICE_AVAILABLE:
        sections["\U0001F3B5 Music"] = MUSIC_HELP
    if bot.tree.get_command("item"):
        sections["\U0001F30C Destiny 2"] = DESTINY_HELP
    return sections

def _help_embed(sections: Dict[str, str], choice: str = ALL_SECTIONS) -> discord.Embed:
    embed = create_embed("\U0001F916 Bot Commands", "All available commands:", discord.Color.blue())
    for name, value in sections.items():
        if choice in (ALL_SECTIONS, name):
            embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text="Use slash commands (/) to interact!")
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    return embed

class HelpView(RequesterView):
    def __init__(self, interaction: discord.Interaction, sections: Dict[str, str]):
        super().__init__(interaction)
        self.sections = sections
        options = [discord.SelectOption(label="All commands", value=ALL_SECTIONS)]
        options += [discord.SelectOption(label=name, value=name) for name in sections]
        self.picker = discord.ui.Select(placeholder="Pick a section", options=options[:25])
        self.picker.callback = self.pick
        self.add_item(self.picker)

    async def pick(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=_help_embed(self.sections, self.picker.values[0]), view=self)

@bot.tree.command(name="help", description="Show available commands.")
async def help_command(interaction: discord.Interaction):
    sections = _help_sections()
    await interaction.response.send_message(embed=_help_embed(sections), view=HelpView(interaction, sections))

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

DICE_PATTERN = re.compile(r"(\d{0,3})d(\d{1,4})([+-]\d{1,4})?", re.IGNORECASE)  # Digit caps keep int() trivially safe

def parse_dice(text: str) -> Optional[Tuple[int, int, int]]:
    """(count, sides, modifier), or None if malformed or out of bounds."""
    match = DICE_PATTERN.fullmatch(text.replace(" ", ""))
    if not match:
        return None
    count, sides, modifier = int(match[1] or 1), int(match[2]), int(match[3] or 0)
    if not 1 <= count <= Config.DICE_MAX_COUNT or not 2 <= sides <= Config.DICE_MAX_SIDES or abs(modifier) > Config.DICE_MAX_MODIFIER:
        return None
    return count, sides, modifier

@bot.tree.command(name="roll", description="Roll dice.")
@app_commands.describe(dice="Dice notation, like 2d6+1")
async def roll_command(interaction: discord.Interaction, dice: app_commands.Range[str, 1, 20] = "1d6"):
    parsed = parse_dice(dice)
    if parsed is None:
        embed = create_embed("\u274c Invalid Dice", f"Use a format like 2d6+1. Up to {Config.DICE_MAX_COUNT} dice, 2 to {Config.DICE_MAX_SIDES} sides, modifier up to {Config.DICE_MAX_MODIFIER}.", discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    count, sides, modifier = parsed
    rolls = [random.randint(1, sides) for _ in range(count)]
    embed = create_embed("\U0001F3B2 Dice Roll", f"Rolling **{dice}**", discord.Color.blue())
    embed.add_field(name="Rolls", value=", ".join(map(str, rolls)), inline=False)
    embed.add_field(name="Total", value=str(sum(rolls) + modifier), inline=False)
    embed.set_footer(text=f"Rolled by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)
