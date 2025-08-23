import os
import re
import logging
import random
import html
from typing import Dict, Optional, Any, List
import asyncio
import datetime
from threading import Thread
import json

# Third-party imports
import discord
import aiohttp
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, Button, Select
from flask import Flask
from spellchecker import SpellChecker
import randfacts    
from dotenv import load_dotenv
import python_weather
from python_weather.errors import Error, RequestError

# ========== Configuration Constants ==========
class Config:
    # Timeout configurations
    REQUEST_TIMEOUT = 10
    TRIVIA_TIMEOUT = 30
    SETUP_TIMEOUT = 60
    STEAM_API_TIMEOUT = 15
    
    # Limits for security
    MAX_CITY_NAME_LENGTH = 100
    MIN_CITY_NAME_LENGTH = 1
    MAX_TRACKED_GAMES = 50
    
    # Steam APIs
    STEAM_SEARCH_API = "https://steamcommunity.com/actions/SearchApps"
    STEAM_STORE_API = "https://store.steampowered.com/api/appdetails"
    
    # Trivia API
    TRIVIA_CATEGORIES_API = "https://opentdb.com/api_category.php"
    TRIVIA_API = "https://opentdb.com/api.php?amount=1&type=multiple"
    
    # User agent for API requests
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

# Response constants
MISSPELL_REPLIES = [
    "Let's try that again, shall we?",
    "Great spelling, numb-nuts!",
    "Learn to spell, Sandwich.",
    "Learn English, Torta.",
    "Read a book, Schmuck!",
    "Seems like your dictionary took a vacation, pal!",
    "Even your keyboard is questioning your grammar, genius.",
    "Autocorrect just waved the white flag, rookie.",
    "Are you inventing a new language? Because that's something else!",
    "Spell check is tapping out—maybe it's time for a lesson!"
]

# ========== Logging Configuration ==========
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ========== Environment Setup ==========
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

if not DISCORD_TOKEN:
    logger.critical("Missing DISCORD_TOKEN environment variable.")
    exit(1)

if not re.match(r'^[A-Za-z0-9._-]+$', DISCORD_TOKEN):
    logger.critical("Invalid Discord token format.")
    exit(1)

# ========== Flask App ==========
app = Flask(__name__)

@app.route('/')
def home() -> str:
    return "Discord Bot is Running!"

# ========== Global Instances ==========
# Spell checker setup
SPELL = SpellChecker()
try:
    SPELL.word_frequency.load_text_file(os.path.join(os.path.dirname(__file__), "addedwords.txt"))
except FileNotFoundError:
    logger.warning("addedwords.txt not found, using default dictionary only")

# Discord bot setup
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# In-memory storage for Steam sales
steam_tracking_data = {}

# HTTP session for reuse
http_session: Optional[aiohttp.ClientSession] = None

async def get_http_session() -> aiohttp.ClientSession:
    """Get or create HTTP session for reuse."""
    global http_session
    if http_session is None or http_session.closed:
        timeout = aiohttp.ClientTimeout(total=Config.REQUEST_TIMEOUT)
        http_session = aiohttp.ClientSession(
            timeout=timeout,
            headers={'User-Agent': Config.USER_AGENT}
        )
    return http_session

async def cleanup_http_session():
    """Clean up HTTP session."""
    global http_session
    if http_session and not http_session.closed:
        await http_session.close()

# ========== Utility Functions ==========
def create_embed(title: str, description: str = None, color: discord.Color = discord.Color.blue()) -> discord.Embed:
    """Create a standard embed."""
    embed = discord.Embed(title=title, description=description, color=color)
    return embed

async def safe_api_request(url: str, params: dict = None) -> Optional[dict]:
    """Make a safe API request with error handling."""
    try:
        session = await get_http_session()
        async with session.get(url, params=params) as response:
            if response.status == 200:
                return await response.json()
            else:
                logger.error(f"API request failed: {url} returned {response.status}")
                return None
    except asyncio.TimeoutError:
        logger.error(f"API request timeout: {url}")
        return None
    except Exception as e:
        logger.error(f"API request error for {url}: {e}")
        return None

# ========== Steam API Functions ==========
async def search_steam_game(query: str) -> List[Dict[str, Any]]:
    """Search for Steam games by name."""
    params = {"term": query, "f": "games", "cc": "US", "l": "english"}
    data = await safe_api_request(Config.STEAM_SEARCH_API, params)
    
    if data and isinstance(data, list):
        return data[:10]
    
    # Fallback to mock data if API fails
    return await search_steam_game_fallback(query)

