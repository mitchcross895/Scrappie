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

# Set up logging
logging.basicConfig(level=logging.INFO)

# Load environment variables
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = ("http://localhost:8888/callback")
SPOTIFY_PLAYLIST_ID = os.getenv("SPOTIFY_PLAYLIST_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not all([DISCORD_TOKEN, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_PLAYLIST_ID, OPENAI_API_KEY]):
    logging.error("Missing one or more required environment variables!")
    exit(1)

# Flask app (keeps Railway service alive)
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

# Authenticate with the Spotify API
client_credentials_manager = SpotifyClientCredentials(client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET)

# Slash command to add a song to the playlist
@bot.tree.command(name="add", description="Add a song to the playlist")
@app_commands.describe(track_url="The URL of the Spotify track to add.")
async def add_song(interaction: discord.Interaction, track_url: str):
    logging.info(f"Received add song command with track URL: {track_url}")
    match = re.search(SPOTIFY_URL_REGEX, track_url)

    if match:
        track_id = match.group(1)
        logging.info(f"Extracted track ID: {track_id}")
        try:
            sp.playlist_add_items(SPOTIFY_PLAYLIST_ID, [f"spotify:track:{track_id}"])
            await interaction.response.send_message("Track has been successfully added!")
            logging.info(f"Track {track_id} added to playlist.")
        except spotipy.exceptions.SpotifyException as e:
            logging.error(f"Error adding song to Spotify: {e}")
            await interaction.response.send_message(f"Failed to add song: {str(e)}.")
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            await interaction.response.send_message(f"Failed to add song: {str(e)}.")
    else:
        await interaction.response.send_message("Invalid Spotify track URL.")
        logging.warning(f"Invalid URL provided: {track_url}")

        # Search for a song
def search_song(query):
    results = sp.search(q=query, type='track')
    if results['tracks']['items']:
        return results['tracks']['items'][0]
    else:
        return None

    # Add a song to a playlist
def add_song_to_playlist(playlist_id, track_uri):
    sp.playlist_add_items(playlist_id, [track_uri])

# Regular command
@bot.command()
async def fact(ctx):
    await ctx.send("Here's a fact!")

# Slash command with a unique name
@bot.tree.command(name="fact", description="Get a random fact.")
async def random_fact_slash(interaction: discord.Interaction):
    fact = randfacts.get_fact()
    await interaction.response.send_message(f"Did you know? {fact}")

#Regular Command for Terraria Wiki
@bot.command()
async def wiki(ctx, *, query: str):
    base_url = "https://terraria.wiki.gg/wiki/"
    query = query.replace(" ", "_")
    wiki_url = f"{base_url}{query}"

    response = requests.get(wiki_url)
    if response.status_code == 200:
        await ctx.send(f"Here's the Terraria Wiki page for **{query}**: {wiki_url}")
    else:
        await ctx.send(f"Sorry, {query} doesn't seem to exist. Maybe check your spelling, or try a different entity.")

# Slash Command for wiki
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


# Message-based ping command
@bot.command()
async def ping(ctx):
    await ctx.send("pong")

# Slash command for ping
@bot.tree.command(name="ping")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message("pong")

#Regular command for radnom number generating
@bot.command()
async def number(ctx, min_num: int, max_num: int):
    if min_num > max_num:
        await ctx.send("Your small number should be SMALLER than your bigger number...")
        return

    random_number = random.randint(min_num, max_num)
    await ctx.send(f"Here is your number: {random_number}")

#Slash command for number
@bot.tree.command(name="number", description="Generate a random number between two values.")
async def number_slash(interaction: discord.Interaction, min_num: int, max_num: int):
    """Generates a random number between min_num and max_num."""
    if min_num > max_num:
        await interaction.response.send_message("Invalid range! The first number should be smaller than the second.", ephemeral=True)
        return

    random_number = random.randint(min_num, max_num)
    await interaction.response.send_message(f"Here is your number: {random_number}")

#Regular Command for flipping a coin
@bot.command()
async def coin(ctx):
    """Flips a coin and returns Heads or Tails."""
    result = "Heads" if random.randint(1, 2) == 1 else "Tails"
    await ctx.send(f"It was {result}!")

#Slash command for coin
@bot.tree.command(name="coin", description="Flip a coin.")
async def coin_slash(interaction: discord.Interaction):
    """Flips a coin and returns Heads or Tails."""
    result = "Heads" if random.randint(1, 2) == 1 else "Tails"
    await interaction.response.send_message(f"It was {result}!")

#Command for asking OpenAI a question
@bot.command()
async def ask(ctx, *, question: str):
    """Ask OpenAI a question"""
    try:
        client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY")
        )

        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": question}]
        )
        ai_reply = response.choices[0].message.content

        await ctx.send(ai_reply)  # Send the response to Discord
    except Exception as e:
        logging.error(f"Error with OpenAI API: {e}")
        await ctx.send("Sorry, I couldn't process that right now.")

#Slash command for ask
@bot.tree.command(name="ask", description="Ask OpenAI a question")
async def ask_slash(interaction: discord.Interaction, question: str):
    # Immediately defer the response to prevent timeout
    await interaction.response.defer()
    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": question}]
        )
        ai_reply = response.choices[0].message.content
        # Send the result as a followup message after deferring
        await interaction.followup.send(ai_reply)
    except Exception as e:
        logging.error(f"Error with OpenAI API: {e}")
        await interaction.followup.send("Sorry, I couldn't process that request.")


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
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
    
    Thread(target=run_flask).start()
    
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        logging.error(f"Bot crashed with error: {e}")
