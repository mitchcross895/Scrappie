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
from typing import Dict, Optional, Any

# ========== Logging Configuration ==========
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ========== Environment ==========
load_dotenv()
DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not all([DISCORD_TOKEN, OPENAI_API_KEY]):
    logging.critical("Missing required environment variables.")
    exit(1)

# ========== Flask App ==========
app = Flask(__name__)
@app.route('/')
def home():
    return "Discord Bot is Running!"

# ========== Spell Checker ==========
SPELL = SpellChecker()
SPELL.word_frequency.load_text_file(os.path.join(os.path.dirname(__file__), "addedwords.txt"))
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

# ========== Discord Bot Setup ==========
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ========== Trivia Classes ==========
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
                return await interaction.response.send_message("This isn't your question! Start your own trivia with /trivia", ephemeral=True)

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
        options = [discord.SelectOption(label="Random", description="Any category", value="0")]
        for category in categories[:24]:
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
        await fetch_and_display_trivia(interaction, self.category, self.difficulty)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.interaction.edit_original_response(view=self)
        except:
            pass

# ========== Trivia Functions ==========
async def fetch_categories() -> list[dict[str, Any]]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://opentdb.com/api_category.php") as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("trivia_categories", [])
    except Exception as e:
        logging.warning(f"Using fallback categories: {e}")
    return [
        {"id": 9,  "name": "General Knowledge"},
        {"id": 21, "name": "Sports"},
        {"id": 22, "name": "Geography"},
        {"id": 23, "name": "History"}
    ]

async def fetch_and_display_trivia(interaction, category_id="0", difficulty="any"):
    url = "https://opentdb.com/api.php?amount=1&type=multiple"
    if category_id != "0":
        url += f"&category={category_id}"
    if difficulty != "any":
        url += f"&difficulty={difficulty}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
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
                embed = discord.Embed(
                    title=f"Trivia Time! ({difficulty.capitalize()})",
                    description=question,
                    color=discord.Color.blurple()
                )
                embed.set_author(name=category)
                embed.set_footer(text=f"Requested by {interaction.user.display_name}")

                for idx, option in enumerate(options, 1):
                    embed.add_field(name=f"{idx}.", value=option, inline=False)

                msg = await interaction.followup.send(embed=embed, view=view)
                view.message = msg
    except Exception as e:
        logging.error(f"Trivia error: {e}")
        await interaction.followup.send("An error occurred. Please try again.")

# [Rest of your commands like /fact, /wiki, /weather, /ask, etc. are already solid and can remain unchanged.]

# ========== Events ==========
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    words = re.findall(r"[\w']+", message.content)
    miss = SPELL.unknown(words)
    if miss:
        logging.debug(f"Misspelled words detected: {miss}")
        await message.channel.send(random.choice(MISSPELL_REPLIES))
    await bot.process_commands(message)

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user} (ID: {bot.user.id})')
    try:
        await bot.tree.sync()
        logging.info("Slash commands synced.")
    except Exception as e:
        logging.error(f"Error syncing commands: {e}")

# ========== Start Bot ==========
def start_discord_bot():
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
