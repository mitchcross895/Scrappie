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
from openai import OpenAI
from spellchecker import SpellChecker
import randfacts    
from dotenv import load_dotenv
from threading import Thread
import python_weather
import asyncio
import datetime
from python_weather.errors import Error, RequestError

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s: %(message)s")

load_dotenv()
DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not all([DISCORD_TOKEN, OPENAI_API_KEY]):
    logging.error("Missing one or more required environment variables!")
    exit(1)

app = Flask(__name__)

@app.route('/')
def home():
    return "Discord Bot is Running!"

SPELL = SpellChecker()
SPELL.word_frequency.load_text_file(
    os.path.join(os.path.dirname(__file__), "addedwords.txt")
)
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

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

class TriviaView(View):
    def __init__(self, user_id: int, options: list[str], correct: str, category: str, difficulty: str):
        super().__init__(timeout=30)
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
            
            if option == self.correct:
                embed = discord.Embed(
                    title="🎉 Correct!", 
                    description=f"Well done, {interaction.user.mention}!",
                    color=discord.Color.green()
                )
            else:
                embed = discord.Embed(
                    title="❌ Incorrect",
                    description=f"{interaction.user.mention} answered **{option}**, but the correct answer was **{self.correct}**.",
                    color=discord.Color.red()
                )
            
            embed.add_field(name="Difficulty", value=self.difficulty.capitalize())
            embed.add_field(name="Category", value=self.category)
            
            await interaction.response.send_message(embed=embed)
            self.stop()
        
        return callback
    
    async def on_timeout(self):
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
            logging.error(f"Error updating expired trivia: {e}")


class TriviaCategorySelect(discord.ui.Select):
    def __init__(self, categories):
        options = [
            discord.SelectOption(label="Random", description="Any category", value="0")
        ]
        for category in categories[:24]:  
            options.append(discord.SelectOption(label=category["name"], value=str(category["id"])))
        super().__init__(
            placeholder="Select a category...",
            min_values=1,
            max_values=1,
            options=options
        )


class TriviaDifficultySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Random", description="Any difficulty", value="any"),
            discord.SelectOption(label="Easy",   description="Simple questions",   value="easy"),
            discord.SelectOption(label="Medium", description="Moderate difficulty", value="medium"),
            discord.SelectOption(label="Hard",   description="Challenging questions",value="hard"),
        ]
        super().__init__(
            placeholder="Select difficulty...",
            min_values=1,
            max_values=1,
            options=options
        )


