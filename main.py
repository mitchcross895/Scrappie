import discord
import spotipy
from spotipy.oauth2 import SpotifyOAuth
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

if not all([DISCORD_TOKEN, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_PLAYLIST_ID]):
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
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.tree.command(name="add", description="Add a song to the playlist")
@app_commands.describe(track_url="The URL of the Spotify track to add.")
async def add_song(interaction: discord.Interaction, track_url: str):
    match = re.search(SPOTIFY_URL_REGEX, track_url)
    if match:
        track_id = match.group(1)
        try:
            sp.playlist_add_items(SPOTIFY_PLAYLIST_ID, [f"spotify:track:{track_id}"])
            await interaction.response.send_message("Track has been successfully added!")
        except spotipy.exceptions.SpotifyException as e:
            logging.error(f"Error: {e}")
            await interaction.response.send_message(f"Failed to add song: {str(e)}.")
        except Exception as e:
            logging.error(f"Unexpected Error: {e}")
            await interaction.response.send_message(f"Failed to add song: {str(e)}.")
    else:
        await interaction.response.send_message("Invalid Spotify track URL.")


@bot.event
async def fact_slash(interaction: discord.Interaction):
    fact = randfacts.get_fact()
    await interaction.response.send_message(f"Did you know? {fact}")

# Regular expression to match Spotify song links
SPOTIFY_URL_REGEX = r"https?://open\.spotify\.com/track/([a-zA-Z0-9]+)"

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user}')
    try:
        await bot.tree.sync()  # Sync slash commands
        logging.info("Slash commands synced.")
    except Exception as e:
        logging.error(f"Error syncing commands: {e}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    
    match = re.search(SPOTIFY_URL_REGEX, message.content)
    if match:
        track_id = match.group(1)
        try:
            sp.playlist_add_items(SPOTIFY_PLAYLIST_ID, [f"spotify:track:{track_id}"])
            await message.channel.send("✅ Added to playlist!")
        except Exception as e:
            logging.error(f"Failed to add song: {e}")
            await message.channel.send(f"❌ Failed to add song: {str(e)}")
    
    await bot.process_commands(message)

# Message-based ping command
@bot.command()
async def ping(ctx):
    await ctx.send("pong")

# Slash command for ping
@bot.tree.command(name="ping")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message("pong")

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