async def search_steam_game_fallback(query: str) -> List[Dict[str, Any]]:
    """Fallback Steam search with mock data."""
    mock_results = [
        {"appid": "730", "name": "Counter-Strike 2"},
        {"appid": "570", "name": "Dota 2"},
        {"appid": "440", "name": "Team Fortress 2"},
        {"appid": "1085660", "name": "Destiny 2"},
        {"appid": "945360", "name": "Among Us"},
        {"appid": "271590", "name": "Grand Theft Auto V"},
        {"appid": "578080", "name": "PLAYERUNKNOWN'S BATTLEGROUNDS"},
        {"appid": "1174180", "name": "Red Dead Redemption 2"},
        {"appid": "292030", "name": "The Witcher 3: Wild Hunt"},
        {"appid": "1091500", "name": "Cyberpunk 2077"},
    ]
    
    return [game for game in mock_results if query.lower() in game["name"].lower()][:10]

async def get_steam_game_details(app_id: str) -> Optional[Dict[str, Any]]:
    """Get detailed information about a Steam game."""
    params = {"appids": app_id, "cc": "US", "filters": "price_overview,basic", "format": "json"}
    data = await safe_api_request(Config.STEAM_STORE_API, params)
    
    if data and app_id in data and data[app_id].get("success"):
        return data[app_id]["data"]
    return None

async def check_steam_sales():
    """Check all tracked games for sales and send notifications."""
    logger.info("Checking Steam sales...")
    
    for guild_id, guild_data in steam_tracking_data.items():
        if not guild_data.get("games"):
            continue
            
        channel_id = guild_data.get("channel_id")
        if not channel_id:
            continue
            
        channel = bot.get_channel(channel_id)
        if not channel:
            logger.warning(f"Could not find channel {channel_id} for guild {guild_id}")
            continue
        
        await process_guild_sales(channel, guild_id, guild_data)

async def process_guild_sales(channel: discord.TextChannel, guild_id: int, guild_data: dict):
    """Process sales for a specific guild."""
    for app_id, game_data in guild_data["games"].items():
        try:
            game_details = await get_steam_game_details(app_id)
            if not game_details:
                continue
            
            price_overview = game_details.get("price_overview")
            if not price_overview:
                continue
            
            current_price = price_overview["final"] / 100
            discount_percent = price_overview.get("discount_percent", 0)
            original_price = price_overview["initial"] / 100 if price_overview["initial"] else current_price
            
            target_discount = game_data.get("target_discount", 0)
            last_price = game_data.get("last_price", current_price)
            
            if (discount_percent >= target_discount and 
                discount_percent > 0 and 
                current_price < last_price):
                
                await send_sale_notification(channel, app_id, game_data, current_price, 
                                           original_price, discount_percent, game_details)
                logger.info(f"Sent sale alert for {game_data['name']} ({discount_percent}% off)")
            
            # Update last known price
            steam_tracking_data[guild_id]["games"][app_id]["last_price"] = current_price
            
        except Exception as e:
            logger.error(f"Error checking sale for game {app_id}: {e}")

async def send_sale_notification(channel: discord.TextChannel, app_id: str, game_data: dict,
                               current_price: float, original_price: float, discount_percent: int,
                               game_details: dict):
    """Send a sale notification embed."""
    embed = create_embed("🔥 Steam Sale Alert!", color=discord.Color.red())
    embed.add_field(name="Wind", value=wind_info, inline=True)
    
    # Additional weather data
    embed.add_field(name="Precipitation", value=f"{weather.precipitation} in", inline=True)
    embed.add_field(name="Pressure", value=f"{weather.pressure} in", inline=True)
    embed.add_field(name="Visibility", value=f"{weather.visibility} mi", inline=True)
    
    if weather.ultraviolet:
        uv_text = str(weather.ultraviolet)
        if hasattr(weather.ultraviolet, "index"):
            uv_text = f"{uv_text} ({weather.ultraviolet.index})"
        embed.add_field(name="UV Index", value=uv_text, inline=True)
    
    # Forecast information
    if weather.daily_forecasts:
        forecast_text = ""
        
        for i, day_forecast in enumerate(weather.daily_forecasts[:3]):
            day_name = get_day_name(i, day_forecast)
            day_emoji = getattr(getattr(day_forecast, 'kind', None), 'emoji', '🌤️')
            
            day_text = f"{day_emoji} **{day_name}**: "
            
            if hasattr(day_forecast, 'description'):
                day_text += f"{day_forecast.description}, "
            elif hasattr(day_forecast, 'kind'):
                day_text += f"{day_forecast.kind}, "
            
            temp_info = get_temperature_info(day_forecast)
            day_text += temp_info
            
            forecast_text += day_text + "\n"
        
        embed.add_field(name="Forecast", value=forecast_text, inline=False)
    
    embed.set_footer(text=f"Data provided by python_weather • {weather.datetime.strftime('%H:%M')}")
    return embed

def get_day_name(index: int, day_forecast) -> str:
    """Get the appropriate day name for forecast."""
    if index == 0:
        return "Today"
    elif index == 1:
        return "Tomorrow"
    else:
        if hasattr(day_forecast, 'date'):
            return day_forecast.date.strftime('%A')
        else:
            return f"Day {index + 1}"