class TriviaSetupView(View):
    def __init__(self, interaction: discord.Interaction, categories):
        super().__init__(timeout=60)
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
    
    async def category_callback(self, interaction: discord.Interaction):
        self.category = self.category_select.values[0]
        await interaction.response.defer()
    
    async def difficulty_callback(self, interaction: discord.Interaction):
        self.difficulty = self.difficulty_select.values[0]
        await interaction.response.defer()
    
    async def start_callback(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await fetch_and_display_trivia(interaction, category_id=self.category, difficulty=self.difficulty)
    
    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.interaction.edit_original_response(view=self)
        except Exception:
            pass


async def fetch_categories():
    """Fetch available trivia categories from the API"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://opentdb.com/api_category.php") as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("trivia_categories", [])
    except Exception as e:
        logging.error(f"Failed to fetch trivia categories: {e}")
    # Fallback if API is unavailable
    return [
        {"id": 9,  "name": "General Knowledge"},
        {"id": 21, "name": "Sports"},
        {"id": 22, "name": "Geography"},
        {"id": 23, "name": "History"}
    ]


async def fetch_and_display_trivia(interaction, category_id="0", difficulty="any"):
    """Fetch and display a trivia question with the given parameters"""
    url = "https://opentdb.com/api.php?amount=1&type=multiple"
    if category_id != "0":
        url += f"&category={category_id}"
    if difficulty != "any":
        url += f"&difficulty={difficulty}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return await interaction.followup.send(
                        "Sorry, the trivia service is unavailable right now. Please try again later."
                    )
                data = await response.json()
                if data["response_code"] != 0 or not data["results"]:
                    return await interaction.followup.send(
                        "No trivia questions found with those parameters. Try different options!"
                    )
                result    = data["results"][0]
                question  = html.unescape(result["question"])
                correct   = html.unescape(result["correct_answer"])
                incorrect = [html.unescape(ans) for ans in result["incorrect_answers"]]
                category  = result["category"]
                difficulty = result["difficulty"]

                options = incorrect + [correct]
                random.shuffle(options)

                view = TriviaView(interaction.user.id, options, correct, category, difficulty)
                embed = discord.Embed(
                    title=f"Trivia Time! ({difficulty.capitalize()})",
                    description=question,
                    color=discord.Color.blue()
                )
                embed.set_author(name=category)
                embed.set_footer(text=f"Requested by {interaction.user.display_name}")

                for idx, opt in enumerate(options, start=1):
                    embed.add_field(name=f"{idx}.", value=opt, inline=False)

                msg = await interaction.followup.send(embed=embed, view=view)
                view.message = msg
    except Exception as e:
        logging.error(f"Trivia error: {e}")
        await interaction.followup.send(
            "Sorry, something went wrong while fetching your trivia question. Please try again."
        )

@bot.tree.command(name="fact", description="Get a random fact.")
async def fact_slash(interaction: discord.Interaction):
    await interaction.response.send_message(f"Did you know? {randfacts.get_fact()}")

@bot.tree.command(name="wiki", description="Search the Terraria Wiki for an entity page.")
async def wiki_slash(interaction: discord.Interaction, query: str):
    url  = f"https://terraria.wiki.gg/wiki/{query.replace(' ', '_')}"
    resp = requests.get(url)
    if resp.status_code == 200:
        await interaction.response.send_message(f"Here's the page: {url}")
    else:
        await interaction.response.send_message(f"No page found for **{query}**.")

@bot.tree.command(name="ping", description="Check the bot's latency.")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message("pong")

@bot.tree.command(name="number", description="Generate a random number between two values.")
async def number_slash(interaction: discord.Interaction, min_num: int, max_num: int):
    if min_num > max_num:
        return await interaction.response.send_message(
            "Invalid range! First number must be ≤ second.", ephemeral=True
        )
    await interaction.response.send_message(f"Here is your number: {random.randint(min_num, max_num)}")

@bot.tree.command(name="coin", description="Flip a coin.")
async def coin_slash(interaction: discord.Interaction):
    await interaction.response.send_message("Heads" if random.randint(0,1)==0 else "Tails")

@bot.tree.command(name="trivia", description="Answer a multiple choice trivia question.")
async def trivia_slash(interaction: discord.Interaction):
    await interaction.response.defer()
    categories = await fetch_categories()
    view       = TriviaSetupView(interaction, categories)
    embed      = discord.Embed(
        title="Trivia Setup",
        description="Choose a category and difficulty for your trivia question!",
        color=discord.Color.blue()
    )
    await interaction.followup.send(embed=embed, view=view)

@bot.tree.command(name="ask", description="Ask OpenAI a question")
async def ask_slash(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp   = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":question}],
            max_tokens=50
        )
        await interaction.followup.send(resp.choices[0].message.content)
    except Exception as e:
        logging.error(f"OpenAI error: {e}")
        await interaction.followup.send("Sorry, couldn't process that request.")
\

@bot.tree.command(name="weather", description="Look up the weather of your desired city.")
async def weather_slash(interaction: discord.Interaction, city: str):
    await interaction.response.defer()
    try:
        async with python_weather.Client(unit=python_weather.IMPERIAL) as client:
            weather = await client.get(city)
            weather_emoji = "🌤️"  
            if hasattr(weather.kind, "emoji"):
                weather_emoji = weather.kind.emoji
            embed = discord.Embed(
                title=f"{weather_emoji} Weather in {weather.location} - {weather.datetime.strftime('%A, %B %d')}",
                description=f"**{weather.description}**, {weather.temperature}°F",
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
                    if hasattr(day_forecast, 'highest'):
                        day_text += f"High: {day_forecast.highest}°F, "
                    if hasattr(day_forecast, 'lowest'):
                        day_text += f"Low: {day_forecast.lowest}°F"
                    forecast_text += day_text + "\n"
                embed.add_field(name="Forecast", value=forecast_text, inline=False)
            embed.set_footer(text=f"Data provided by python_weather • {weather.datetime.strftime('%H:%M')}")
            
            await interaction.followup.send(embed=embed)

    except RequestError as e:
        logging.error(f"Weather lookup error (status {e.status}): {str(e)}")
        await interaction.followup.send(f"Couldn't fetch weather for '{city}'. Server returned status code: {e.status}")
    except Error as e:
        logging.error(f"Weather lookup error: {str(e)}")
        await interaction.followup.send(f"Error getting weather for '{city}': {str(e)}")
    except Exception as e:
        logging.error(f"Unexpected error in weather command: {str(e)}")
        await interaction.followup.send(f"Couldn't fetch weather for '{city}'. Please try a valid city name.")

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    words = re.findall(r"[\w']+", message.content)
    miss  = SPELL.unknown(words)
    if miss:
        logging.debug(f"Misspelled words detected: {miss}")
        await message.channel.send(random.choice(MISSPELL_REPLIES))
    await bot.process_commands(message)

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user}')
    try:
        await bot.tree.sync()
        logging.info("Slash commands synced.")
    except Exception as e:
        logging.error(f"Error syncing commands: {e}")

def start_discord_bot():
    """Run the Discord bot (blocking)."""
    bot.run(DISCORD_TOKEN)

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
    start_discord_bot()