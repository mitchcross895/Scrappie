import os
import re
import logging
import random
import html
from typing import Dict, Optional, Any, List, Tuple
import asyncio
import datetime
from threading import Thread
from collections import deque
import signal
import sys
from pathlib import Path
from functools import wraps, lru_cache
import hmac
import hashlib
from datetime import datetime

# Third-party imports
import discord
import aiohttp
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, Button, Select
from flask import Flask, jsonify, request
import randfacts
from dotenv import load_dotenv
import python_weather
from python_weather.errors import Error, RequestError

try:
    import yt_dlp
    import discord.voice_client
    YT_DLP_AVAILABLE = True
    VOICE_AVAILABLE = True
    
    if not discord.opus.is_loaded():
        opus_libs = [
            'libopus.so.0', 'libopus.so', 'libopus.so.0.8.0', 'libopus.so.0.8',
            'opus', '/usr/lib/x86_64-linux-gnu/libopus.so.0',
            '/usr/lib/x86_64-linux-gnu/libopus.so',
            '/usr/lib/aarch64-linux-gnu/libopus.so.0',
            '/usr/lib/aarch64-linux-gnu/libopus.so',
            '/usr/local/lib/libopus.so.0', '/usr/local/lib/libopus.so',
        ]
        
        for opus_lib in opus_libs:
            try:
                discord.opus.load_opus(opus_lib)
                if discord.opus.is_loaded():
                    logging.info(f"Successfully loaded Opus from: {opus_lib}")
                    break
            except Exception:
                continue
        else:
            logging.error("Failed to load Opus library. Voice features disabled!")
            VOICE_AVAILABLE = False
    
except ImportError as e:
    YT_DLP_AVAILABLE = False
    VOICE_AVAILABLE = False
    logging.warning(f"yt-dlp or voice dependencies unavailable: {e}")

# ========== Configuration ==========
class Config:
    REQUEST_TIMEOUT = 15
    TRIVIA_TIMEOUT = 30
    SETUP_TIMEOUT = 60
    MAX_CITY_NAME_LENGTH = 100
    MIN_CITY_NAME_LENGTH = 1
    TRIVIA_CATEGORIES_API = "https://opentdb.com/api_category.php"
    TRIVIA_API = "https://opentdb.com/api.php?amount=1&type=multiple"
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    LOG_FILE = "bot.log"
    MAX_REQUESTS_PER_MINUTE = 30
    GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    GITHUB_CHANNEL_ID = int(os.getenv("GITHUB_CHANNEL_ID", "0"))

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

# ========== Logging Setup ==========
def setup_logging():
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(f"logs/{Config.LOG_FILE}"),
            logging.StreamHandler(sys.stdout)
        ]
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    return logging.getLogger(__name__)

logger = setup_logging()

# ========== Environment Setup ==========
load_dotenv()

def validate_environment():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.critical("Missing DISCORD_TOKEN environment variable.")
        sys.exit(1)
    if not re.match(r'^[A-Za-z0-9._-]+$', token):
        logger.critical("Invalid Discord token format.")
        sys.exit(1)
    return token

DISCORD_TOKEN = validate_environment()

# ========== Flask App ==========
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "status": "online",
        "bot_name": "Discord Bot",
        "version": "2.0",
        "timestamp": datetime.datetime.utcnow().isoformat()
    })

@app.route('/health')
def health_check():
    return jsonify({
        "status": "healthy",
        "uptime": str(datetime.datetime.utcnow() - start_time) if 'start_time' in globals() else "unknown"
    })

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404

# ========== Bot State ==========
class BotState:
    def __init__(self):
        self.start_time = datetime.datetime.utcnow()
        self.http_session: Optional[aiohttp.ClientSession] = None
        # spellchecker removed
        self.music_queues: Dict[str, deque] = {}
        self.request_counts: Dict[int, List] = {}
        self.now_playing: Dict[str, Optional[str]] = {}
        self._session_lock = asyncio.Lock()
        
    async def initialize(self):
        await self.get_http_session()
    
    async def get_http_session(self) -> aiohttp.ClientSession:
        if self.http_session is None or self.http_session.closed:
            async with self._session_lock:
                if self.http_session is None or self.http_session.closed:
                    timeout = aiohttp.ClientTimeout(total=Config.REQUEST_TIMEOUT)
                    self.http_session = aiohttp.ClientSession(
                        timeout=timeout,
                        headers={'User-Agent': Config.USER_AGENT}
                    )
        return self.http_session
    
    async def cleanup(self):
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
            logger.info("HTTP session closed")
    
    def is_rate_limited(self, user_id: int) -> bool:
        now = datetime.datetime.utcnow()
        if user_id not in self.request_counts:
            self.request_counts[user_id] = []
        
        cutoff = now - datetime.timedelta(minutes=1)
        self.request_counts[user_id] = [ts for ts in self.request_counts[user_id] if ts > cutoff]
        
        if len(self.request_counts[user_id]) >= Config.MAX_REQUESTS_PER_MINUTE:
            return True
        
        self.request_counts[user_id].append(now)
        return False

bot_state = BotState()
start_time = bot_state.start_time

# ========== Discord Bot Setup ==========
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
if VOICE_AVAILABLE:
    intents.voice_states = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    case_insensitive=True
)

# ========== Utility Functions ==========
def create_embed(title: str, description: str = None, color: discord.Color = discord.Color.blue(), **kwargs) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    for key in ('author', 'footer', 'thumbnail', 'image'):
        if key in kwargs:
            setter = getattr(embed, f'set_{key}')
            if key in ('author', 'footer'):
                setter(**kwargs[key])
            else:
                setter(url=kwargs[key])
    return embed

async def safe_api_request(url: str, params: dict = None, headers: dict = None) -> Optional[dict]:
    try:
        session = await bot_state.get_http_session()
        async with session.get(url, params=params, headers=headers or {}) as response:
            if response.status == 200:
                return await response.json()
            logger.error(f"API request failed: {url} returned {response.status}")
    except asyncio.TimeoutError:
        logger.error(f"API request timeout: {url}")
    except Exception as e:
        logger.error(f"API request error for {url}: {e}")
    return None

@lru_cache(maxsize=128)
def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds//60}m {seconds%60}s"
    return f"{seconds//3600}h {(seconds%3600)//60}m {seconds%60}s"

# ========== Rate Limiting Decorator ==========
def rate_limit(func):
    @wraps(func)
    async def wrapper(interaction: discord.Interaction, *args, **kwargs):
        if bot_state.is_rate_limited(interaction.user.id):
            embed = create_embed("Rate Limited", "You're making requests too quickly. Please wait.", discord.Color.red())
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        return await func(interaction, *args, **kwargs)
    return wrapper