def get_temperature_info(day_forecast) -> str:
    """Extract temperature information from day forecast."""
    temp_high = getattr(day_forecast, 'highest', None) or getattr(day_forecast, 'high', None) or getattr(day_forecast, 'temperature', None)
    temp_low = getattr(day_forecast, 'lowest', None) or getattr(day_forecast, 'low', None)
    
    if temp_high is not None:
        temp_info = f"High: {temp_high}°F"
        if temp_low is not None:
            temp_info += f", Low: {temp_low}°F"
        return temp_info
    
    # Fallback: check all attributes for temperature-related data
    attrs = vars(day_forecast)
    temp_attrs = [f"{name}: {value}°F" for name, value in attrs.items() 
                 if any(keyword in name.lower() for keyword in ['temp', 'high', 'low'])]
    return ", ".join(temp_attrs) if temp_attrs else "Temperature data unavailable"

# ========== Additional Steam Commands ==========
@bot.tree.command(name="list_steam_games", description="List all tracked Steam games.")
async def list_steam_games_slash(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild.id
    
    if (guild_id not in steam_tracking_data or 
        not steam_tracking_data[guild_id].get("games")):
        return await interaction.response.send_message("No games are currently being tracked!")
    
    games = steam_tracking_data[guild_id]["games"]
    
    embed = create_embed("📊 Tracked Steam Games", color=discord.Color.blue())
    
    description = ""
    for app_id, game_data in games.items():
        description += f"**{game_data['name']}**\n"
        description += f"├ Target Discount: {game_data['target_discount']}%\n"
        description += f"├ Last Price: ${game_data['last_price']:.2f}\n"
        description += f"└ [Steam Store](https://store.steampowered.com/app/{app_id}/)\n\n"
    
    embed.description = description
    embed.set_footer(text=f"Tracking {len(games)} games")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="remove_steam_game", description="Remove a Steam game from tracking.")
async def remove_steam_game_slash(interaction: discord.Interaction, game_name: str) -> None:
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "You need administrator permissions to manage tracked games!", ephemeral=True
        )
    
    guild_id = interaction.guild.id
    
    if (guild_id not in steam_tracking_data or 
        not steam_tracking_data[guild_id].get("games")):
        return await interaction.response.send_message("No games are currently being tracked!")
    
    games = steam_tracking_data[guild_id]["games"]
    
    # Find game by name (exact or partial match)
    game_to_remove, app_id_to_remove = find_game_by_name(games, game_name)
    
    if not game_to_remove:
        return await interaction.response.send_message(
            f"Game '{game_name}' not found in tracked games. Use `/list_steam_games` to see tracked games."
        )
    
    del steam_tracking_data[guild_id]["games"][app_id_to_remove]
    
    embed = create_embed(
        "✅ Game Removed from Tracking",
        f"**{game_to_remove['name']}** is no longer being tracked for sales.",
        discord.Color.green()
    )
    
    await interaction.response.send_message(embed=embed)
    logger.info(f"Removed game {game_to_remove['name']} from tracking for guild {guild_id}")

def find_game_by_name(games: dict, game_name: str) -> tuple:
    """Find a game in the tracking list by name (exact or partial match)."""
    game_name_lower = game_name.lower()
    
    # Try exact match first
    for app_id, game_data in games.items():
        if game_data["name"].lower() == game_name_lower:
            return game_data, app_id
    
    # Try partial match
    for app_id, game_data in games.items():
        if game_name_lower in game_data["name"].lower():
            return game_data, app_id
    
    return None, None

@bot.tree.command(name="check_steam_sales_now", description="Manually check for Steam sales right now.")
async def check_steam_sales_now_slash(interaction: discord.Interaction) -> None:
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "You need administrator permissions to manually check sales!", ephemeral=True
        )
    
    guild_id = interaction.guild.id
    
    if (guild_id not in steam_tracking_data or 
        not steam_tracking_data[guild_id].get("games")):
        return await interaction.response.send_message("No games are currently being tracked!")
    
    await interaction.response.defer()
    
    try:
        await check_steam_sales()
        await interaction.followup.send("✅ Steam sales check completed! Any sales found have been posted.")
    except Exception as e:
        logger.error(f"Error during manual sales check: {e}")
        await interaction.followup.send("❌ An error occurred while checking for sales.")

