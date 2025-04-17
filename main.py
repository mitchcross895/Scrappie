import os
import re
import logging
import random
import html
import requests
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button
from flask import Flask
from openai import OpenAI
import spotipy
from spotipy.oauth2 import SpotifyOAuth, SpotifyClientCredentials
from spellchecker import SpellChecker
import randfacts
from dotenv import load_dotenv
from threading import Thread

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s: %(message)s")

load_dotenv()
DISCORD_TOKEN        = os.getenv("DISCORD_TOKEN")
SPOTIFY_CLIENT_ID    = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET= os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = "http://localhost:8888/callback"
SPOTIFY_PLAYLIST_ID  = os.getenv("SPOTIFY_PLAYLIST_ID")
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY")

if not all([DISCORD_TOKEN, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_PLAYLIST_ID, OPENAI_API_KEY]):
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
    "Are you inventing a new language? Because that’s something else!",
    "Spell check is tapping out—maybe it's time for a lesson!"
]

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET,
    redirect_uri=SPOTIFY_REDIRECT_URI,
    scope="playlist-modify-public",
    cache_path=".spotify-token-cache"
))
client_credentials_manager = SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET
)
SPOTIFY_URL_REGEX = r"https?://open\.spotify\.com/track/([a-zA-Z0-9]+)"

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

class TriviaView(View):
    def __init__(self, user_id: int, options: list[str], correct: str):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.correct = correct
        for idx, text in enumerate(options, start=1):
            btn = Button(label=str(idx), style=discord.ButtonStyle.primary, custom_id=str(idx))
            async def callback(interaction: discord.Interaction, idx=idx):
                if interaction.user.id != self.user_id:
                    return await interaction.response.send_message(
                        "This isn't your question!", ephemeral=True
                    )
                for child in self.children:
                    child.disabled = True
                await interaction.message.edit(view=self)

                picked = options[idx-1]
                if picked == self.correct:
                    await interaction.response.send_message("🎉 Correct!")
                else:
                    await interaction.response.send_message(
                        f"❌ {interaction.user.mention} answered **{picked}**, but the correct was **{self.correct}**."
                    )
                self.stop()
            btn.callback = callback
            self.add_item(btn)
    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(view=self)
        except Exception:
            pass

@bot.tree.command(name="add", description="Add a song to the playlist using a URL or search query.")
@app_commands.describe(track="Spotify link or search query for a song.")
async def add_song(interaction: discord.Interaction, track: str):
    await interaction.response.defer()
    match = re.search(SPOTIFY_URL_REGEX, track)
    if match:
        track_id  = match.group(1)
        track_uri = f"spotify:track:{track_id}"
    else:
        result = search_song(track)
        if not result:
            return await interaction.followup.send("Couldn't find a track with that name.")
        track_uri = result["uri"]
        track_id  = result["id"]

    existing = sp.playlist_tracks(SPOTIFY_PLAYLIST_ID)
    if any(item['track']['id'] == track_id for item in existing['items']):
        return await interaction.followup.send("That track is already in the playlist! ✅")

    try:
        sp.playlist_add_items(SPOTIFY_PLAYLIST_ID, [track_uri])
        info = sp.track(track_id)
        await interaction.followup.send(
            f"Added **{info['name']}** by **{info['artists'][0]['name']}** to the playlist! 🎶"
        )
    except spotipy.exceptions.SpotifyException as e:
        logging.error(f"Spotify error: {e}")
        await interaction.followup.send(f"Failed to add song: {e}")

@bot.tree.command(name="fact", description="Get a random fact.")
async def fact_slash(interaction: discord.Interaction):
    await interaction.response.send_message(f"Did you know? {randfacts.get_fact()}")

@bot.tree.command(name="wiki", description="Search the Terraria Wiki for an entity page.")
async def wiki_slash(interaction: discord.Interaction, query: str):
    url = f"https://terraria.wiki.gg/wiki/{query.replace(' ', '_')}"
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
    try:
        res = requests.get("https://opentdb.com/api.php?amount=1&type=multiple")
        data = res.json()
        result = data["results"][0]
        question = html.unescape(result["question"])
        correct  = html.unescape(result["correct_answer"])
        incorrect= [html.unescape(ans) for ans in result["incorrect_answers"]]
        options  = incorrect + [correct]
        random.shuffle(options)

        view = TriviaView(interaction.user.id, options, correct)
        embed= discord.Embed(title="Trivia Time!", description=question)
        for idx, opt in enumerate(options, start=1):
            embed.add_field(name=f"{idx}.", value=opt, inline=False)
        msg= await interaction.followup.send(embed=embed, view=view)
        view.message = msg
    except Exception as e:
        logging.error(f"Trivia error: {e}")
        await interaction.followup.send("Sorry, couldn't fetch a trivia question right now.")

@bot.tree.command(name="ask", description="Ask OpenAI a question")
async def ask_slash(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    try:
        client= OpenAI(api_key=OPENAI_API_KEY)
        resp  = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":question}],
            max_tokens=50
        )
        await interaction.followup.send(resp.choices[0].message.content)
    except Exception as e:
        logging.error(f"OpenAI error: {e}")
        await interaction.followup.send("Sorry, couldn't process that request.")

def search_song(query: str):
    results= sp.search(q=query, type='track')
    return results['tracks']['items'][0] if results['tracks']['items'] else None

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    words= re.findall(r"[\w']+", message.content)
    miss = SPELL.unknown(words)
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

if __name__ == "__main__":
    Thread(target=lambda: app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=False,
        use_reloader=False
    )).start()
    bot.run(DISCORD_TOKEN)
