import discord
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import re
import os
import logging
from flask import Flask
from discord.ext import commands

# Set up logging
logging.basicConfig(level=logging.INFO)

# Load environment variables
DISCORD_TOKEN = ("MTM1NDk0OTIwOTQ5Njc1MjE4OQ.Gb8KE3.q6d7M_KBVv-AS56kbG-Bw7k60_feZJPpf31h0E")
SPOTIFY_CLIENT_ID = ("261eb8789c39435aa9dfa8b877752b99")
SPOTIFY_CLIENT_SECRET = ("baed6fc2438545e1b6eb65ab44bba7b6")
SPOTIFY_REDIRECT_URI = ("http://localhost:8888/callback")
SPOTIFY_PLAYLIST_ID = ("2jDHNTwRjUcfL8bFnxbFhA")

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

# Regular expression to match Spotify song links
SPOTIFY_URL_REGEX = r"https?://open\.spotify\.com/track/([a-zA-Z0-9]+)"

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user}')

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