@bot.tree.command(name="test_steam_search", description="Test Steam search functionality (Admin only)")
async def test_steam_search_slash(interaction: discord.Interaction, game_name: str) -> None:
    """Test command to debug Steam search issues."""
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "You need administrator permissions to use test commands!", ephemeral=True
        )
    
    await interaction.response.defer()
    
    embed = create_embed(
        "🔧 Steam Search Test",
        f"Testing search for: **{game_name}**",
        discord.Color.blue()
    )
    
    # Test primary search
    try:
        results1 = await search_steam_game(game_name)
        embed.add_field(
            name="Primary Search Results", 
            value=f"Found {len(results1)} results" if results1 else "No results found",
            inline=False
        )
        if results1:
            sample_games = [f"• {game['name']} (ID: {game['appid']})" for game in results1[:3]]
            embed.add_field(name="Sample Results", value="\n".join(sample_games), inline=False)
    except Exception as e:
        embed.add_field(name="Primary Search Error", value=str(e), inline=False)
    
    # Test fallback search
    try:
        results2 = await search_steam_game_fallback(game_name)
        embed.add_field(
            name="Fallback Search Results", 
            value=f"Found {len(results2)} results" if results2 else "No results found",
            inline=False
        )
        if results2:
            sample_games = [f"• {game['name']} (ID: {game['appid']})" for game in results2[:3]]
            embed.add_field(name="Sample Fallback Results", value="\n".join(sample_games), inline=False)
    except Exception as e:
        embed.add_field(name="Fallback Search Error", value=str(e), inline=False)
    
    await interaction.followup.send(embed=embed)

# ========== Bot Events ==========
@bot.event
async def on_ready() -> None:
    logger.info(f'{bot.user} has connected to Discord!')
    try:
        synced = await bot.tree.sync()
        logger.info(f'Synced {len(synced)} command(s)')
    except Exception as e:
        logger.error(f'Failed to sync commands: {e}')
    
    # Start the Steam sales task
    if not steam_sales_task.is_running():
        steam_sales_task.start()
        logger.info("Started Steam sales monitoring task")

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    logger.error(f'Command error: {error}')

@bot.event
async def on_message(message: discord.Message) -> None:
    """Handle message events for spell checking (if needed)."""
    if message.author == bot.user:
        return
    
    # Process commands first
    await bot.process_commands(message)

# ========== Cleanup and Shutdown ==========
async def cleanup_resources():
    """Clean up resources on shutdown."""
    await cleanup_http_session()
    if steam_sales_task.is_running():
        steam_sales_task.cancel()

@bot.event
async def on_disconnect():
    """Handle bot disconnect."""
    logger.info("Bot disconnected, cleaning up resources...")
    await cleanup_resources()

# ========== Bot Startup Function ==========
def start_discord_bot() -> None:
    """Start the Discord bot with proper error handling."""
    try:
        logger.info("Starting Discord bot...")
        bot.run(DISCORD_TOKEN)
    except discord.LoginFailure:
        logger.critical("Invalid Discord token! Check your .env file.")
        exit(1)
    except discord.HTTPException as e:
        logger.critical(f"Discord HTTP error: {e}")
        exit(1)
    except KeyboardInterrupt:
        logger.info("Bot shutdown requested by user.")
    except Exception as e:
        logger.critical(f"Bot startup failed: {e}")
        exit(1)
    finally:
        # Ensure cleanup happens
        import asyncio
        asyncio.run(cleanup_resources())

# ========== Application Entry Points ==========
if __name__ != "__main__":
    Thread(target=start_discord_bot, daemon=False).start()

application = app

if __name__ == "__main__":
    Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=int(os.getenv("PORT", 5000)),
            debug=False,
            use_reloader=False
        ),
        daemon=False
    ).start()
    
    # Start Discord bot in main thread when running directly
    start_discord_bot()="Game", value=game_data["name"], inline=False)
    embed.add_field(name="Discount", value=f"{discount_percent}%", inline=True)
    embed.add_field(name="Current Price", value=f"${current_price:.2f}", inline=True)
    embed.add_field(name="Original Price", value=f"${original_price:.2f}", inline=True)
    
    store_url = f"https://store.steampowered.com/app/{app_id}/"
    embed.add_field(name="Steam Store", value=f"[View on Steam]({store_url})", inline=False)
    
    if header_image := game_details.get("header_image"):
        embed.set_thumbnail(url=header_image)
    embed.timestamp = datetime.datetime.utcnow()
    
    await channel.send(embed=embed)

# ========== Steam Sales Task ==========
@tasks.loop(hours=6)
async def steam_sales_task():
    """Background task to check for Steam sales."""
    if steam_tracking_data:
        await check_steam_sales()

@steam_sales_task.before_loop
async def before_steam_sales_task():
    """Wait until bot is ready before starting the sales checking task."""
    await bot.wait_until_ready()

