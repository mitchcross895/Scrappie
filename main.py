import os
import re
import logging
import random
import html
import requests
import discord
import aiohttp
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button, Select
from flask import Flask
from spellchecker import SpellChecker
import randfacts    
from dotenv import load_dotenv
from threading import Thread
import python_weather
import asyncio
import datetime
from python_weather.errors import Error, RequestError
from typing import Dict, Optional, Any, List

# ========== Configuration Constants ==========
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

# Timeout configurations
REQUEST_TIMEOUT = 10
TRIVIA_TIMEOUT = 30
SETUP_TIMEOUT = 60

# Limits for security
MAX_CITY_NAME_LENGTH = 100
MIN_CITY_NAME_LENGTH = 1

# ========== Logging Configuration ==========
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ========== Environment ==========
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

if not DISCORD_TOKEN:
    logger.critical("Missing DISCORD_TOKEN environment variable.")
    exit(1)

# Validate Discord token format (basic validation)
if not re.match(r'^[A-Za-z0-9._-]+$', DISCORD_TOKEN):
    logger.critical("Invalid Discord token format.")
    exit(1)

# ========== Flask App ==========
app = Flask(__name__)

@app.route('/')
def home() -> str:
    return "Discord Bot is Running!"

# ========== Spell Checker ==========
SPELL = SpellChecker()
try:
    SPELL.word_frequency.load_text_file(os.path.join(os.path.dirname(__file__), "addedwords.txt"))
except FileNotFoundError:
    logger.warning("addedwords.txt not found, using default dictionary only")

# ========== Discord Bot Setup ==========
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ========== Trivia Classes ==========
class TriviaView(View):
    def __init__(self, user_id: int, options: List[str], correct: str, category: str, difficulty: str):
        super().__init__(timeout=TRIVIA_TIMEOUT)
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

            for child in self.children:
                child.disabled = True
                if self.options[int(child.custom_id)-1] == self.correct:
                    child.style = discord.ButtonStyle.success
                elif child.custom_id == str(idx):
                    child.style = discord.ButtonStyle.danger
            
            await interaction.message.edit(view=self)

            correct = option == self.correct
            title = "🎉 Correct!" if correct else "❌ Incorrect"
            desc = (
                f"Well done, {interaction.user.mention}!"
                if correct else
                f"{interaction.user.mention} answered **{option}**, but the correct answer was **{self.correct}**."
            )
            color = discord.Color.green() if correct else discord.Color.red()
            embed = discord.Embed(title=title, description=desc, color=color)
            embed.add_field(name="Difficulty", value=self.difficulty.capitalize())
            embed.add_field(name="Category", value=self.category)
            
            await interaction.response.send_message(embed=embed)
            self.stop()
        return callback

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

class TriviaCategorySelect(discord.ui.Select):
    def __init__(self, categories: List[Dict[str, Any]]):
        options = [discord.SelectOption(label="Random", description="Any category", value="0")]
        for category in categories[:24]:  # Discord limit
            options.append(discord.SelectOption(label=category["name"], value=str(category["id"])))
        super().__init__(placeholder="Select a category...", min_values=1, max_values=1, options=options)

class TriviaDifficultySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Random", value="any"),
            discord.SelectOption(label="Easy", value="easy"),
            discord.SelectOption(label="Medium", value="medium"),
            discord.SelectOption(label="Hard", value="hard")
        ]
        super().__init__(placeholder="Select difficulty...", min_values=1, max_values=1, options=options)

class TriviaSetupView(View):
    def __init__(self, interaction: discord.Interaction, categories: List[Dict[str, Any]]):
        super().__init__(timeout=SETUP_TIMEOUT)
        self.interaction = interaction
        self.category = "0"
        self.difficulty = "any"

        self.category_select = TriviaCategorySelect(categories)
        self.category_select.callback = self.category_callback
        self.add_item(self.category_select)

        self.difficulty_select = TriviaDifficultySelect()
        self.difficulty_select.callback = self.difficulty_callback
        self.add_item(self.difficulty_select)

        self.start_button = Button(label="Start Trivia", style=discord.ButtonStyle.success)
        self.start_button.callback = self.start_callback
        self.add_item(self.start_button)
    
    async def category_callback(self, interaction: discord.Interaction) -> None:
        self.category = self.category_select.values[0]
        await interaction.response.defer()

    async def difficulty_callback(self, interaction: discord.Interaction) -> None:
        self.difficulty = self.difficulty_select.values[0]
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
    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://opentdb.com/api_category.php") as response:
                if response.status == 200:
                    data = await response.json()
                    categories = data.get("trivia_categories", [])
                    logger.info(f"Fetched {len(categories)} trivia categories")
                    return categories
                else:
                    logger.warning(f"Categories API returned status {response.status}")
    except Exception as e:
        logger.warning(f"Failed to fetch categories, using fallback: {e}")
    
    # Fallback categories
    return [
        {"id": 9,  "name": "General Knowledge"},
        {"id": 21, "name": "Sports"},
        {"id": 22, "name": "Geography"},
        {"id": 23, "name": "History"}
    ]

