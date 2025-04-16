import discord
import spotipy
import requests
from openai import OpenAI
from spotipy.oauth2 import SpotifyOAuth, SpotifyClientCredentials
from dotenv import load_dotenv
import re
import os
import logging
import random
import randfacts
from flask import Flask
from discord.ext import commands
from discord import app_commands
from spellchecker import SpellChecker

# Set up logging
logging.basicConfig(level=logging.INFO)
logging.basicConfig(level=logging.DEBUG)

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = "http://localhost:8888/callback"
SPOTIFY_PLAYLIST_ID = os.getenv("SPOTIFY_PLAYLIST_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not all([DISCORD_TOKEN, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_PLAYLIST_ID, OPENAI_API_KEY]):
    logging.error("Missing one or more required environment variables!")
    exit(1)

app = Flask(__name__)

@app.route('/')
def home():
    return "Discord Bot is Running!"

# Set up Spotify authentication
sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET,
    redirect_uri=SPOTIFY_REDIRECT_URI,
    scope="playlist-modify-public"
))

# Set up Discord bot with command handler
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True  # Ensure message content is enabled
bot = commands.Bot(command_prefix="!", intents=intents)

# Regular expression to match Spotify song links
SPOTIFY_URL_REGEX = r"https?://open\.spotify\.com/track/([a-zA-Z0-9]+)"

# Authenticate with the Spotify API using Client Credentials
client_credentials_manager = SpotifyClientCredentials(client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET)

# Slash command to add a song to the playlist
@bot.tree.command(name="add", description="Add a song to the playlist using a URL or search query.")
@app_commands.describe(track="Spotify link or search query for a song.")
async def add_song(interaction: discord.Interaction, track: str):
    await interaction.response.defer()  # prevent timeout

    # Check if it's a Spotify URL
    match = re.search(SPOTIFY_URL_REGEX, track)
    if match:
        track_id = match.group(1)
        track_uri = f"spotify:track:{track_id}"
    else:
        # Try searching for the track
        result = search_song(track)
        if not result:
            await interaction.followup.send("Couldn't find a track with that name.")
            return
        track_uri = result["uri"]
        track_id = result["id"]

    # Duplicate-check: verify if the track is already in the playlist
    existing_tracks = sp.playlist_tracks(SPOTIFY_PLAYLIST_ID)
    if any(item['track']['id'] == track_id for item in existing_tracks['items']):
        await interaction.followup.send("That track is already in the playlist! ✅")
        return

    try:
        sp.playlist_add_items(SPOTIFY_PLAYLIST_ID, [track_uri])
        track_info = sp.track(track_id)
        track_name = track_info['name']
        artist_name = track_info['artists'][0]['name']
        await interaction.followup.send(f"Added **{track_name}** by **{artist_name}** to the playlist! 🎶")
    except spotipy.exceptions.SpotifyException as e:
        logging.error(f"Spotify error: {e}")
        await interaction.followup.send(f"Spotify error: {str(e)}")
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        await interaction.followup.send(f"Failed to add song: {str(e)}")

# Helper function to search for a song by query
def search_song(query):
    results = sp.search(q=query, type='track')
    if results['tracks']['items']:
        return results['tracks']['items'][0]
    else:
        return None

# Slash command to get a random fact
@bot.tree.command(name="fact", description="Get a random fact.")
async def fact_slash(interaction: discord.Interaction):
    fact = randfacts.get_fact()
    await interaction.response.send_message(f"Did you know? {fact}")

# Slash command for Terraria Wiki search
@bot.tree.command(name="wiki", description="Search the Terraria Wiki for an entity page.")
async def wiki_slash(interaction: discord.Interaction, query: str):
    base_url = "https://terraria.wiki.gg/wiki/"
    query = query.replace(" ", "_")
    wiki_url = f"{base_url}{query}"

    response = requests.get(wiki_url)
    if response.status_code == 200:
        await interaction.response.send_message(f"Here's the Terraria Wiki page for **{query}**: {wiki_url}")
    else:
        await interaction.response.send_message(f"Sorry, {query} doesn't seem to exist. Maybe check your spelling, or try a different entity.")

# Slash command for ping
@bot.tree.command(name="ping", description="Check the bot's latency.")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message("pong")

# Slash command to generate a random number
@bot.tree.command(name="number", description="Generate a random number between two values.")
async def number_slash(interaction: discord.Interaction, min_num: int, max_num: int):
    if min_num > max_num:
        await interaction.response.send_message("Invalid range! The first number should be smaller than the second.", ephemeral=True)
        return

    random_number = random.randint(min_num, max_num)
    await interaction.response.send_message(f"Here is your number: {random_number}")

# Slash command to flip a coin
@bot.tree.command(name="coin", description="Flip a coin.")
async def coin_slash(interaction: discord.Interaction):
    result = "Heads" if random.randint(1, 2) == 1 else "Tails"
    await interaction.response.send_message(f"It was {result}!")

# Slash command to ask OpenAI a question
@bot.tree.command(name="ask", description="Ask OpenAI a question")
async def ask_slash(interaction: discord.Interaction, question: str):
    await interaction.response.defer()  # Prevent timeout
    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": question}],
            max_tokens=50
        )
        ai_reply = response.choices[0].message.content
        await interaction.followup.send(ai_reply)
    except Exception as e:
        logging.error(f"Error with OpenAI API: {e}")
        await interaction.followup.send("Sorry, I couldn't process that request.")

import os

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    sentence = message.content.strip()
    if not sentence:
        return 

    word_list = re.findall(r"[\w']+", sentence)
    spell = SpellChecker()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, "addedwords.txt")
    spell.word_frequency.load_text_file(file_path)

    misspelled = spell.unknown(word_list)
    if misspelled:
        logging.debug(f"Misspelled words detected: {', '.join(misspelled)}")
        number = random.randint(1, 10)
        if number == 1:
            response = "Let's try that again, shall we?"
        elif number == 2:
            response = "Great spelling, numb-nuts!"
        elif number == 3:
            response = "Learn to spell, Sandwich."
        elif number == 4:
            response = "Learn English, Torta."
        elif number == 5:
            response = "Read a book, Schmuck!"
        elif number == 6:
            response = "Seems like your dictionary took a vacation, pal!"
        elif number == 7:
            response = "Even your keyboard is questioning your grammar, genius."
        elif number == 8:
            response = "Autocorrect just waved the white flag, rookie."
        elif number == 9:
            response = "Are you inventing a new language? Because that’s something else!"
        elif number == 10:
            response = "Spell check is tapping out—maybe it's time for a lesson!"

        await message.channel.send(response)
    else:
        return

# Sync commands and log in
@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user}')
    try:
        await bot.tree.sync()  # Sync slash commands
        logging.info("Slash commands synced.")
    except Exception as e:
        logging.error(f"Error syncing commands: {e}")

# Run bot and Flask together
if __name__ == "__main__":
    from threading import Thread

    def run_flask():
        app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=False,         # or rely on FLASK_DEBUG env var
        use_reloader=False   # disable the reloader so you don’t hit signal-in-thread errors
    )

    Thread(target=run_flask).start()

    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        logging.error(f"Bot crashed with error: {e}")