# ========== Trivia Classes ==========
class TriviaView(View):
    def __init__(self, user_id: int, options: List[str], correct: str, category: str, difficulty: str):
        super().__init__(timeout=Config.TRIVIA_TIMEOUT)
        self.user_id = user_id
        self.correct = correct
        self.options = options
        self.message = None
        self.category = category
        self.difficulty = difficulty
        
        for idx, text in enumerate(options, start=1):
            btn = Button(label=str(idx), style=discord.ButtonStyle.primary, custom_id=str(idx))
            btn.callback = self.create_callback(idx, text)
            self.add_item(btn)
    
    def create_callback(self, idx: int, option: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message(
                    "This isn't your question! Start your own trivia with /trivia", 
                    ephemeral=True
                )

            self.disable_all_buttons(idx)
            await interaction.message.edit(view=self)

            is_correct = option == self.correct
            await self.send_result(interaction, is_correct, option)
            self.stop()
        return callback
    
    def disable_all_buttons(self, selected_idx: int):
        """Disable all buttons and color them appropriately."""
        for child in self.children:
            child.disabled = True
            if self.options[int(child.custom_id)-1] == self.correct:
                child.style = discord.ButtonStyle.success
            elif child.custom_id == str(selected_idx):
                child.style = discord.ButtonStyle.danger
    
    async def send_result(self, interaction: discord.Interaction, is_correct: bool, selected_option: str):
        """Send the trivia result."""
        title = "🎉 Correct!" if is_correct else "❌ Incorrect"
        desc = (
            f"Well done, {interaction.user.mention}!"
            if is_correct else
            f"{interaction.user.mention} answered **{selected_option}**, but the correct answer was **{self.correct}**."
        )
        color = discord.Color.green() if is_correct else discord.Color.red()
        embed = create_embed(title, desc, color)
        embed.add_field(name="Difficulty", value=self.difficulty.capitalize(), inline=True)
        embed.add_field(name="Category", value=self.category, inline=True)
        
        await interaction.response.send_message(embed=embed)

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        
        for child in self.children:
            child.disabled = True
            if self.options[int(child.custom_id)-1] == self.correct:
                child.style = discord.ButtonStyle.success
        
        embed = self.message.embeds[0]
        embed.title = "⏰ Trivia Expired"
        embed.color = discord.Color.dark_gray()
        
        try:
            await self.message.edit(embed=embed, view=self)
            await self.message.reply(f"Time's up! The correct answer was **{self.correct}**.")
        except Exception as e:
            logger.error(f"Error updating expired trivia: {e}")

class TriviaSetupView(View):
    def __init__(self, interaction: discord.Interaction, categories: List[Dict[str, Any]]):
        super().__init__(timeout=Config.SETUP_TIMEOUT)
        self.interaction = interaction
        self.category = "0"
        self.difficulty = "any"

        # Category select
        category_options = [discord.SelectOption(label="Random", description="Any category", value="0")]
        for category in categories[:24]:  # Discord limit
            category_options.append(discord.SelectOption(label=category["name"], value=str(category["id"])))
        
        category_select = Select(placeholder="Select a category...", options=category_options)
        category_select.callback = self.category_callback
        self.add_item(category_select)

        # Difficulty select
        difficulty_options = [
            discord.SelectOption(label="Random", value="any"),
            discord.SelectOption(label="Easy", value="easy"),
            discord.SelectOption(label="Medium", value="medium"),
            discord.SelectOption(label="Hard", value="hard")
        ]
        difficulty_select = Select(placeholder="Select difficulty...", options=difficulty_options)
        difficulty_select.callback = self.difficulty_callback
        self.add_item(difficulty_select)

        start_button = Button(label="Start Trivia", style=discord.ButtonStyle.success)
        start_button.callback = self.start_callback
        self.add_item(start_button)
    
    async def category_callback(self, interaction: discord.Interaction) -> None:
        self.category = interaction.data['values'][0]
        await interaction.response.defer()

    async def difficulty_callback(self, interaction: discord.Interaction) -> None:
        self.difficulty = interaction.data['values'][0]
        await interaction.response.defer()

    async def start_callback(self, interaction: discord.Interaction) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await fetch_and_display_trivia(interaction, self.category, self.difficulty)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        try:
            await self.interaction.edit_original_response(view=self)
        except Exception as e:
            logger.warning(f"Could not update expired setup view: {e}")

# ========== Trivia Functions ==========
async def fetch_categories() -> List[Dict[str, Any]]:
    """Fetch trivia categories from API with fallback."""
    data = await safe_api_request(Config.TRIVIA_CATEGORIES_API)
    
    if data and "trivia_categories" in data:
        categories = data["trivia_categories"]
        logger.info(f"Fetched {len(categories)} trivia categories")
        return categories
    
    # Fallback categories
    logger.warning("Failed to fetch categories, using fallback")
    return [
        {"id": 9,  "name": "General Knowledge"},
        {"id": 21, "name": "Sports"},
        {"id": 22, "name": "Geography"},
        {"id": 23, "name": "History"}
    ]

async def fetch_and_display_trivia(interaction: discord.Interaction, category_id: str = "0", difficulty: str = "any") -> None:
    """Fetch and display a trivia question."""
    url = Config.TRIVIA_API
    params = {}
    
    if category_id != "0":
        params["category"] = category_id
    if difficulty != "any":
        params["difficulty"] = difficulty

    data = await safe_api_request(url, params)
    
    if not data or data.get("response_code") != 0 or not data.get("results"):
        return await interaction.followup.send("No trivia found. Try different settings.")

    result = data["results"][0]
    question = html.unescape(result["question"])
    correct = html.unescape(result["correct_answer"])
    incorrect = [html.unescape(a) for a in result["incorrect_answers"]]
    category = result["category"]
    difficulty_level = result["difficulty"]
    
    options = incorrect + [correct]
    random.shuffle(options)

    view = TriviaView(interaction.user.id, options, correct, category, difficulty_level)
    
    embed = create_embed(
        f"Trivia Time! ({difficulty_level.capitalize()})",
        question,
        discord.Color.blurple()
    )
    embed.set_author(name=category)
    embed.set_footer(text=f"Requested by {interaction.user.display_name}")

    for idx, option in enumerate(options, 1):
        embed.add_field(name=f"{idx}.", value=option, inline=False)

    msg = await interaction.followup.send(embed=embed, view=view)
    view.message = msg
    
    logger.info(f"Trivia question sent to user {interaction.user}")

# ========== Steam Commands ==========
class GameSelectView(View):
    def __init__(self, search_results: List[dict], guild_id: int, target_discount: int):
        super().__init__(timeout=60)
        self.search_results = search_results
        self.guild_id = guild_id
        self.target_discount = target_discount
        
        options = [
            discord.SelectOption(
                label=game["name"][:100], 
                description=f"App ID: {game['appid']}", 
                value=str(game["appid"])
            ) for game in search_results[:25]
        ]
        
        select = Select(placeholder="Select a game to track...", options=options)
        select.callback = self.select_game
        self.add_item(select)
    
    async def select_game(self, interaction: discord.Interaction):
        selected_app_id = interaction.data['values'][0]
        selected_game = next(game for game in self.search_results if str(game["appid"]) == selected_app_id)
        
        # Check if already tracked
        if selected_app_id in steam_tracking_data[self.guild_id]["games"]:
            return await interaction.response.send_message(
                f"**{selected_game['name']}** is already being tracked!", ephemeral=True
            )
        
        # Get game details
        game_details = await get_steam_game_details(selected_app_id)
        if not game_details:
            return await interaction.response.send_message(
                "This game is not available or has no price data.", ephemeral=True
            )
        
        # Add to tracking
        current_price = 0
        if price_overview := game_details.get("price_overview"):
            current_price = price_overview["final"] / 100
        
        steam_tracking_data[self.guild_id]["games"][selected_app_id] = {
            "name": selected_game["name"],
            "last_price": current_price,
            "target_discount": self.target_discount
        }
        
        embed = create_embed("✅ Game Added to Tracking", color=discord.Color.green())
        embed.add_field(name="Game", value=selected_game["name"], inline=False)
        embed.add_field(name="Target Discount", value=f"{self.target_discount}%", inline=True)
        embed.add_field(name="Current Price", value=f"${current_price:.2f}" if current_price > 0 else "Free", inline=True)
        embed.set_footer(text=f"App ID: {selected_app_id}")
        
        await interaction.response.edit_message(embed=embed, view=None)
        logger.info(f"Added game {selected_game['name']} to tracking for guild {self.guild_id}")
        self.stop()

@bot.tree.command(name="setup_steam_notifications", description="Set up Steam sale notifications for this channel.")
async def setup_steam_notifications_slash(interaction: discord.Interaction) -> None:
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "You need administrator permissions to set up Steam notifications!", ephemeral=True
        )
    
    guild_id = interaction.guild.id
    channel_id = interaction.channel.id
    
    if guild_id not in steam_tracking_data:
        steam_tracking_data[guild_id] = {"games": {}}
    
    steam_tracking_data[guild_id]["channel_id"] = channel_id
    
    embed = create_embed(
        "✅ Steam Notifications Setup Complete",
        f"Steam sale notifications will now be sent to {interaction.channel.mention}",
        discord.Color.green()
    )
    embed.add_field(name="Next Steps", value="Use `/add_steam_game` to start tracking games for sales!", inline=False)
    
    await interaction.response.send_message(embed=embed)
    logger.info(f"Steam notifications setup for guild {guild_id}, channel {channel_id}")

