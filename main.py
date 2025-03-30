import discord
import spotify
from spotify import SpotifyOAuth
import re
import os
from flask import Flask

# Discord Bot Token
DISCORD_TOKEN = os.getenv("1354949209496752189")

# Spotify API Credentials
SPOTIFY_CLIENT_ID = os.getenv("261eb8789c39435aa9dfa8b877752b99")
SPOTIFY_CLIENT_SECRET = os.getenv("baed6fc2438545e1b6eb65ab44bba7b6")
SPOTIFY_REDIRECT_URI = os.getenv("http://localhost:8888/callback")
SPOTIFY_PLAYLIST_ID = os.getenv("2jDHNTwRjUcfL8bFnxbFhA?si=smxlSKdtQ9ekI1Bezu5WtQ")

# Flask app (keeps Railway service alive)
app = Flask(__name__)

@app.route('/')
def home():
    return "Discord Bot is Running!"

# Set up Spotify authentication
sp = spotify.Spotify(auth_manager=SpotifyOAuth(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET,
    redirect_uri=SPOTIFY_REDIRECT_URI,
    scope="playlist-modify-public"
))

# Set up Discord bot
intents = discord.Intents.default()
intents.messages = True

bot = discord.Client(intents=intents)

# Regular expression to match Spotify song links
SPOTIFY_URL_REGEX = r"https?://open\.spotify\.com/track/([a-zA-Z0-9]+)"

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    match = re.search(SPOTIFY_URL_REGEX, message.content)
    if match:
        track_id = match.group(1)
        try:
            sp.playlist_add_items(SPOTIFY_PLAYLIST_ID, [f"spotify:track:{track_id}"])
            await message.channel.send(f"✅ Added to playlist!")
        except Exception as e:
            await message.channel.send(f"❌ Failed to add song: {str(e)}")

# Run Flask and bot together
def run_bot():
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    from threading import Thread
    Thread(target=run_bot).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))