# ========== Music System ==========
if YT_DLP_AVAILABLE and VOICE_AVAILABLE:
    ytdl_format_options = {
        'format': 'bestaudio[ext=webm]/bestaudio/best',
        'restrictfilenames': True,
        'noplaylist': False,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'ytsearch',
        'source_address': '0.0.0.0',
        'extract_flat': False,
        'geo_bypass': True,
        'nocache': True,
    }

    ytdl = yt_dlp.YoutubeDL(ytdl_format_options)
    
    async def search_ytdlp_async(query, ydl_opts):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))

    def _extract(query, ydl_opts):
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(query, download=False)

    class YTDLSource(discord.PCMVolumeTransformer):
        def __init__(self, source, *, data, volume=0.5):
            super().__init__(source, volume)
            self.data = data
            self.title = data.get('title')
            self.url = data.get('url')
            self.duration = data.get('duration')
        
        @classmethod
        async def from_url(cls, url, *, loop=None, stream=True):
            loop = loop or asyncio.get_event_loop()
            
            try:
                data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
            except Exception as e:
                logger.error(f"yt-dlp extraction failed for {url}: {e}")
                raise
            
            if 'entries' in data:
                data = data['entries'][0]
            
            filename = data['url'] if stream else ytdl.prepare_filename(data)
            ffmpeg_options = {
                'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -reconnect_at_eof 1 -multiple_requests 1',
                'options': '-vn -b:a 128k'
            }
            
            try:
                source = discord.FFmpegPCMAudio(filename, **ffmpeg_options)
            except Exception as e:
                logger.error(f"FFmpeg failed: {e}")
                raise
            
            return cls(source, data=data)

    async def is_voice_connected(voice_client, guild_id) -> bool:
        """Check if voice client is still valid and connected."""
        if not voice_client:
            return False
        if not voice_client.is_connected():
            return False
        if voice_client.guild.id != guild_id:
            return False
        return True

    async def play_next_song(voice_client, guild_key, channel):
        try:
            guild_id = int(guild_key)
            
            # Verify connection
            if not await is_voice_connected(voice_client, guild_id):
                logger.warning(f"Voice client not connected for guild {guild_key}")
                if guild_key in bot_state.music_queues:
                    bot_state.music_queues[guild_key].clear()
                if guild_key in bot_state.now_playing:
                    del bot_state.now_playing[guild_key]
                return
            
            # Check queue
            if guild_key not in bot_state.music_queues or not bot_state.music_queues[guild_key]:
                await asyncio.sleep(5)
                # Recheck after wait
                if (voice_client and voice_client.is_connected() and 
                    (guild_key not in bot_state.music_queues or not bot_state.music_queues[guild_key])):
                    await voice_client.disconnect()
                    if guild_key in bot_state.now_playing:
                        del bot_state.now_playing[guild_key]
                    embed = create_embed("👋 Queue Finished", "All songs played. Disconnecting...", discord.Color.blue())
                    await channel.send(embed=embed)
                return
            
            video_url, title = bot_state.music_queues[guild_key].popleft()
            
            # Attempt to play with retry logic
            max_retries = 2
            for attempt in range(max_retries):
                try:
                    player = await asyncio.wait_for(
                        YTDLSource.from_url(video_url, loop=bot.loop, stream=True),
                        timeout=45.0
                    )
                    
                    def after_play(error):
                        if error:
                            logger.error(f"Playback error for {title}: {error}")
                        
                        if hasattr(player, 'cleanup'):
                            try:
                                player.cleanup()
                            except Exception as e:
                                logger.error(f"Cleanup error: {e}")
                        
                        # Schedule next song
                        fut = asyncio.run_coroutine_threadsafe(
                            _schedule_next_if_connected(voice_client, guild_key, channel),
                            bot.loop
                        )
                        try:
                            fut.result(timeout=10)
                        except Exception as e:
                            logger.error(f"Error scheduling next: {e}")
                    
                    # Stop current playback if any
                    if voice_client.is_playing():
                        voice_client.stop()
                        await asyncio.sleep(0.5)
                    
                    # Verify still connected before playing
                    if not await is_voice_connected(voice_client, guild_id):
                        logger.warning("Disconnected before play")
                        return
                    
                    voice_client.play(player, after=after_play)
                    bot_state.now_playing[guild_key] = title
                    
                    # Send now playing message
                    embed = create_embed("🎵 Now Playing", f"**{title}**", discord.Color.blue())
                    if player.duration:
                        embed.add_field(name="Duration", value=format_duration(int(player.duration)), inline=True)
                    if bot_state.music_queues[guild_key]:
                        embed.add_field(name="Up Next", value=f"{len(bot_state.music_queues[guild_key])} songs", inline=True)
                    
                    await channel.send(embed=embed)
                    break  # Success, exit retry loop
                    
                except asyncio.TimeoutError:
                    logger.error(f"Timeout loading {title} (attempt {attempt + 1}/{max_retries})")
                    if attempt == max_retries - 1:
                        raise
                    await asyncio.sleep(2)
                    
                except Exception as e:
                    logger.error(f"Error on attempt {attempt + 1} for {title}: {e}")
                    if attempt == max_retries - 1:
                        raise
                    await asyncio.sleep(2)
            
        except Exception as e:
            logger.exception(f"Failed to play {title if 'title' in locals() else 'unknown'}: {e}")
            embed = create_embed("❌ Playback Error", 
                               f"Couldn't play **{title if 'title' in locals() else 'song'}**. Skipping...", 
                               discord.Color.red())
            await channel.send(embed=embed)
            
            # Try next song if queue not empty
            await asyncio.sleep(2)
            if guild_key in bot_state.music_queues and bot_state.music_queues[guild_key]:
                if voice_client and voice_client.is_connected():
                    await play_next_song(voice_client, guild_key, channel)
            elif voice_client and voice_client.is_connected():
                await voice_client.disconnect()
                if guild_key in bot_state.now_playing:
                    del bot_state.now_playing[guild_key]

    async def _schedule_next_if_connected(voice_client, guild_key, channel):
        guild_id = int(guild_key)
        if await is_voice_connected(voice_client, guild_id):
            await play_next_song(voice_client, guild_key, channel)

    @bot.tree.command(name="play", description="Play a song or add it to queue. Supports playlists!")
    @app_commands.describe(song_query="Search query or YouTube URL (supports playlists)")
    @rate_limit
    async def play_command(interaction: discord.Interaction, song_query: str):
        await interaction.response.defer()
        
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.followup.send("❌ You must be in a voice channel.")
        
        voice_channel = interaction.user.voice.channel
        voice_client = interaction.guild.voice_client
        
        if voice_client is None:
            try:
                voice_client = await voice_channel.connect(timeout=10.0, reconnect=True)
            except asyncio.TimeoutError:
                return await interaction.followup.send("❌ Failed to connect to voice channel. Try again.")
            except Exception as e:
                logger.error(f"Voice connection error: {e}")
                return await interaction.followup.send("❌ Couldn't connect to voice channel.")
        elif voice_channel != voice_client.channel:
            try:
                await voice_client.move_to(voice_channel)
            except Exception as e:
                logger.error(f"Voice move error: {e}")
                return await interaction.followup.send("❌ Couldn't move to your voice channel.")
        
        guild_key = str(interaction.guild_id)
        bot_state.music_queues.setdefault(guild_key, deque())
        
        is_url = song_query.startswith(('http://', 'https://'))
        is_playlist = 'playlist' in song_query.lower() or 'list=' in song_query
        
        try:
            if is_playlist and is_url:
                ydl_options_flat = {"extract_flat": True, "quiet": True, "no_warnings": True}
                results = await search_ytdlp_async(song_query, ydl_options_flat)
                
                if results.get("_type") == "playlist":
                    entries = [e for e in results.get("entries", []) if e]
                    
                    if not entries:
                        return await interaction.followup.send("❌ No videos in playlist.")
                    
                    embed = create_embed("📝 Adding Playlist", f"Processing {len(entries)} videos...", discord.Color.blue())
                    await interaction.followup.send(embed=embed)
                    
                    added = 0
                    for entry in entries:
                        try:
                            video_id = entry.get('id')
                            title = entry.get('title', 'Unknown')
                            if video_id:
                                bot_state.music_queues[guild_key].append((f"https://www.youtube.com/watch?v={video_id}", title))
                                added += 1
                        except Exception:
                            continue
                    
                    embed = create_embed("✅ Playlist Added", f"Added **{added}** songs!", discord.Color.green())
                    await interaction.channel.send(embed=embed)
                    
                    if not voice_client.is_playing() and not voice_client.is_paused():
                        await play_next_song(voice_client, guild_key, interaction.channel)
                    return
            
            query = f"ytsearch1:{song_query}" if not is_url else song_query
            ydl_options_info = {"extract_flat": False, "quiet": True, "no_warnings": True, "skip_download": True}
            results = await search_ytdlp_async(query, ydl_options_info)
            
            first_track = results['entries'][0] if 'entries' in results else results
            if not first_track:
                return await interaction.followup.send("❌ No results found.")
            
            video_url = first_track.get("webpage_url") or first_track.get("url")
            title = first_track.get("title", "Untitled")
            
            if not video_url:
                return await interaction.followup.send("❌ Could not get video URL.")
            
            bot_state.music_queues[guild_key].append((video_url, title))
            
            if voice_client.is_playing() or voice_client.is_paused():
                embed = create_embed("✅ Added to Queue", f"**{title}**", discord.Color.green())
                embed.add_field(name="Position", value=f"#{len(bot_state.music_queues[guild_key])}", inline=True)
                await interaction.followup.send(embed=embed)
            else:
                embed = create_embed("🎵 Starting Playback", f"**{title}**", discord.Color.blue())
                await interaction.followup.send(embed=embed)
                await play_next_song(voice_client, guild_key, interaction.channel)
                        
        except Exception as e:
            logger.error(f"Error in play command: {e}")
            await interaction.followup.send("❌ Couldn't play that. Try a different search.")

    @bot.tree.command(name="stop", description="Stop and clear queue.")
    async def stop_command(interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return await interaction.response.send_message("❌ Not in voice channel!", ephemeral=True)
        
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        
        guild_key = str(interaction.guild_id)
        if guild_key in bot_state.music_queues:
            bot_state.music_queues[guild_key].clear()
        if guild_key in bot_state.now_playing:
            del bot_state.now_playing[guild_key]
        
        embed = create_embed("⏹️ Music Stopped", "Playback stopped and queue cleared.", discord.Color.orange())
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="leave", description="Leave voice channel.")
    async def leave_command(interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return await interaction.response.send_message("❌ Not in voice channel!", ephemeral=True)
        
        channel_name = voice_client.channel.name
        await voice_client.disconnect()
        
        guild_key = str(interaction.guild_id)
        if guild_key in bot_state.music_queues:
            bot_state.music_queues[guild_key].clear()
        if guild_key in bot_state.now_playing:
            del bot_state.now_playing[guild_key]
        
        embed = create_embed("👋 Left Voice Channel", f"Disconnected from **{channel_name}**", discord.Color.blue())
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="queue", description="View music queue.")
    async def queue_command(interaction: discord.Interaction):
        guild_key = str(interaction.guild_id)
        voice_client = interaction.guild.voice_client
        is_playing = voice_client and (voice_client.is_playing() or voice_client.is_paused())
        current_song = bot_state.now_playing.get(guild_key)
        
        if not is_playing and (guild_key not in bot_state.music_queues or not bot_state.music_queues[guild_key]):
            embed = create_embed("📋 Queue Empty", "No songs. Use `/play` to add!", discord.Color.orange())
            return await interaction.response.send_message(embed=embed)
        
        embed = create_embed("🎵 Music Queue", "", discord.Color.blue())
        
        if is_playing and current_song:
            embed.add_field(name="▶️ Now Playing", value=current_song[:100], inline=False)
        
        if guild_key in bot_state.music_queues and bot_state.music_queues[guild_key]:
            queue = bot_state.music_queues[guild_key]
            for idx, (_, title) in enumerate(list(queue)[:10], 1):
                embed.add_field(name=f"#{idx}", value=title[:100], inline=False)
            
            if len(queue) > 10:
                embed.set_footer(text=f"... and {len(queue) - 10} more")
        
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="skip", description="Skip current song.")
    async def skip_command(interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return await interaction.response.send_message("❌ Not in voice channel!", ephemeral=True)
        
        if not voice_client.is_playing() and not voice_client.is_paused():
            return await interaction.response.send_message("❌ Nothing playing!", ephemeral=True)
        
        voice_client.stop()
        embed = create_embed("⏭️ Skipped", "Skipped to next song!", discord.Color.blue())
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="pause", description="Pause current song.")
    async def pause_command(interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return await interaction.response.send_message("❌ Not in voice channel!", ephemeral=True)
        
        if voice_client.is_paused():
            return await interaction.response.send_message("❌ Already paused!", ephemeral=True)
        
        if not voice_client.is_playing():
            return await interaction.response.send_message("❌ Nothing playing!", ephemeral=True)
        
        voice_client.pause()
        embed = create_embed("⏸️ Paused", "Music paused. Use `/resume` to continue.", discord.Color.orange())
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="resume", description="Resume paused song.")
    async def resume_command(interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return await interaction.response.send_message("❌ Not in voice channel!", ephemeral=True)
        
        if not voice_client.is_paused():
            return await interaction.response.send_message("❌ Nothing paused!", ephemeral=True)
        
        voice_client.resume()
        embed = create_embed("▶️ Resumed", "Music resumed!", discord.Color.green())
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="shuffle", description="Shuffle the music queue.")
    async def shuffle_command(interaction: discord.Interaction):
        guild_key = str(interaction.guild_id)
        queue = bot_state.music_queues.get(guild_key)

        if not queue or len(queue) == 0:
            embed = create_embed("📋 Queue Empty", "No songs to shuffle. Use `/play` to add.", discord.Color.orange())
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if len(queue) < 2:
            embed = create_embed("🔀 Nothing To Shuffle", "Need at least 2 songs to shuffle.", discord.Color.orange())
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        try:
            q_list = list(queue)
            random.shuffle(q_list)
            bot_state.music_queues[guild_key] = deque(q_list)

            embed = create_embed("🔀 Queue Shuffled", f"Shuffled **{len(q_list)}** songs.", discord.Color.green())

            upcoming = "\n".join(f"#{i+1} {t[1][:100]}" for i, t in enumerate(list(bot_state.music_queues[guild_key])[:5]))
            if upcoming:
                embed.add_field(name="Up Next", value=upcoming, inline=False)

            await interaction.response.send_message(embed=embed)
        except Exception as e:
            logger.error(f"Error shuffling queue: {e}")
            embed = create_embed("❌ Shuffle Error", "Couldn't shuffle the queue.", discord.Color.red())
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="nowplaying", description="Show currently playing song.")
    async def nowplaying_command(interaction: discord.Interaction):
        guild_key = str(interaction.guild_id)
        voice_client = interaction.guild.voice_client
        
        if not voice_client or not voice_client.is_connected():
            return await interaction.response.send_message("❌ Not in voice channel!", ephemeral=True)
        
        if not voice_client.is_playing() and not voice_client.is_paused():
            return await interaction.response.send_message("❌ Nothing playing!", ephemeral=True)
        
        current_song = bot_state.now_playing.get(guild_key, "Unknown")
        status = "⏸️ Paused" if voice_client.is_paused() else "▶️ Playing"
        
        embed = create_embed(f"{status}", f"**{current_song}**", discord.Color.blue())
        
        if guild_key in bot_state.music_queues and bot_state.music_queues[guild_key]:
            embed.add_field(name="Up Next", value=f"{len(bot_state.music_queues[guild_key])} songs in queue", inline=True)
        
        await interaction.response.send_message(embed=embed)

    # Voice keepalive task
    @tasks.loop(minutes=2)
    async def voice_keepalive():
        """Periodically check voice connections are healthy."""
        for guild in bot.guilds:
            if guild.voice_client and guild.voice_client.is_connected():
                guild_key = str(guild.id)
                # If stuck (not playing but has queue), restart
                if (not guild.voice_client.is_playing() and 
                    not guild.voice_client.is_paused() and
                    guild_key in bot_state.music_queues and 
                    bot_state.music_queues[guild_key]):
                    
                    logger.warning(f"Detected stuck player in guild {guild.name}, restarting")
                    try:
                        text_channel = guild.system_channel or guild.text_channels[0] if guild.text_channels else None
                        if text_channel:
                            await play_next_song(guild.voice_client, guild_key, text_channel)
                    except Exception as e:
                        logger.error(f"Failed to restart player: {e}")

    @voice_keepalive.before_loop
    async def before_voice_keepalive():
        await bot.wait_until_ready()