@bot.tree.command(name="add_steam_game", description="Add a Steam game to track for sales.")
async def add_steam_game_slash(interaction: discord.Interaction, game_name: str, target_discount: int = 0) -> None:
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "You need administrator permissions to manage tracked games!", ephemeral=True
        )
    
    if not (0 <= target_discount <= 90):
        return await interaction.response.send_message(
            "Target discount must be between 0% and 90%!", ephemeral=True
        )
    
    guild_id = interaction.guild.id
    
    if guild_id not in steam_tracking_data or "channel_id" not in steam_tracking_data[guild_id]:
        return await interaction.response.send_message(
            "Please run `/setup_steam_notifications` first!", ephemeral=True
        )
    
    # Check tracking limit
    current_games = len(steam_tracking_data[guild_id].get("games", {}))
    if current_games >= Config.MAX_TRACKED_GAMES:
        return await interaction.response.send_message(
            f"Maximum number of tracked games reached ({Config.MAX_TRACKED_GAMES})! Remove some games first.", 
            ephemeral=True
        )
    
    await interaction.response.defer()
    
    search_results = await search_steam_game(game_name)
    
    if not search_results:
        embed = create_embed(
            "❌ No Games Found",
            f"No Steam games found for '{game_name}'. Try:\n"
            "• Checking the spelling\n"
            "• Using a shorter or different name\n"
            "• Searching for the exact Steam store name",
            discord.Color.red()
        )
        return await interaction.followup.send(embed=embed)
    
    embed = create_embed(
        "🔍 Steam Game Search Results",
        f"Found {len(search_results)} games for '{game_name}'. Select one to track:",
        discord.Color.blue()
    )
    
    view = GameSelectView(search_results, guild_id, target_discount)
    await interaction.followup.send(embed=embed, view=view)