async def fetch_and_display_trivia(interaction: discord.Interaction, category_id: str = "0", difficulty: str = "any") -> None:
    """Fetch and display a trivia question."""
    url = "https://opentdb.com/api.php?amount=1&type=multiple"
    if category_id != "0":
        url += f"&category={category_id}"
    if difficulty != "any":
        url += f"&difficulty={difficulty}"

    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    logger.error(f"Trivia API returned status {response.status}")
                    return await interaction.followup.send("Trivia service is unavailable.")
                
                data = await response.json()
                if data["response_code"] != 0 or not data["results"]:
                    return await interaction.followup.send("No trivia found. Try different settings.")

                result = data["results"][0]
                question = html.unescape(result["question"])
                correct = html.unescape(result["correct_answer"])
                incorrect = [html.unescape(a) for a in result["incorrect_answers"]]
                category = result["category"]
                difficulty = result["difficulty"]
                
                options = incorrect + [correct]
                random.shuffle(options)

                view = TriviaView(interaction.user.id, options, correct, category, difficulty)
                
                title = f"Trivia Time! ({difficulty.capitalize()})"
                embed = discord.Embed(
                    title=title,
                    description=question,
                    color=discord.Color.blurple()
                )
                embed.set_author(name=category)
                embed.set_footer(text=f"Requested by {interaction.user.display_name}")

                for idx, option in enumerate(options, 1):
                    embed.add_field(name=f"{idx}.", value=option, inline=False)

                msg = await interaction.followup.send(embed=embed, view=view)
                view.message = msg
                
                logger.info(f"Trivia question sent to user {interaction.user}")
                
    except asyncio.TimeoutError:
        logger.error("Trivia API request timed out")
        await interaction.followup.send("Request timed out. Please try again.")
    except Exception as e:
        logger.error(f"Trivia error: {e}")
        await interaction.followup.send("An error occurred. Please try again.")

# ========== Bot Commands ==========
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
        embed = discord.Embed(
            title="Trivia Setup",
            description="Choose a category and difficulty for your trivia question!",
            color=discord.Color.blue()
        )
        await interaction.followup.send(embed=embed, view=view)
        logger.info(f"Trivia setup shown to {interaction.user}")
    except Exception as e:
        logger.error(f"Error in trivia setup: {e}")
        await interaction.followup.send("Sorry, couldn't start trivia right now!")

@bot.tree.command(name="weather", description="Look up the weather of your desired city.")
async def weather_slash(interaction: discord.Interaction, city: str) -> None:
    # Input validation
    if not city or len(city.strip()) < MIN_CITY_NAME_LENGTH:
        return await interaction.response.send_message(
            "Please provide a city name!", ephemeral=True
        )
    
    if len(city) > MAX_CITY_NAME_LENGTH:
        return await interaction.response.send_message(
            "City name too long! Please use a shorter name.", ephemeral=True
        )
    
    city = city.strip()
    await interaction.response.defer()
    
    try:
        async with python_weather.Client(unit=python_weather.IMPERIAL) as client:
            weather = await client.get(city)
            weather_emoji = "🌤️"  
            if hasattr(weather.kind, "emoji"):
                weather_emoji = weather.kind.emoji
        
            # Build title and description separately for readability
            title = f"{weather_emoji} Weather in {weather.location} - {weather.datetime.strftime('%A, %B %d')}"
            description = f"**{weather.description}**, {weather.temperature}°F"
            
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.blue()
            )
            
            if weather.region and weather.country:
                embed.add_field(name="Location", value=f"{weather.region}, {weather.country}", inline=False)
            
            embed.add_field(name="Feels Like", value=f"{weather.feels_like}°F", inline=True)
            embed.add_field(name="Humidity", value=f"{weather.humidity}%", inline=True)
            
            wind_info = f"{weather.wind_speed} mph"
            if weather.wind_direction:
                direction_str = str(weather.wind_direction)
                wind_info += f" {direction_str}"
                if hasattr(weather.wind_direction, "emoji"):
                    wind_info += f" {weather.wind_direction.emoji}"
            embed.add_field(name="Wind", value=wind_info, inline=True)
            
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
    ).start()