# ========== Trivia System ==========
class TriviaView(View):
    def __init__(self, user_id: int, question_data: dict):
        super().__init__(timeout=Config.TRIVIA_TIMEOUT)
        self.user_id = user_id
        self.question_data = question_data
        self.correct = html.unescape(question_data['correct_answer'])
        self.incorrect = [html.unescape(a) for a in question_data['incorrect_answers']]
        self.options = self.incorrect + [self.correct]
        random.shuffle(self.options)
        self.message = None
        self.answered = False
        
        for idx, option in enumerate(self.options, 1):
            btn = Button(label=str(idx), style=discord.ButtonStyle.primary, custom_id=f"option_{idx}", emoji="🔢")
            btn.callback = self.create_callback(idx, option)
            self.add_item(btn)
    
    def create_callback(self, idx: int, option: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("Not your question! Use `/trivia`.", ephemeral=True)
            if self.answered:
                return await interaction.response.send_message("Already answered!", ephemeral=True)
            self.answered = True
            await self.process_answer(interaction, idx, option)
        return callback
    
    async def process_answer(self, interaction: discord.Interaction, idx: int, selected_option: str):
        is_correct = selected_option == self.correct
        
        for child in self.children:
            child.disabled = True
            option_idx = int(child.custom_id.split('_')[1])
            option_text = self.options[option_idx - 1]
            
            if option_text == self.correct:
                child.style = discord.ButtonStyle.success
                child.emoji = "✅"
            elif option_idx == idx:
                child.style = discord.ButtonStyle.danger if not is_correct else discord.ButtonStyle.success
                child.emoji = "❌" if not is_correct else "✅"
            else:
                child.style = discord.ButtonStyle.secondary
        
        await interaction.response.edit_message(view=self)
        await self.send_result(interaction, is_correct, selected_option)
        self.stop()
    
    async def send_result(self, interaction: discord.Interaction, is_correct: bool, selected_option: str):
        title = "🎉 Correct!" if is_correct else "❌ Incorrect"
        desc = f"Well done, {interaction.user.mention}!" if is_correct else f"{interaction.user.mention}, correct: **{self.correct}**"
        color = discord.Color.green() if is_correct else discord.Color.red()
        
        embed = create_embed(title, desc, color)
        embed.add_field(name="Question", value=html.unescape(self.question_data['question']), inline=False)
        embed.add_field(name="Category", value=self.question_data['category'], inline=True)
        embed.add_field(name="Difficulty", value=self.question_data['difficulty'].capitalize(), inline=True)
        
        if not is_correct:
            embed.add_field(name="Your Answer", value=selected_option, inline=True)
        
        embed.set_footer(text="Use /trivia to play again!")
        await interaction.followup.send(embed=embed)

    async def on_timeout(self):
        if self.message is None or self.answered:
            return
        
        self.answered = True
        for child in self.children:
            child.disabled = True
            option_idx = int(child.custom_id.split('_')[1])
            if self.options[option_idx - 1] == self.correct:
                child.style = discord.ButtonStyle.success
                child.emoji = "✅"
            else:
                child.style = discord.ButtonStyle.secondary
        
        try:
            embed = self.message.embeds[0]
            embed.title = "⏰ Time's Up!"
            embed.color = discord.Color.dark_gray()
            embed.set_footer(text="Time's up! Use /trivia to try again.")
            await self.message.edit(embed=embed, view=self)
            await self.message.reply(f"⏰ Time's up! Answer: **{self.correct}**")
        except Exception as e:
            logger.error(f"Error updating expired trivia: {e}")

class TriviaSetupView(View):
    def __init__(self, interaction: discord.Interaction, categories: List[Dict[str, Any]]):
        super().__init__(timeout=Config.SETUP_TIMEOUT)
        self.interaction = interaction
        self.category = "0"
        self.difficulty = "any"
        self.categories = categories
        self.setup_components()
    
    def setup_components(self):
        category_options = [discord.SelectOption(label="Any Category", description="Random", value="0", emoji="🎲")]
        for cat in self.categories[:23]:
            category_options.append(discord.SelectOption(label=cat["name"][:100], value=str(cat["id"])))
        
        cat_select = Select(placeholder="🎯 Choose category...", options=category_options, custom_id="category_select")
        cat_select.callback = self.category_callback
        self.add_item(cat_select)
        
        diff_options = [
            discord.SelectOption(label="Any Difficulty", value="any", emoji="🎲"),
            discord.SelectOption(label="Easy", value="easy", emoji="🟢"),
            discord.SelectOption(label="Medium", value="medium", emoji="🟡"),
            discord.SelectOption(label="Hard", value="hard", emoji="🔴")
        ]
        
        diff_select = Select(placeholder="⚡ Choose difficulty...", options=diff_options, custom_id="difficulty_select")
        diff_select.callback = self.difficulty_callback
        self.add_item(diff_select)
        
        start_btn = Button(label="Start Trivia!", style=discord.ButtonStyle.success, emoji="🚀", custom_id="start_trivia")
        start_btn.callback = self.start_callback
        self.add_item(start_btn)
    
    async def category_callback(self, interaction: discord.Interaction):
        self.category = interaction.data['values'][0]
        selected = next((c["name"] for c in self.categories if str(c["id"]) == self.category), "Any Category")
        await interaction.response.send_message(f"📂 Selected: **{selected}**", ephemeral=True)
    
    async def difficulty_callback(self, interaction: discord.Interaction):
        self.difficulty = interaction.data['values'][0]
        display = self.difficulty.capitalize() if self.difficulty != "any" else "Any Difficulty"
        await interaction.response.send_message(f"⚡ Selected: **{display}**", ephemeral=True)
    
    async def start_callback(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        
        embed = create_embed("🔄 Loading Trivia...", "Fetching question...", discord.Color.yellow())
        await interaction.response.edit_message(embed=embed, view=self)
        await fetch_and_display_trivia(interaction, self.category, self.difficulty)
    
    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            embed = create_embed("⏰ Setup Expired", "Timed out. Use `/trivia` to try again.", discord.Color.dark_gray())
            await self.interaction.edit_original_response(embed=embed, view=self)
        except Exception as e:
            logger.warning(f"Could not update expired setup: {e}")

@lru_cache(maxsize=1)
async def fetch_categories() -> List[Dict[str, Any]]:
    data = await safe_api_request(Config.TRIVIA_CATEGORIES_API)
    
    if data and "trivia_categories" in data:
        return data["trivia_categories"]
    
    logger.warning("Failed to fetch categories, using fallback")
    return [
        {"id": 9, "name": "General Knowledge"},
        {"id": 10, "name": "Entertainment: Books"},
        {"id": 11, "name": "Entertainment: Film"},
        {"id": 12, "name": "Entertainment: Music"},
        {"id": 17, "name": "Science & Nature"},
        {"id": 18, "name": "Science: Computers"},
        {"id": 19, "name": "Science: Mathematics"},
        {"id": 21, "name": "Sports"},
        {"id": 22, "name": "Geography"},
        {"id": 23, "name": "History"},
        {"id": 27, "name": "Animals"}
    ]

async def fetch_and_display_trivia(interaction: discord.Interaction, category_id: str = "0", difficulty: str = "any"):
    params = {}
    if category_id != "0":
        params["category"] = category_id
    if difficulty != "any":
        params["difficulty"] = difficulty
    
    data = await safe_api_request(Config.TRIVIA_API, params)
    
    if not data or data.get("response_code") != 0 or not data.get("results"):
        embed = create_embed("😅 No Questions", "Try different settings!", discord.Color.orange())
        return await interaction.edit_original_response(embed=embed, view=None)
    
    question_data = data["results"][0]
    view = TriviaView(interaction.user.id, question_data)
    
    question_text = html.unescape(question_data["question"])
    category = question_data["category"]
    difficulty_level = question_data["difficulty"]
    
    difficulty_emojis = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}
    difficulty_emoji = difficulty_emojis.get(difficulty_level, "⚪")
    
    embed = create_embed("🧠 Trivia Question", f"**{question_text}**", discord.Color.blurple())
    embed.add_field(name="📂 Category", value=category, inline=True)
    embed.add_field(name=f"{difficulty_emoji} Difficulty", value=difficulty_level.capitalize(), inline=True)
    embed.add_field(name="⏱️ Time", value=f"{Config.TRIVIA_TIMEOUT}s", inline=True)
    
    for idx, option in enumerate(view.options, 1):
        embed.add_field(name=f"{idx}️⃣ Option {idx}", value=option, inline=False)
    
    embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    
    msg = await interaction.edit_original_response(embed=embed, view=view)
    view.message = msg

# ========== Bot Commands ==========
@bot.tree.command(name="help", description="Show available commands.")
async def help_command(interaction: discord.Interaction):
    embed = create_embed("🤖 Bot Commands", "All available commands:", discord.Color.blue())
    
    embed.add_field(
        name="🎲 Fun Commands",
        value="`/trivia` - Trivia questions\n`/fact` - Random fact\n`/coin` - Flip coin\n`/number <min> <max>` - Random number",
        inline=False
    )
    
    embed.add_field(
        name="🔧 Utility",
        value="`/weather <city>` - Weather info\n`/ping` - Bot latency\n`/help` - This message",
        inline=False
    )
    
    if YT_DLP_AVAILABLE and VOICE_AVAILABLE:
        embed.add_field(
            name="🎵 Music",
            value="`/play <song>` - Play music\n`/queue` - View queue\n`/skip` - Skip song\n`/stop` - Stop & clear\n`/leave` - Leave voice",
            inline=False
        )
    
    embed.set_footer(text="Use slash commands (/) to interact!")
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="fact", description="Get a random fact.")
@rate_limit
async def fact_command(interaction: discord.Interaction):
    await interaction.response.defer()
    
    try:
        fact = randfacts.get_fact()
        embed = create_embed("🧠 Random Fact", fact, discord.Color.green())
        embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"Error getting fact: {e}")
        embed = create_embed("❌ Error", "Couldn't fetch fact. Try again later.", discord.Color.red())
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="ping", description="Check bot latency.")
async def ping_command(interaction: discord.Interaction):
    start = datetime.datetime.utcnow()
    embed = create_embed("🏓 Pong!", "Checking...", discord.Color.yellow())
    await interaction.response.send_message(embed=embed)
    
    response_time = (datetime.datetime.utcnow() - start).total_seconds() * 1000
    ws_latency = round(bot.latency * 1000)
    
    embed = create_embed("🏓 Pong!", "Connection status:", discord.Color.green())
    embed.add_field(name="WebSocket", value=f"{ws_latency}ms", inline=True)
    embed.add_field(name="Response", value=f"{response_time:.1f}ms", inline=True)
    embed.add_field(name="Status", value="✅ Online", inline=True)
    
    uptime = datetime.datetime.utcnow() - bot_state.start_time
    embed.add_field(name="Uptime", value=format_duration(int(uptime.total_seconds())), inline=True)
    embed.add_field(name="Guilds", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Users", value=str(len(bot.users)), inline=True)
    
    await interaction.edit_original_response(embed=embed)

@bot.tree.command(name="number", description="Generate random number.")
@app_commands.describe(min_num="Minimum", max_num="Maximum")
async def number_command(interaction: discord.Interaction, min_num: int, max_num: int):
    if min_num > max_num:
        embed = create_embed("❌ Invalid Range", f"Min ({min_num}) must be ≤ max ({max_num}).", discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    
    if max_num - min_num > 1000000:
        embed = create_embed("❌ Range Too Large", "Max range: 1,000,000.", discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    
    result = random.randint(min_num, max_num)
    embed = create_embed("🎲 Random Number", f"Between **{min_num}** and **{max_num}**:", discord.Color.blue())
    embed.add_field(name="Result", value=f"**{result}**", inline=False)
    embed.set_footer(text=f"Generated for {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="coin", description="Flip a coin.")
async def coin_command(interaction: discord.Interaction):
    embed = create_embed("🪙 Flipping...", "Coin spinning...", discord.Color.yellow())
    await interaction.response.send_message(embed=embed)
    await asyncio.sleep(1)
    
    result = "Heads" if random.randint(0, 1) == 0 else "Tails"
    emoji = "👑" if result == "Heads" else "🎯"
    
    embed = create_embed("🪙 Coin Flip", f"Landed on **{result}**! {emoji}", discord.Color.green())
    embed.set_footer(text=f"Flipped by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    await interaction.edit_original_response(embed=embed)

@bot.tree.command(name="trivia", description="Play trivia game.")
@rate_limit
async def trivia_command(interaction: discord.Interaction):
    await interaction.response.defer()
    
    try:
        categories = await fetch_categories()
        view = TriviaSetupView(interaction, categories)
        
        embed = create_embed("🧠 Trivia Setup", "Configure your question:", discord.Color.blue())
        embed.add_field(name="📂 Categories", value=f"{len(categories)} available", inline=True)
        embed.add_field(name="⚡ Difficulties", value="Easy, Medium, Hard", inline=True)
        embed.add_field(name="⏱️ Time", value=f"{Config.TRIVIA_TIMEOUT}s", inline=True)
        embed.set_footer(text="Select preferences and click 'Start Trivia!'", icon_url=interaction.user.display_avatar.url)
        
        await interaction.followup.send(embed=embed, view=view)
    except Exception as e:
        logger.error(f"Error in trivia setup: {e}")
        embed = create_embed("❌ Setup Error", "Try again later.", discord.Color.red())
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="weather", description="Get weather information.")
@app_commands.describe(city="City name")
@rate_limit
async def weather_command(interaction: discord.Interaction, city: str):
    city = city.strip()
    if not city or len(city) < Config.MIN_CITY_NAME_LENGTH:
        embed = create_embed("❌ Invalid Input", "Provide valid city name!", discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    
    if len(city) > Config.MAX_CITY_NAME_LENGTH:
        embed = create_embed("❌ Too Long", f"Max {Config.MAX_CITY_NAME_LENGTH} chars!", discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    
    await interaction.response.defer()
    
    try:
        embed = create_embed("🔍 Fetching...", f"Getting weather for **{city}**...", discord.Color.yellow())
        await interaction.edit_original_response(embed=embed)
        
        async with python_weather.Client(unit=python_weather.IMPERIAL) as client:
            weather = await client.get(city)
            embed = create_weather_embed(weather, interaction.user)
            await interaction.edit_original_response(embed=embed)
            
    except RequestError as e:
        logger.error(f"Weather API error: {e}")
        embed = create_embed("🌐 API Error", f"Error for '{city}'. Check city name.", discord.Color.red())
        await interaction.edit_original_response(embed=embed)
    except Error as e:
        logger.error(f"Weather error: {e}")
        embed = create_embed("❌ Weather Error", f"Couldn't get data for '{city}'.", discord.Color.red())
        await interaction.edit_original_response(embed=embed)
    except Exception as e:
        logger.error(f"Unexpected weather error: {e}")
        embed = create_embed("💥 Error", "Something went wrong. Try later.", discord.Color.red())
        await interaction.edit_original_response(embed=embed)

def create_weather_embed(weather, user: discord.User) -> discord.Embed:
    weather_emoji = getattr(weather.kind, 'emoji', '🌤️')
    date_str = weather.datetime.strftime('%A, %B %d, %Y')
    
    title = f"{weather_emoji} Weather in {weather.location}"
    description = f"**{weather.description}** • **{weather.temperature}°F**\n{date_str}"
    
    temp = weather.temperature
    color = discord.Color.red() if temp >= 80 else discord.Color.orange() if temp >= 60 else discord.Color.blue() if temp >= 40 else discord.Color.dark_blue()
    
    embed = create_embed(title, description, color)
    
    if weather.region and weather.country:
        embed.add_field(name="📍 Location", value=f"{weather.region}, {weather.country}", inline=False)
    
    embed.add_field(name="🌡️ Temperature", value=f"{weather.temperature}°F", inline=True)
    embed.add_field(name="🤚 Feels Like", value=f"{weather.feels_like}°F", inline=True)
    embed.add_field(name="💧 Humidity", value=f"{weather.humidity}%", inline=True)
    
    wind_info = f"{weather.wind_speed} mph"
    if weather.wind_direction:
        direction = str(weather.wind_direction)
        if hasattr(weather.wind_direction, "emoji"):
            direction += f" {weather.wind_direction.emoji}"
        wind_info += f" {direction}"
    embed.add_field(name="💨 Wind", value=wind_info, inline=True)
    
    embed.add_field(name="🌧️ Precipitation", value=f"{weather.precipitation} in", inline=True)
    embed.add_field(name="🔽 Pressure", value=f"{weather.pressure} inHg", inline=True)
    
    if weather.visibility:
        embed.add_field(name="👁️ Visibility", value=f"{weather.visibility} mi", inline=True)
    
    if weather.ultraviolet:
        uv_text = str(weather.ultraviolet)
        if hasattr(weather.ultraviolet, "index"):
            uv_index = weather.ultraviolet.index
            uv_text = f"{uv_index}/10" + (" ⚠️" if uv_index >= 8 else " 🟡" if uv_index >= 6 else "")
        embed.add_field(name="☀️ UV Index", value=uv_text, inline=True)
    
    if weather.daily_forecasts:
        forecast_text = ""
        for i, day in enumerate(weather.daily_forecasts[:4]):
            day_name = "Today" if i == 0 else "Tomorrow" if i == 1 else day.date.strftime('%A') if hasattr(day, 'date') else f"Day {i+1}"
            emoji = getattr(getattr(day, 'kind', None), 'emoji', '🌤️')
            
            desc = day.description if hasattr(day, 'description') else str(day.kind) if hasattr(day, 'kind') else ""
            
            temp_high = getattr(day, 'highest', None) or getattr(day, 'high', None) or getattr(day, 'temperature', None)
            temp_low = getattr(day, 'lowest', None) or getattr(day, 'low', None)
            
            temp_info = f"H: {temp_high}°F, L: {temp_low}°F" if temp_high and temp_low else f"{temp_high}°F" if temp_high else ""
            
            forecast_text += f"{emoji} **{day_name}**: {desc}"
            if temp_info:
                forecast_text += f" • {temp_info}"
            forecast_text += "\n"
        
        if forecast_text:
            embed.add_field(name="📅 Forecast", value=forecast_text, inline=False)
    
    embed.set_footer(text=f"Requested by {user.display_name} • {weather.datetime.strftime('%I:%M %p')}", icon_url=user.display_avatar.url)
    return embed

# ========== Bot Events ==========
@bot.event
async def on_ready():
    logger.info(f'🤖 {bot.user} connected!')
    logger.info(f'📊 {len(bot.guilds)} guilds, {len(bot.users)} users')
    
    await bot_state.initialize()
    
    try:
        synced = await bot.tree.sync()
        logger.info(f'✅ Synced {len(synced)} commands')
    except Exception as e:
        logger.error(f'❌ Failed to sync: {e}')
    
    activity = discord.Activity(type=discord.ActivityType.listening, name=f"/help • {len(bot.guilds)} servers")
    await bot.change_presence(activity=activity)
    
    if not status_update_task.is_running():
        status_update_task.start()

    if not voice_keepalive.is_running():
        voice_keepalive.start()

@bot.event
async def on_guild_join(guild):
    logger.info(f'📥 Joined: {guild.name}')
    activity = discord.Activity(type=discord.ActivityType.listening, name=f"/help • {len(bot.guilds)} servers")
    await bot.change_presence(activity=activity)
    
    if guild.system_channel:
        embed = create_embed("👋 Hello!", f"Thanks for adding me!\n\nUse `/help` to see commands.", discord.Color.green())
        try:
            await guild.system_channel.send(embed=embed)
        except discord.Forbidden:
            pass

@bot.event
async def on_guild_remove(guild):
    logger.info(f'📤 Left: {guild.name}')
    guild_key = str(guild.id)
    bot_state.music_queues.pop(guild_key, None)
    activity = discord.Activity(type=discord.ActivityType.listening, name=f"/help • {len(bot.guilds)} servers")
    await bot.change_presence(activity=activity)

@bot.event
async def on_command_error(ctx, error):
    logger.error(f'Command error: {error}', exc_info=error)

@bot.event
async def on_application_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logger.error(f'Slash command error: {error}')
    
    if isinstance(error, app_commands.CommandOnCooldown):
        embed = create_embed("⏰ Cooldown", f"Try again in {error.retry_after:.1f}s.", discord.Color.orange())
    elif isinstance(error, app_commands.MissingPermissions):
        embed = create_embed("🔒 No Permission", "You can't use this command.", discord.Color.red())
    else:
        embed = create_embed("💥 Error", "Unexpected error. Try later.", discord.Color.red())
    
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Couldn't send error message: {e}")

@bot.event
async def on_voice_state_update(member, before, after):
    if not VOICE_AVAILABLE or member != bot.user:
        return
    
    if before.channel and not after.channel:
        guild_key = str(before.channel.guild.id)
        bot_state.music_queues.pop(guild_key, None)

@app.route('/github-webhook', methods=['POST'])
def github_webhook():
    """Handle GitHub webhook events."""
    try:
        # Verify webhook signature
        signature = request.headers.get('X-Hub-Signature-256', '')
        if Config.GITHUB_WEBHOOK_SECRET:
            if not verify_github_signature(request.data, signature, Config.GITHUB_WEBHOOK_SECRET):
                logger.warning("Invalid GitHub webhook signature")
                return jsonify({"error": "Invalid signature"}), 401
        
        event_type = request.headers.get('X-GitHub-Event', 'unknown')
        payload = request.json
        
        # Handle different event types
        if event_type == 'push':
            asyncio.run_coroutine_threadsafe(
                handle_push_event(payload),
                bot.loop
            )
        elif event_type == 'pull_request':
            asyncio.run_coroutine_threadsafe(
                handle_pr_event(payload),
                bot.loop
            )
        elif event_type == 'issues':
            asyncio.run_coroutine_threadsafe(
                handle_issue_event(payload),
                bot.loop
            )
        elif event_type == 'ping':
            return jsonify({"message": "Webhook received!"}), 200
        
        return jsonify({"status": "success"}), 200
        
    except Exception as e:
        logger.error(f"GitHub webhook error: {e}")
        return jsonify({"error": "Internal server error"}), 500

def verify_github_signature(payload_body, signature_header, secret):
    """Verify that the payload was sent from GitHub by validating SHA256."""
    if not signature_header:
        return False
    
    hash_object = hmac.new(
        secret.encode('utf-8'),
        msg=payload_body,
        digestmod=hashlib.sha256
    )
    expected_signature = "sha256=" + hash_object.hexdigest()
    
    return hmac.compare_digest(expected_signature, signature_header)

async def handle_push_event(payload):
    """Handle GitHub push events."""
    try:
        channel = bot.get_channel(Config.GITHUB_CHANNEL_ID)
        if not channel:
            logger.error(f"GitHub notification channel {Config.GITHUB_CHANNEL_ID} not found")
            return
        
        # Extract push information
        pusher = payload.get('pusher', {}).get('name', 'Unknown')
        repo_name = payload.get('repository', {}).get('full_name', 'Unknown Repo')
        repo_url = payload.get('repository', {}).get('html_url', '')
        ref = payload.get('ref', 'refs/heads/main').split('/')[-1]  # Branch name
        commits = payload.get('commits', [])
        compare_url = payload.get('compare', '')
        
        # Don't send notification if no commits
        if not commits:
            return
        
        # Create embed
        embed = discord.Embed(
            title=f"🔨 New Push to {repo_name}",
            description=f"**{pusher}** pushed {len(commits)} commit{'s' if len(commits) != 1 else ''} to `{ref}`",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )
        
        # Add repository info
        embed.add_field(
            name="📦 Repository",
            value=f"[{repo_name}]({repo_url})",
            inline=True
        )
        
        embed.add_field(
            name="🌿 Branch",
            value=f"`{ref}`",
            inline=True
        )
        
        embed.add_field(
            name="📊 Commits",
            value=f"{len(commits)}",
            inline=True
        )
        
        # Add commit details (up to 5 most recent)
        commit_details = []
        for commit in commits[:5]:
            sha = commit.get('id', '')[:7]  # Short SHA
            message = commit.get('message', 'No message').split('\n')[0][:100]  # First line only
            author = commit.get('author', {}).get('name', 'Unknown')
            commit_url = commit.get('url', '')
            
            commit_details.append(f"`{sha}` [{message}]({commit_url})\n👤 {author}")
        
        if commit_details:
            embed.add_field(
                name="📝 Commits",
                value="\n\n".join(commit_details),
                inline=False
            )
        
        if len(commits) > 5:
            embed.add_field(
                name="➕ More",
                value=f"... and {len(commits) - 5} more commit{'s' if len(commits) - 5 != 1 else ''}",
                inline=False
            )
        
        # Add compare link
        if compare_url:
            embed.add_field(
                name="🔗 Compare Changes",
                value=f"[View All Changes]({compare_url})",
                inline=False
            )
        
        # Set footer with pusher info
        embed.set_footer(
            text=f"Pushed by {pusher}",
            icon_url=payload.get('sender', {}).get('avatar_url', '')
        )
        
        await channel.send(embed=embed)
        logger.info(f"Sent GitHub push notification for {repo_name}")
        
    except Exception as e:
        logger.error(f"Error handling push event: {e}")

async def handle_pr_event(payload):
    """Handle GitHub pull request events."""
    try:
        channel = bot.get_channel(Config.GITHUB_CHANNEL_ID)
        if not channel:
            return
        
        action = payload.get('action', 'unknown')
        pr = payload.get('pull_request', {})
        repo_name = payload.get('repository', {}).get('full_name', 'Unknown Repo')
        
        # Only notify on opened, closed, or merged PRs
        if action not in ['opened', 'closed', 'reopened', 'merged']:
            return
        
        pr_number = pr.get('number', 0)
        pr_title = pr.get('title', 'No title')
        pr_url = pr.get('html_url', '')
        author = pr.get('user', {}).get('login', 'Unknown')
        avatar = pr.get('user', {}).get('avatar_url', '')
        
        # Determine color based on action
        color_map = {
            'opened': discord.Color.green(),
            'closed': discord.Color.red(),
            'reopened': discord.Color.orange(),
            'merged': discord.Color.purple()
        }
        color = color_map.get(action, discord.Color.blue())
        
        # Determine emoji
        emoji_map = {
            'opened': '🟢',
            'closed': '🔴',
            'reopened': '🟠',
            'merged': '🟣'
        }
        emoji = emoji_map.get(action, '📋')
        
        embed = discord.Embed(
            title=f"{emoji} Pull Request #{pr_number} {action.capitalize()}",
            description=f"**{pr_title}**",
            url=pr_url,
            color=color,
            timestamp=datetime.utcnow()
        )
        
        embed.add_field(name="📦 Repository", value=repo_name, inline=True)
        embed.add_field(name="👤 Author", value=author, inline=True)
        embed.add_field(name="🔢 PR Number", value=f"#{pr_number}", inline=True)
        
        embed.set_footer(text=f"Pull Request {action}", icon_url=avatar)
        
        await channel.send(embed=embed)
        logger.info(f"Sent GitHub PR notification for {repo_name} PR #{pr_number}")
        
    except Exception as e:
        logger.error(f"Error handling PR event: {e}")

async def handle_issue_event(payload):
    """Handle GitHub issue events."""
    try:
        channel = bot.get_channel(Config.GITHUB_CHANNEL_ID)
        if not channel:
            return
        
        action = payload.get('action', 'unknown')
        issue = payload.get('issue', {})
        repo_name = payload.get('repository', {}).get('full_name', 'Unknown Repo')
        
        # Only notify on opened or closed issues
        if action not in ['opened', 'closed', 'reopened']:
            return
        
        issue_number = issue.get('number', 0)
        issue_title = issue.get('title', 'No title')
        issue_url = issue.get('html_url', '')
        author = issue.get('user', {}).get('login', 'Unknown')
        avatar = issue.get('user', {}).get('avatar_url', '')
        
        color = discord.Color.green() if action == 'opened' else discord.Color.red()
        emoji = '🟢' if action == 'opened' else '🔴'
        
        embed = discord.Embed(
            title=f"{emoji} Issue #{issue_number} {action.capitalize()}",
            description=f"**{issue_title}**",
            url=issue_url,
            color=color,
            timestamp=datetime.utcnow()
        )
        
        embed.add_field(name="📦 Repository", value=repo_name, inline=True)
        embed.add_field(name="👤 Author", value=author, inline=True)
        embed.add_field(name="🔢 Issue Number", value=f"#{issue_number}", inline=True)
        
        embed.set_footer(text=f"Issue {action}", icon_url=avatar)
        
        await channel.send(embed=embed)
        logger.info(f"Sent GitHub issue notification for {repo_name} issue #{issue_number}")
        
    except Exception as e:
        logger.error(f"Error handling issue event: {e}")

# ========== Admin Commands for GitHub Setup ==========
@bot.tree.command(name="github-setup", description="Set the channel for GitHub notifications (Admin only)")
@app_commands.describe(channel="The channel to send GitHub notifications to")
@app_commands.default_permissions(administrator=True)
async def github_setup_command(interaction: discord.Interaction, channel: discord.TextChannel):
    """Set the GitHub notification channel."""
    try:
        # Update the config (you'll want to save this to a file or database in production)
        Config.GITHUB_CHANNEL_ID = channel.id
        
        embed = create_embed(
            "✅ GitHub Notifications Configured",
            f"GitHub notifications will be sent to {channel.mention}",
            discord.Color.green()
        )
        
        # Add webhook URL info
        webhook_url = f"{os.getenv('BOT_URL', 'https://your-bot-url.com')}/github-webhook"
        embed.add_field(
            name="📡 Webhook URL",
            value=f"```{webhook_url}```",
            inline=False
        )
        
        embed.add_field(
            name="⚙️ Setup Instructions",
            value=(
                "1. Go to your GitHub repo → Settings → Webhooks\n"
                "2. Click 'Add webhook'\n"
                "3. Paste the webhook URL above\n"
                "4. Set Content type to 'application/json'\n"
                "5. Add your webhook secret (if configured)\n"
                "6. Select events: Push, Pull Request, Issues\n"
                "7. Click 'Add webhook'"
            ),
            inline=False
        )
        
        await interaction.response.send_message(embed=embed)
        
        # Send test message to the channel
        test_embed = create_embed(
            "🎉 GitHub Notifications Active",
            "This channel will receive GitHub push notifications!",
            discord.Color.green()
        )
        await channel.send(embed=test_embed)
        
    except Exception as e:
        logger.error(f"Error in github-setup: {e}")
        embed = create_embed("❌ Setup Failed", "Couldn't configure GitHub notifications.", discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="github-test", description="Send a test GitHub notification (Admin only)")
@app_commands.default_permissions(administrator=True)
async def github_test_command(interaction: discord.Interaction):
    """Send a test GitHub notification."""
    try:
        channel = bot.get_channel(Config.GITHUB_CHANNEL_ID)
        if not channel:
            return await interaction.response.send_message(
                "❌ GitHub notification channel not configured. Use `/github-setup` first.",
                ephemeral=True
            )
        
        # Create test notification
        embed = discord.Embed(
            title="🔨 Test Push to your-repo/main",
            description="**TestUser** pushed 2 commits to `main`",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow()
        )
        
        embed.add_field(name="📦 Repository", value="[your-username/your-repo](https://github.com)", inline=True)
        embed.add_field(name="🌿 Branch", value="`main`", inline=True)
        embed.add_field(name="📊 Commits", value="2", inline=True)
        
        embed.add_field(
            name="📝 Commits",
            value=(
                "`abc1234` [Added new feature](https://github.com)\n👤 TestUser\n\n"
                "`def5678` [Fixed bug](https://github.com)\n👤 TestUser"
            ),
            inline=False
        )
        
        embed.set_footer(text="Pushed by TestUser • This is a test notification")
        
        await channel.send(embed=embed)
        await interaction.response.send_message(f"✅ Test notification sent to {channel.mention}", ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error in github-test: {e}")
        await interaction.response.send_message("❌ Failed to send test notification.", ephemeral=True)

# ========== Background Tasks ==========
@tasks.loop(minutes=30)
async def status_update_task():
    activities = [
        discord.Activity(type=discord.ActivityType.listening, name=f"/help • {len(bot.guilds)} servers"),
        discord.Activity(type=discord.ActivityType.playing, name="trivia games"),
        discord.Activity(type=discord.ActivityType.watching, name="the weather"),
    ]
    await bot.change_presence(activity=random.choice(activities))

# ========== Cleanup ==========
async def cleanup_resources():
    logger.info("🧹 Cleaning up...")
    await bot_state.cleanup()
    
    if VOICE_AVAILABLE:
        for guild in bot.guilds:
            if guild.voice_client:
                await guild.voice_client.disconnect()
    
    bot_state.music_queues.clear()
    logger.info("✅ Cleanup done")

def signal_handler(signum, frame):
    logger.info(f"📥 Signal {signum}, shutting down...")
    asyncio.create_task(cleanup_resources())
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

@bot.event
async def on_disconnect():
    logger.info("🔌 Bot disconnected")
    await cleanup_resources()

# ========== Startup ==========
def start_discord_bot():
    try:
        logger.info("🚀 Starting bot...")
        is_deployment = os.getenv("DEPLOYMENT") == "true" or "gunicorn" in os.environ.get("SERVER_SOFTWARE", "")
        
        if is_deployment:
            logger.info("🔧 Deployment mode")
            bot.run(DISCORD_TOKEN, log_handler=None)
        else:
            logger.info("🔧 Development mode")
            bot.run(DISCORD_TOKEN)
            
    except discord.LoginFailure:
        logger.critical("❌ Invalid token!")
        sys.exit(1)
    except discord.HTTPException as e:
        logger.critical(f"❌ Discord HTTP error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("⏹️ Shutdown requested")
    except Exception as e:
        logger.critical(f"💥 Startup failed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        try:
            asyncio.run(cleanup_resources())
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

# ========== Entry Point ==========
if __name__ != "__main__":
    Thread(target=start_discord_bot, daemon=False).start()

application = app

if __name__ == "__main__":
    import atexit
    atexit.register(lambda: asyncio.run(cleanup_resources()))
    
    Thread(
        target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False, use_reloader=False),
        daemon=True
    ).start()
    
    logger.info(f"🌐 Flask server on port {os.getenv('PORT', 5000)}")
    start_discord_bot()