# ========== Basic Bot Commands ==========
@bot.tree.command(name="fact", description="Get a random fact.")
async def fact_slash(interaction: discord.Interaction) -> None:
    try:
        fact = randfacts.get_fact()
        await interaction.response.send_message(f"Did you know? {fact}")
        logger.info(f"Fact command used by {interaction.user}")
    except Exception as e:
        logger.error(f"Error getting fact: {e}")
        await interaction.response.send_message("Sorry, couldn't fetch a fact right now!")

@bot.tree.command(name="ping", description="Check the bot's latency.")
async def ping_slash(interaction: discord.Interaction) -> None:
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! Latency: {latency}ms")

@bot.tree.command(name="number", description="Generate a random number between two values.")
async def number_slash(interaction: discord.Interaction, min_num: int, max_num: int) -> None:
    if min_num > max_num:
        return await interaction.response.send_message(
            "Invalid range! First number must be ≤ second.", ephemeral=True
        )
    
    result = random.randint(min_num, max_num)
    await interaction.response.send_message(f"Here is your number: {result}")

@bot.tree.command(name="coin", description="Flip a coin.")
async def coin_slash(interaction: discord.Interaction) -> None:
    result = "Heads" if random.randint(0, 1) == 0 else "Tails"
    await interaction.response.send_message(result)

@bot.tree.command(name="trivia", description="Answer a multiple choice trivia question.")
async def trivia_slash(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    
    try:
        categories = await fetch_categories()
        view = TriviaSetupView(interaction, categories)
        embed = create_embed(
            "Trivia Setup",
            "Choose a category and difficulty for your trivia question!",
            discord.Color.blue()
        )
        await interaction.followup.send(embed=embed, view=view)
        logger.info(f"Trivia setup shown to {interaction.user}")
    except Exception as e:
        logger.error(f"Error in trivia setup: {e}")
        await interaction.followup.send("Sorry, couldn't start trivia right now!")

@bot.tree.command(name="weather", description="Look up the weather of your desired city.")
async def weather_slash(interaction: discord.Interaction, city: str) -> None:
    # Input validation
    if not city or len(city.strip()) < Config.MIN_CITY_NAME_LENGTH:
        return await interaction.response.send_message("Please provide a city name!", ephemeral=True)
    
    if len(city) > Config.MAX_CITY_NAME_LENGTH:
        return await interaction.response.send_message("City name too long! Please use a shorter name.", ephemeral=True)
    
    city = city.strip()
    await interaction.response.defer()
    
    try:
        async with python_weather.Client(unit=python_weather.IMPERIAL) as client:
            weather = await client.get(city)
            
            embed = await create_weather_embed(weather)
            await interaction.followup.send(embed=embed)
            logger.info(f"Weather data sent for {city} to user {interaction.user}")

    except RequestError as e:
        logger.error(f"Weather lookup error (status {e.status}): {str(e)}")
        await interaction.followup.send(f"Couldn't fetch weather for '{city}'. Server returned status code: {e.status}")
    except Error as e:
        logger.error(f"Weather lookup error: {str(e)}")
        await interaction.followup.send(f"Error getting weather for '{city}': {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error in weather command: {str(e)}")
        await interaction.followup.send(f"Couldn't fetch weather for '{city}'. Please try a valid city name.")

async def create_weather_embed(weather) -> discord.Embed:
    """Create weather embed with all information."""
    weather_emoji = getattr(weather.kind, 'emoji', '🌤️')
    
    title = f"{weather_emoji} Weather in {weather.location} - {weather.datetime.strftime('%A, %B %d')}"
    description = f"**{weather.description}**, {weather.temperature}°F"
    
    embed = create_embed(title, description)
    
    if weather.region and weather.country:
        embed.add_field(name="Location", value=f"{weather.region}, {weather.country}", inline=False)
    
    # Basic weather info
    embed.add_field(name="Feels Like", value=f"{weather.feels_like}°F", inline=True)
    embed.add_field(name="Humidity", value=f"{weather.humidity}%", inline=True)
    
    # Wind information
    wind_info = f"{weather.wind_speed} mph"
    if weather.wind_direction:
        wind_info += f" {str(weather.wind_direction)}"
        if hasattr(weather.wind_direction, "emoji"):
            wind_info += f" {weather.wind_direction.emoji}"
    embed.add_field(name"Wind", value=wind_info, inline=True)



            embed.add_field(name="Precipitation", value=f"{weather.precipitation} in", inline=True)

            embed.add_field(name="Pressure", value=f"{weather.pressure} in", inline=True)

            embed.add_field(name="Visibility", value=f"{weather.visibility} mi", inline=True)



            if weather.ultraviolet:

                uv_text = str(weather.ultraviolet)

                if hasattr(weather.ultraviolet, "index"):

                    uv_text = f"{uv_text} ({weather.ultraviolet.index})"

                embed.add_field(name="UV Index", value=uv_text, inline=True)



            if weather.daily_forecasts:

                forecast_text = ""



                for i, day_forecast in enumerate(weather.daily_forecasts[:3]):

                    if i == 0:

                        day_name = "Today"

                    elif i == 1:

                        day_name = "Tomorrow"

                    else:

                        if hasattr(day_forecast, 'date'):

                            day_name = day_forecast.date.strftime('%A')

                        else:

                            day_name = f"Day {i+1}"



                    day_emoji = "🌤️"

                    if hasattr(day_forecast, "kind") and hasattr(day_forecast.kind, "emoji"):

                        day_emoji = day_forecast.kind.emoji



                    day_text = f"{day_emoji} **{day_name}**: "



                    if hasattr(day_forecast, 'description'):

                        day_text += f"{day_forecast.description}, "

                    elif hasattr(day_forecast, 'kind'):

                        day_text += f"{day_forecast.kind}, "



                    logger.debug(f"Day {i} forecast attributes: {dir(day_forecast)}")



                    temp_high = None

                    if hasattr(day_forecast, 'highest'):

                        temp_high = day_forecast.highest

                    elif hasattr(day_forecast, 'high'):

                        temp_high = day_forecast.high

                    elif hasattr(day_forecast, 'temperature'):

                        temp_high = day_forecast.temperature



                    temp_low = None

                    if hasattr(day_forecast, 'lowest'):

                        temp_low = day_forecast.lowest

                    elif hasattr(day_forecast, 'low'):

                        temp_low = day_forecast.low



                    if temp_high is not None:

                        day_text += f"High: {temp_high}°F"

                        if temp_low is not None:

                            day_text += f", Low: {temp_low}°F"

                    else:

                        attrs = vars(day_forecast)

                        logger.debug(f"Day {i} forecast dict: {attrs}")



                        for attr_name, attr_value in attrs.items():

                            if 'temp' in attr_name.lower() or 'high' in attr_name.lower() or 'low' in attr_name.lower():

                                day_text += f"{attr_name}: {attr_value}°F, "



                    forecast_text += day_text + "\n"



                embed.add_field(name="Forecast", value=forecast_text, inline=False)



            embed.set_footer(text=f"Data provided by python_weather • {weather.datetime.strftime('%H:%M')}")



            await interaction.followup.send(embed=embed)

            logger.info(f"Weather data sent for {city} to user {interaction.user}")



    except RequestError as e:

        logger.error(f"Weather lookup error (status {e.status}): {str(e)}")

        await interaction.followup.send(f"Couldn't fetch weather for '{city}'. Server returned status code: {e.status}")

    except Error as e:

        logger.error(f"Weather lookup error: {str(e)}")

        await interaction.followup.send(f"Error getting weather for '{city}': {str(e)}")

    except Exception as e:

        logger.error(f"Unexpected error in weather command: {str(e)}")

        await interaction.followup.send(f"Couldn't fetch weather for '{city}'. Please try a valid city name.")



# ========== Bot Events ==========

@bot.event

async def on_ready() -> None:

    logger.info(f'{bot.user} has connected to Discord!')

    try:

        synced = await bot.tree.sync()

        logger.info(f'Synced {len(synced)} command(s)')

    except Exception as e:

        logger.error(f'Failed to sync commands: {e}')



@bot.event

async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:

    logger.error(f'Command error: {error}')



# ========== Bot Startup Function ==========

def start_discord_bot() -> None:


    steam_sales_task.start()

    """Start the Discord bot with proper error handling."""

    try:

        logger.info("Starting Discord bot...")

        bot.run(DISCORD_TOKEN)

    except discord.LoginFailure:

        logger.critical("Invalid Discord token! Check your .env file.")

        exit(1)

    except discord.HTTPException as e:

        logger.critical(f"Discord HTTP error: {e}")

        exit(1)

    except KeyboardInterrupt:

        logger.info("Bot shutdown requested by user.")

    except Exception as e:

        logger.critical(f"Bot startup failed: {e}")

        exit(1)



# ========== Application Entry Points ==========

if __name__ != "__main__":

    Thread(target=start_discord_bot, daemon=False).start()



application = app



if __name__ == "__main__":

    Thread(

        target=lambda: app.run(

            host="0.0.0.0",

            port=int(os.getenv("PORT", 5000)),

            debug=False,

            use_reloader=False

        ),

        daemon=False