import os
import re
import logging
import random
import html
from typing import Dict, Optional, Any, List, Union
import asyncio
import datetime
from threading import Thread
from collections import deque
import json
import signal
import sys
from pathlib import Path

# Third-party imports
import discord
import aiohttp
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, Button, Select
from flask import Flask, jsonify
from spellchecker import SpellChecker
import randfacts
from dotenv import load_dotenv
import python_weather
from python_weather.errors import Error, RequestError

try:
    import yt_dlp
    import discord.voice_client  # This will fail if PyNaCl is not available
    YT_DLP_AVAILABLE = True
    VOICE_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False
    VOICE_AVAILABLE = False
    logging.warning("yt-dlp or voice dependencies not available. Music functionality will be disabled.")

# ========== Configuration Constants ==========
class Config:
    """Centralized configuration management."""
    # Timeout configurations
    REQUEST_TIMEOUT = 15
    TRIVIA_TIMEOUT = 30
    SETUP_TIMEOUT = 60
    
    # Limits for security
    MAX_CITY_NAME_LENGTH = 100
    MIN_CITY_NAME_LENGTH = 1
    MAX_SONG_QUERY_LENGTH = 200
    
    # API URLs
    TRIVIA_CATEGORIES_API = "https://opentdb.com/api_category.php"
    TRIVIA_API = "https://opentdb.com/api.php?amount=1&type=multiple"
    
    # User agent for API requests
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    
    # File paths
    ADDED_WORDS_FILE = "addedwords.txt"
    LOG_FILE = "bot.log"
    
    # Rate limiting
    MAX_REQUESTS_PER_MINUTE = 30

# Response constants
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

# ========== Enhanced Logging Configuration ==========
def setup_logging():
    """Setup comprehensive logging configuration."""
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    
    # Create logs directory if it doesn't exist
    Path("logs").mkdir(exist_ok=True)
    
    # Configure root logger
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=[
            logging.FileHandler(f"logs/{Config.LOG_FILE}"),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    # Set specific loggers
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    
    return logging.getLogger(__name__)

logger = setup_logging()

# ========== Environment Setup ==========
load_dotenv()

def validate_environment():
    """Validate required environment variables."""
    token = os.getenv("DISCORD_TOKEN")
    
    if not token:
        logger.critical("Missing DISCORD_TOKEN environment variable.")
        sys.exit(1)
    
    if not re.match(r'^[A-Za-z0-9._-]+$', token):
        logger.critical("Invalid Discord token format.")
        sys.exit(1)
    
    return token

DISCORD_TOKEN = validate_environment()

# ========== Enhanced Flask App ==========
app = Flask(__name__)

@app.route('/')
def home() -> str:
    return jsonify({
        "status": "online",
        "bot_name": "Discord Bot",
        "version": "2.0",
        "timestamp": datetime.datetime.utcnow().isoformat()
    })

@app.route('/health')
def health_check():
    """Health check endpoint for monitoring."""
    return jsonify({
        "status": "healthy",
        "uptime": str(datetime.datetime.utcnow() - start_time) if 'start_time' in globals() else "unknown"
    })

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404

# ========== Global Instances ==========
class BotState:
    """Centralized bot state management."""
    def __init__(self):
        self.start_time = datetime.datetime.utcnow()
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.spell_checker = None
        # Store tuples of (video_url/id, title)
        self.music_queues: Dict[str, deque] = {}
        self.request_counts: Dict[int, List] = {}
        self.now_playing: Dict[str, Optional[str]] = {}  # guild_id -> current song title
        
    async def initialize(self):
        """Initialize async components."""
        await self.get_http_session()
        self.setup_spell_checker()
    
    def setup_spell_checker(self):
        """Setup spell checker with custom words."""
        self.spell_checker = SpellChecker()
        try:
            words_file = Path(Config.ADDED_WORDS_FILE)
            if words_file.exists():
                self.spell_checker.word_frequency.load_text_file(str(words_file))
                logger.info(f"Loaded custom words from {Config.ADDED_WORDS_FILE}")
        except Exception as e:
            logger.warning(f"Could not load custom words: {e}")
    
    async def get_http_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session for reuse."""
        if self.http_session is None or self.http_session.closed:
            timeout = aiohttp.ClientTimeout(total=Config.REQUEST_TIMEOUT)
            self.http_session = aiohttp.ClientSession(
                timeout=timeout,
                headers={'User-Agent': Config.USER_AGENT}
            )
        return self.http_session
    
    async def cleanup(self):
        """Clean up resources."""
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
            logger.info("HTTP session closed")
    
    def is_rate_limited(self, user_id: int) -> bool:
        """Check if user is rate limited."""
        now = datetime.datetime.utcnow()
        if user_id not in self.request_counts:
            self.request_counts[user_id] = []
        
        # Clean old requests
        cutoff = now - datetime.timedelta(minutes=1)
        self.request_counts[user_id] = [
            ts for ts in self.request_counts[user_id] if ts > cutoff
        ]
        
        if len(self.request_counts[user_id]) >= Config.MAX_REQUESTS_PER_MINUTE:
            return True
        
        self.request_counts[user_id].append(now)
        return False

# Initialize bot state
bot_state = BotState()
start_time = bot_state.start_time

# Discord bot setup
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True

# Only enable voice intents if voice is available
if VOICE_AVAILABLE:
    intents.voice_states = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,  # We'll create a custom one
    case_insensitive=True
)

# ========== Utility Functions ==========
def create_embed(
    title: str, 
    description: str = None, 
    color: discord.Color = discord.Color.blue(),
    **kwargs
) -> discord.Embed:
    """Create a standard embed with additional options."""
    embed = discord.Embed(title=title, description=description, color=color)
    
    # Add optional fields
    if 'author' in kwargs:
        embed.set_author(**kwargs['author'])
    if 'footer' in kwargs:
        embed.set_footer(**kwargs['footer'])
    if 'thumbnail' in kwargs:
        embed.set_thumbnail(url=kwargs['thumbnail'])
    if 'image' in kwargs:
        embed.set_image(url=kwargs['image'])
    
    return embed

async def safe_api_request(url: str, params: dict = None, headers: dict = None) -> Optional[dict]:
    """Make a safe API request with comprehensive error handling."""
    try:
        session = await bot_state.get_http_session()
        request_headers = headers or {}
        
        async with session.get(url, params=params, headers=request_headers) as response:
            if response.status == 200:
                data = await response.json()
                logger.debug(f"API request successful: {url}")
                return data
            elif response.status == 429:
                logger.warning(f"Rate limited on API request: {url}")
                return None
            else:
                logger.error(f"API request failed: {url} returned {response.status}")
                return None
    except asyncio.TimeoutError:
        logger.error(f"API request timeout: {url}")
        return None
    except Exception as e:
        logger.error(f"API request error for {url}: {e}")
        return None

def format_duration(seconds: int) -> str:
    """Format duration in human-readable format."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds//60}m {seconds%60}s"
    else:
        return f"{seconds//3600}h {(seconds%3600)//60}m {seconds%60}s"

# ========== Rate Limiting Decorator ==========
def rate_limit(func):
    """Decorator to add rate limiting to commands."""
    import functools
    
    @functools.wraps(func)
    async def wrapper(interaction: discord.Interaction, *args: Any, **kwargs: Any) -> Any:
        if bot_state.is_rate_limited(interaction.user.id):
            embed = create_embed(
                "Rate Limited",
                "You're making requests too quickly. Please wait a moment.",
                discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        return await func(interaction, *args, **kwargs)
    
    # Copy the original function's annotations to the wrapper
    wrapper.__annotations__ = func.__annotations__.copy()
    return wrapper

# ========== Enhanced Music System ==========
if YT_DLP_AVAILABLE and VOICE_AVAILABLE:
    ytdl_format_options = {
        'format': 'bestaudio[ext=webm]/bestaudio/best',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': False,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'ytsearch',
        'source_address': '0.0.0.0',
        'extract_flat': False,
        'cookiefile': None,
        'age_limit': None,
        'geo_bypass': True,
        'nocache': True,
    }

    ytdl = yt_dlp.YoutubeDL(ytdl_format_options)
    
    # Helper functions for yt-dlp
    async def search_ytdlp_async(query, ydl_opts):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))

    def _extract(query, ydl_opts):
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(query, download=False)

    # YTDLSource class for music streaming
    class YTDLSource(discord.PCMVolumeTransformer):
        """Enhanced YTDL source with fresh URL extraction."""
        
        def __init__(self, source, *, data, volume=0.5):
            super().__init__(source, volume)
            self.data = data
            self.title = data.get('title')
            self.url = data.get('url')
            self.duration = data.get('duration')
        
        @classmethod
        async def from_url(cls, url, *, loop=None, stream=True):
            """Create source from URL with fresh extraction."""
            loop = loop or asyncio.get_event_loop()
            
            # Extract info in executor to avoid blocking
            try:
                data = await loop.run_in_executor(
                    None, 
                    lambda: ytdl.extract_info(url, download=not stream)
                )
            except Exception as e:
                logger.error(f"yt-dlp extraction failed for {url}: {e}")
                raise
            
            if 'entries' in data:
                # Playlist - take first item
                data = data['entries'][0]
            
            filename = data['url'] if stream else ytdl.prepare_filename(data)
            
            # FFmpeg options optimized for streaming with better error handling
            ffmpeg_options = {
                'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostats -loglevel error',
                'options': '-vn -ar 48000 -ac 2 -b:a 128k -bufsize 512k'
            }
            
            try:
                source = discord.FFmpegPCMAudio(filename, **ffmpeg_options, executable='ffmpeg')
            except Exception as e:
                logger.error(f"FFmpeg failed to create audio source: {e}")
                raise
            
            return cls(source, data=data)

    # Music playback functions
    async def play_next_song(voice_client, guild_key, channel):
        """Play the next song in the queue with fresh URL extraction."""
        try:
            # Check if voice client is still valid
            if not voice_client or not getattr(voice_client, "is_connected", lambda: False)():
                logger.info(f"Voice client disconnected for guild {guild_key}")
                return
            
            if guild_key not in bot_state.music_queues or not bot_state.music_queues[guild_key]:
                # No more songs, disconnect after a delay
                await asyncio.sleep(3)
                try:
                    if voice_client and getattr(voice_client, "is_connected", lambda: False)():
                        await voice_client.disconnect()
                        embed = create_embed(
                            "👋 Queue Finished",
                            "All songs have been played. Disconnecting...",
                            discord.Color.blue()
                        )
                        await channel.send(embed=embed)
                except Exception as e:
                    logger.error(f"Error disconnecting voice client: {e}")
                return
            
            # Get next song from queue
            video_url, title = bot_state.music_queues[guild_key].popleft()
            
            try:
                # Extract fresh audio URL right before playing
                logger.info(f"Extracting audio for: {title}")
                
                # Add timeout for extraction
                try:
                    player = await asyncio.wait_for(
                        YTDLSource.from_url(video_url, loop=bot.loop, stream=True),
                        timeout=30.0
                    )
                except asyncio.TimeoutError:
                    logger.error(f"Timeout extracting audio for {title}")
                    raise Exception("Audio extraction timed out")
                
                def after_play(error):
                    if error:
                        logger.error(f"Playback error for {title}: {error}")
                    
                    # Cleanup player
                    try:
                        if hasattr(player, 'cleanup'):
                            player.cleanup()
                    except Exception as cleanup_error:
                        logger.error(f"Error during player cleanup: {cleanup_error}")
                    
                    # Schedule the next song on the bot loop
                    coro = _schedule_next_if_connected(voice_client, guild_key, channel)
                    fut = asyncio.run_coroutine_threadsafe(coro, bot.loop)
                    try:
                        fut.result(timeout=5)
                    except Exception as e:
                        logger.error(f"Error scheduling next song: {e}")
                
                # Stop any current playback before starting new song
                if voice_client.is_playing():
                    voice_client.stop()
                
                voice_client.play(player, after=after_play)
                bot_state.now_playing[guild_key] = title
                
                # Send now playing message
                try:
                    embed = create_embed(
                        "🎵 Now Playing",
                        f"**{title}**",
                        discord.Color.blue()
                    )
                    
                    # Add duration if available
                    if player.duration:
                        embed.add_field(
                            name="Duration",
                            value=format_duration(int(player.duration)),
                            inline=True
                        )
                    
                    # Add queue info if there are more songs
                    if bot_state.music_queues[guild_key]:
                        embed.add_field(
                            name="Up Next",
                            value=f"{len(bot_state.music_queues[guild_key])} songs in queue",
                            inline=True
                        )
                    
                    await channel.send(embed=embed)
                except Exception as e:
                    logger.error(f"Error sending now playing message: {e}")
                    
            except Exception as e:
                logger.error(f"Error extracting/playing {title}: {e}", exc_info=True)
                # Send error message and try next song
                try:
                    embed = create_embed(
                        "❌ Playback Error",
                        f"Couldn't play **{title}**. Skipping to next song...",
                        discord.Color.red()
                    )
                    await channel.send(embed=embed)
                except:
                    pass
                
                # Try next song after a short delay
                await asyncio.sleep(1)
                if bot_state.music_queues.get(guild_key):
                    await play_next_song(voice_client, guild_key, channel)
                
        except Exception as e:
            logger.exception(f"Unexpected error in play_next_song: {e}")
            # Clear the queue on catastrophic failure
            if guild_key in bot_state.music_queues:
                bot_state.music_queues[guild_key].clear()
            
            try:
                embed = create_embed(
                    "💥 Fatal Error",
                    "Music playback encountered a fatal error. Queue has been cleared.",
                    discord.Color.dark_red()
                )
                await channel.send(embed=embed)
            except:
                pass

    async def _schedule_next_if_connected(voice_client, guild_key, channel):
        """Schedule next song if voice client is still connected."""
        if not voice_client or not getattr(voice_client, "is_connected", lambda: False)():
            return
        await play_next_song(voice_client, guild_key, channel)

    # Music commands
    @bot.tree.command(name="play", description="Play a song or add it to the queue. Supports playlists!")
    @app_commands.describe(song_query="Search query or YouTube URL (supports playlists)")
    @rate_limit
    async def play_command(interaction: discord.Interaction, song_query: str):
        await interaction.response.defer()
        
        voice_channel = interaction.user.voice
        if voice_channel is None or voice_channel.channel is None:
            await interaction.followup.send("❌ You must be in a voice channel.")
            return
        
        voice_channel = voice_channel.channel
        voice_client = interaction.guild.voice_client
        
        if voice_client is None:
            voice_client = await voice_channel.connect()
        elif voice_channel != voice_client.channel:
            await voice_client.move_to(voice_channel)
        
        guild_key = str(interaction.guild_id)
        if guild_key not in bot_state.music_queues:
            bot_state.music_queues[guild_key] = deque()
        
        # Check if it's a URL
        is_url = song_query.startswith(('http://', 'https://'))
        is_playlist = 'playlist' in song_query.lower() or 'list=' in song_query
        
        try:
            # Handle playlist
            if is_playlist and is_url:
                ydl_options_flat = {
                    "extract_flat": True,
                    "quiet": True,
                    "no_warnings": True,
                }
                
                results = await search_ytdlp_async(song_query, ydl_options_flat)
                
                if results.get("_type") != "playlist":
                    is_playlist = False
                else:
                    entries = results.get("entries", [])
                    valid_entries = [e for e in entries if e is not None]
                    
                    if not valid_entries:
                        await interaction.followup.send("❌ No videos found in this playlist.")
                        return
                    
                    playlist_title = results.get("title", "Unknown Playlist")
                    
                    embed = create_embed(
                        "📝 Adding Playlist to Queue",
                        f"Processing **{playlist_title}**\nFound {len(valid_entries)} videos...",
                        discord.Color.blue()
                    )
                    await interaction.followup.send(embed=embed)
                    
                    added_count = 0
                    for entry in valid_entries:
                        try:
                            video_id = entry.get('id')
                            title = entry.get('title', 'Unknown')
                            
                            if video_id:
                                video_url = f"https://www.youtube.com/watch?v={video_id}"
                                # Store video URL and title only
                                bot_state.music_queues[guild_key].append((video_url, title))
                                added_count += 1
                        except Exception as e:
                            logger.warning(f"Failed to add song from playlist: {e}")
                            continue
                    
                    embed = create_embed(
                        "✅ Playlist Added",
                        f"Added **{added_count}** songs from **{playlist_title}** to the queue!",
                        discord.Color.green()
                    )
                    await interaction.channel.send(embed=embed)
                    
                    if not voice_client.is_playing() and not voice_client.is_paused():
                        await play_next_song(voice_client, guild_key, interaction.channel)
                    
                    return
            
            # Handle single track
            if not is_url:
                query = "ytsearch1:" + song_query
            else:
                query = song_query
            
            # Just extract basic info, not the audio URL
            ydl_options_info = {
                "extract_flat": False,
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
            }
            
            results = await search_ytdlp_async(query, ydl_options_info)
            
            if 'entries' in results:
                if not results['entries']:
                    await interaction.followup.send("❌ No results found.")
                    return
                first_track = results['entries'][0]
            else:
                first_track = results
            
            video_url = first_track.get("webpage_url") or first_track.get("url")
            title = first_track.get("title", "Untitled")
            
            if not video_url:
                await interaction.followup.send("❌ Could not get video URL.")
                return
            
            # Store video URL and title
            bot_state.music_queues[guild_key].append((video_url, title))
            
            if voice_client.is_playing() or voice_client.is_paused():
                embed = create_embed(
                    "✅ Added to Queue",
                    f"**{title}**",
                    discord.Color.green()
                )
                embed.add_field(
                    name="Position in Queue",
                    value=f"#{len(bot_state.music_queues[guild_key])}",
                    inline=True
                )
                await interaction.followup.send(embed=embed)
            else:
                embed = create_embed(
                    "🎵 Starting Playback",
                    f"**{title}**",
                    discord.Color.blue()
                )
                await interaction.followup.send(embed=embed)
                await play_next_song(voice_client, guild_key, interaction.channel)
                        
        except Exception as e:
            logger.error(f"Error in play command: {e}", exc_info=True)
            await interaction.followup.send(
                "❌ Sorry, I couldn't play that song or playlist. Please try a different search term or URL."
            )

    @bot.tree.command(name="stop", description="Stop the current song and clear the queue.")
    async def stop_command(interaction: discord.Interaction):
        """Stop music and clear queue."""
        voice_client = interaction.guild.voice_client
        
        if not voice_client or not getattr(voice_client, "is_connected", lambda: False)():
            return await interaction.response.send_message(
                "❌ I'm not connected to a voice channel!", 
                ephemeral=True
            )
        
        # Stop current playback if any
        try:
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()
        except Exception as e:
            logger.error(f"Error stopping playback: {e}")
        
        # Clear queue using string key
        guild_key = str(interaction.guild_id)
        if guild_key in bot_state.music_queues:
            bot_state.music_queues[guild_key].clear()
        
        embed = create_embed(
            "⏹️ Music Stopped",
            "Playback stopped and queue cleared.",
            discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="leave", description="Leave the voice channel.")
    async def leave_command(interaction: discord.Interaction):
        """Leave voice channel."""
        voice_client = interaction.guild.voice_client
        
        if not voice_client or not getattr(voice_client, "is_connected", lambda: False)():
            return await interaction.response.send_message(
                "❌ I'm not in a voice channel!", 
                ephemeral=True
            )
        
        channel_name = getattr(voice_client.channel, "name", "Unknown")
        try:
            await voice_client.disconnect()
        except Exception as e:
            logger.error(f"Error disconnecting: {e}")
        
        # Clear queue using string key
        guild_key = str(interaction.guild_id)
        if guild_key in bot_state.music_queues:
            bot_state.music_queues[guild_key].clear()
        
        embed = create_embed(
            "👋 Left Voice Channel",
            f"Disconnected from **{channel_name}**",
            discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="queue", description="View the current music queue.")
    async def queue_command(interaction: discord.Interaction):
        """Display the current music queue."""
        guild_key = str(interaction.guild_id)
        
        # Check if anything is playing
        voice_client = interaction.guild.voice_client
        is_playing = voice_client and (voice_client.is_playing() or voice_client.is_paused())
        current_song = bot_state.now_playing.get(guild_key)
        
        if not is_playing and (guild_key not in bot_state.music_queues or not bot_state.music_queues[guild_key]):
            embed = create_embed(
                "📋 Queue Empty",
                "There are no songs in the queue. Use `/play` to add some!",
                discord.Color.orange()
            )
            return await interaction.response.send_message(embed=embed)
        
        embed = create_embed(
            "🎵 Music Queue",
            f"",
            discord.Color.blue()
        )
        
        # Show currently playing
        if is_playing and current_song:
            embed.add_field(
                name="▶️ Now Playing",
                value=current_song[:100],
                inline=False
            )
        
        # Show queue
        if guild_key in bot_state.music_queues and bot_state.music_queues[guild_key]:
            queue = bot_state.music_queues[guild_key]
            
            for idx, (_, title) in enumerate(list(queue)[:10], 1):
                embed.add_field(
                    name=f"#{idx}",
                    value=title[:100],
                    inline=False
                )
            
            if len(queue) > 10:
                embed.set_footer(text=f"... and {len(queue) - 10} more songs")
        
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="skip", description="Skip the current song.")
    async def skip_command(interaction: discord.Interaction):
        """Skip the current song."""
        voice_client = interaction.guild.voice_client
        
        if not voice_client or not getattr(voice_client, "is_connected", lambda: False)():
            return await interaction.response.send_message(
                "❌ I'm not connected to a voice channel!", 
                ephemeral=True
            )
        
        if not voice_client.is_playing() and not voice_client.is_paused():
            return await interaction.response.send_message(
                "❌ Nothing is currently playing!", 
                ephemeral=True
            )
        
        # Stop current song (this will trigger the after_play callback which plays next song)
        voice_client.stop()
        
        embed = create_embed(
            "⏭️ Skipped",
            "Skipped to the next song!",
            discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed)

# ========== Enhanced Trivia System ==========
class TriviaView(View):
    """Enhanced trivia view with better UX."""
    
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
        
        # Create buttons
        for idx, option in enumerate(self.options, 1):
            btn = Button(
                label=str(idx), 
                style=discord.ButtonStyle.primary, 
                custom_id=f"option_{idx}",
                emoji="🔢"
            )
            btn.callback = self.create_callback(idx, option)
            self.add_item(btn)
    
    def create_callback(self, idx: int, option: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message(
                    "This isn't your trivia question! Use `/trivia` to start your own.", 
                    ephemeral=True
                )
            
            if self.answered:
                return await interaction.response.send_message(
                    "This question has already been answered!", 
                    ephemeral=True
                )
            
            self.answered = True
            await self.process_answer(interaction, idx, option)
        
        return callback
    
    async def process_answer(self, interaction: discord.Interaction, idx: int, selected_option: str):
        """Process the user's answer."""
        is_correct = selected_option == self.correct
        
        # Update button styles
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
        
        # Send result
        await self.send_result(interaction, is_correct, selected_option)
        self.stop()
    
    async def send_result(self, interaction: discord.Interaction, is_correct: bool, selected_option: str):
        """Send detailed trivia result."""
        if is_correct:
            title = "🎉 Correct!"
            desc = f"Well done, {interaction.user.mention}!"
            color = discord.Color.green()
        else:
            title = "❌ Incorrect"
            desc = f"{interaction.user.mention}, the correct answer was **{self.correct}**"
            color = discord.Color.red()
        
        embed = create_embed(title, desc, color)
        
        # Add question details
        embed.add_field(
            name="Question", 
            value=html.unescape(self.question_data['question']), 
            inline=False
        )
        embed.add_field(
            name="Category", 
            value=self.question_data['category'], 
            inline=True
        )
        embed.add_field(
            name="Difficulty", 
            value=self.question_data['difficulty'].capitalize(), 
            inline=True
        )
        
        if not is_correct:
            embed.add_field(
                name="Your Answer", 
                value=selected_option, 
                inline=True
            )
        
        embed.set_footer(text="Use /trivia to play again!")
        
        await interaction.followup.send(embed=embed)

    async def on_timeout(self) -> None:
        """Handle timeout."""
        if self.message is None or self.answered:
            return
        
        self.answered = True
        
        # Update buttons to show correct answer
        for child in self.children:
            child.disabled = True
            option_idx = int(child.custom_id.split('_')[1])
            option_text = self.options[option_idx - 1]
            
            if option_text == self.correct:
                child.style = discord.ButtonStyle.success
                child.emoji = "✅"
            else:
                child.style = discord.ButtonStyle.secondary
        
        # Update embed
        try:
            embed = self.message.embeds[0]
            embed.title = "⏰ Time's Up!"
            embed.color = discord.Color.dark_gray()
            embed.set_footer(text="You ran out of time! Use /trivia to try again.")
            
            await self.message.edit(embed=embed, view=self)
            await self.message.reply(f"⏰ Time's up! The correct answer was **{self.correct}**")
        except Exception as e:
            logger.error(f"Error updating expired trivia: {e}")

class TriviaSetupView(View):
    """Enhanced trivia setup with better organization."""
    
    def __init__(self, interaction: discord.Interaction, categories: List[Dict[str, Any]]):
        super().__init__(timeout=Config.SETUP_TIMEOUT)
        self.interaction = interaction
        self.category = "0"  # Any category
        self.difficulty = "any"
        self.categories = categories
        
        self.setup_components()
    
    def setup_components(self):
        """Setup all UI components."""
        # Category select
        category_options = [discord.SelectOption(
            label="Any Category", 
            description="Random category", 
            value="0",
            emoji="🎲"
        )]
        
        for category in self.categories[:23]:  # Discord limit is 25, we have 2 already
            category_options.append(discord.SelectOption(
                label=category["name"][:100],  # Discord limit
                value=str(category["id"])
            ))
        
        category_select = Select(
            placeholder="🎯 Choose a category...", 
            options=category_options,
            custom_id="category_select"
        )
        category_select.callback = self.category_callback
        self.add_item(category_select)
        
        # Difficulty select
        difficulty_options = [
            discord.SelectOption(label="Any Difficulty", value="any", emoji="🎲"),
            discord.SelectOption(label="Easy", value="easy", emoji="🟢"),
            discord.SelectOption(label="Medium", value="medium", emoji="🟡"),
            discord.SelectOption(label="Hard", value="hard", emoji="🔴")
        ]
        
        difficulty_select = Select(
            placeholder="⚡ Choose difficulty...", 
            options=difficulty_options,
            custom_id="difficulty_select"
        )
        difficulty_select.callback = self.difficulty_callback
        self.add_item(difficulty_select)
        
        # Start button
        start_button = Button(
            label="Start Trivia!", 
            style=discord.ButtonStyle.success, 
            emoji="🚀",
            custom_id="start_trivia"
        )
        start_button.callback = self.start_callback
        self.add_item(start_button)
    
    async def category_callback(self, interaction: discord.Interaction) -> None:
        self.category = interaction.data['values'][0]
        selected_name = next(
            (cat["name"] for cat in self.categories if str(cat["id"]) == self.category), 
            "Any Category"
        )
        await interaction.response.send_message(
            f"📂 Selected category: **{selected_name}**", 
            ephemeral=True
        )
    
    async def difficulty_callback(self, interaction: discord.Interaction) -> None:
        self.difficulty = interaction.data['values'][0]
        difficulty_display = self.difficulty.capitalize() if self.difficulty != "any" else "Any Difficulty"
        await interaction.response.send_message(
            f"⚡ Selected difficulty: **{difficulty_display}**", 
            ephemeral=True
        )
    
    async def start_callback(self, interaction: discord.Interaction) -> None:
        for child in self.children:
            child.disabled = True
        
        embed = create_embed(
            "🔄 Loading Trivia...", 
            "Fetching your question...", 
            discord.Color.yellow()
        )
        await interaction.response.edit_message(embed=embed, view=self)
        
        await fetch_and_display_trivia(interaction, self.category, self.difficulty)
    
    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        
        try:
            embed = create_embed(
                "⏰ Setup Expired", 
                "The trivia setup timed out. Use `/trivia` to try again.", 
                discord.Color.dark_gray()
            )
            await self.interaction.edit_original_response(embed=embed, view=self)
        except Exception as e:
            logger.warning(f"Could not update expired setup view: {e}")

# ========== Enhanced Trivia Functions ==========
async def fetch_categories() -> List[Dict[str, Any]]:
    """Fetch trivia categories with better error handling."""
    data = await safe_api_request(Config.TRIVIA_CATEGORIES_API)
    
    if data and "trivia_categories" in data:
        categories = data["trivia_categories"]
        logger.info(f"Fetched {len(categories)} trivia categories")
        return categories
    
    # Comprehensive fallback categories
    logger.warning("Failed to fetch categories, using comprehensive fallback")
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

async def fetch_and_display_trivia(
    interaction: discord.Interaction, 
    category_id: str = "0", 
    difficulty: str = "any"
) -> None:
    """Fetch and display trivia with enhanced presentation."""
    params = {}
    if category_id != "0":
        params["category"] = category_id
    if difficulty != "any":
        params["difficulty"] = difficulty
    
    data = await safe_api_request(Config.TRIVIA_API, params)
    
    if not data or data.get("response_code") != 0 or not data.get("results"):
        embed = create_embed(
            "😅 No Questions Found",
            "Couldn't find a trivia question with those settings. Try different options!",
            discord.Color.orange()
        )
        return await interaction.edit_original_response(embed=embed, view=None)
    
    question_data = data["results"][0]
    view = TriviaView(interaction.user.id, question_data)
    
    # Create enhanced embed
    question_text = html.unescape(question_data["question"])
    category = question_data["category"]
    difficulty_level = question_data["difficulty"]
    
    # Difficulty emoji mapping
    difficulty_emojis = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}
    difficulty_emoji = difficulty_emojis.get(difficulty_level, "⚪")
    
    embed = create_embed(
        f"🧠 Trivia Question",
        f"**{question_text}**",
        discord.Color.blurple()
    )
    
    embed.add_field(
        name="📂 Category", 
        value=category, 
        inline=True
    )
    embed.add_field(
        name=f"{difficulty_emoji} Difficulty", 
        value=difficulty_level.capitalize(), 
        inline=True
    )
    embed.add_field(
        name="⏱️ Time Limit", 
        value=f"{Config.TRIVIA_TIMEOUT} seconds", 
        inline=True
    )
    
    # Add options
    for idx, option in enumerate(view.options, 1):
        embed.add_field(
            name=f"{idx}️⃣ Option {idx}", 
            value=option, 
            inline=False
        )
    
    embed.set_footer(
        text=f"Requested by {interaction.user.display_name} • Select an option below!",
        icon_url=interaction.user.display_avatar.url
    )
    
    msg = await interaction.edit_original_response(embed=embed, view=view)
    view.message = msg
    
    logger.info(f"Trivia question displayed to {interaction.user} (Category: {category}, Difficulty: {difficulty_level})")

# ========== Enhanced Bot Commands ==========
@bot.tree.command(name="help", description="Show all available commands and their usage.")
async def help_command(interaction: discord.Interaction):
    """Enhanced help command with categorized information."""
    embed = create_embed(
        "🤖 Bot Commands", 
        "Here are all the available commands:", 
        discord.Color.blue()
    )
    
    # Fun Commands
    embed.add_field(
        name="🎲 Fun Commands",
        value="`/trivia` - Play trivia questions\n"
              "`/fact` - Get a random fact\n"
              "`/coin` - Flip a coin\n"
              "`/number <min> <max>` - Random number",
        inline=False
    )
    
    # Utility Commands
    embed.add_field(
        name="🔧 Utility Commands",
        value="`/weather <city>` - Get weather info\n"
              "`/ping` - Check bot latency\n"
              "`/help` - Show this message",
        inline=False
    )
    
    # Music Commands (if available)
    if YT_DLP_AVAILABLE and VOICE_AVAILABLE:
        embed.add_field(
            name="🎵 Music Commands",
            value="`/play <song>` - Play music or add playlist\n"
                  "`/queue` - View current queue\n"
                  "`/skip` - Skip current song\n"
                  "`/stop` - Stop music and clear queue\n"
                  "`/leave` - Leave voice channel",
            inline=False
        )
    
    embed.set_footer(text="Use slash commands (/) to interact with the bot!")
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="fact", description="Get a random interesting fact.")
@rate_limit
async def fact_command(interaction: discord.Interaction):
    """Enhanced fact command with better presentation."""
    await interaction.response.defer()
    
    try:
        fact = randfacts.get_fact()
        
        embed = create_embed(
            "🧠 Random Fact",
            fact,
            discord.Color.green()
        )
        embed.set_footer(
            text=f"Fact requested by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url
        )
        
        await interaction.followup.send(embed=embed)
        logger.info(f"Fact command used by {interaction.user}")
        
    except Exception as e:
        logger.error(f"Error getting fact: {e}")
        embed = create_embed(
            "❌ Error",
            "Sorry, couldn't fetch a fact right now! Please try again later.",
            discord.Color.red()
        )
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="ping", description="Check the bot's latency and status.")
async def ping_command(interaction: discord.Interaction):
    """Enhanced ping command with detailed information."""
    start_time = datetime.datetime.utcnow()
    
    embed = create_embed(
        "🏓 Pong!",
        "Checking connection...",
        discord.Color.yellow()
    )
    await interaction.response.send_message(embed=embed)
    
    # Calculate response time
    end_time = datetime.datetime.utcnow()
    response_time = (end_time - start_time).total_seconds() * 1000
    
    # Update embed with detailed info
    ws_latency = round(bot.latency * 1000)
    
    embed = create_embed(
        "🏓 Pong!",
        "Connection status and latency information:",
        discord.Color.green()
    )
    embed.add_field(name="WebSocket Latency", value=f"{ws_latency}ms", inline=True)
    embed.add_field(name="Response Time", value=f"{response_time:.1f}ms", inline=True)
    embed.add_field(name="Status", value="✅ Online", inline=True)
    
    uptime = datetime.datetime.utcnow() - bot_state.start_time
    embed.add_field(
        name="Uptime", 
        value=format_duration(int(uptime.total_seconds())), 
        inline=True
    )
    embed.add_field(name="Guilds", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Users", value=str(len(bot.users)), inline=True)
    
    await interaction.edit_original_response(embed=embed)

@bot.tree.command(name="number", description="Generate a random number between two values.")
@app_commands.describe(min_num="Minimum number", max_num="Maximum number")
async def number_command(interaction: discord.Interaction, min_num: int, max_num: int):
    """Enhanced random number generator with validation."""
    if min_num > max_num:
        embed = create_embed(
            "❌ Invalid Range",
            f"The minimum number ({min_num}) must be less than or equal to the maximum number ({max_num}).",
            discord.Color.red()
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    
    if max_num - min_num > 1000000:
        embed = create_embed(
            "❌ Range Too Large",
            "The range is too large. Please use a smaller range (max 1,000,000).",
            discord.Color.red()
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    
    result = random.randint(min_num, max_num)
    
    embed = create_embed(
        "🎲 Random Number",
        f"Your random number between **{min_num}** and **{max_num}** is:",
        discord.Color.blue()
    )
    embed.add_field(name="Result", value=f"**{result}**", inline=False)
    embed.set_footer(
        text=f"Generated for {interaction.user.display_name}",
        icon_url=interaction.user.display_avatar.url
    )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="coin", description="Flip a coin and get heads or tails.")
async def coin_command(interaction: discord.Interaction):
    """Enhanced coin flip with animation effect."""
    embed = create_embed(
        "🪙 Flipping Coin...",
        "The coin is spinning through the air...",
        discord.Color.yellow()
    )
    await interaction.response.send_message(embed=embed)
    
    # Add a small delay for effect
    await asyncio.sleep(1)
    
    result = "Heads" if random.randint(0, 1) == 0 else "Tails"
    emoji = "👑" if result == "Heads" else "🎯"
    
    embed = create_embed(
        f"🪙 Coin Flip Result",
        f"The coin landed on **{result}**! {emoji}",
        discord.Color.green()
    )
    embed.set_footer(
        text=f"Flipped by {interaction.user.display_name}",
        icon_url=interaction.user.display_avatar.url
    )
    
    await interaction.edit_original_response(embed=embed)

@bot.tree.command(name="trivia", description="Play an interactive trivia game with multiple categories and difficulties.")
@rate_limit
async def trivia_command(interaction: discord.Interaction):
    """Enhanced trivia command with better setup."""
    await interaction.response.defer()
    
    try:
        categories = await fetch_categories()
        view = TriviaSetupView(interaction, categories)
        
        embed = create_embed(
            "🧠 Trivia Setup",
            "Welcome to Trivia! Configure your question below:",
            discord.Color.blue()
        )
        embed.add_field(
            name="📂 Categories Available", 
            value=f"{len(categories)} categories", 
            inline=True
        )
        embed.add_field(
            name="⚡ Difficulties", 
            value="Easy, Medium, Hard", 
            inline=True
        )
        embed.add_field(
            name="⏱️ Time Limit", 
            value=f"{Config.TRIVIA_TIMEOUT} seconds", 
            inline=True
        )
        embed.set_footer(
            text="Select your preferences and click 'Start Trivia!' to begin",
            icon_url=interaction.user.display_avatar.url
        )
        
        await interaction.followup.send(embed=embed, view=view)
        logger.info(f"Trivia setup presented to {interaction.user}")
        
    except Exception as e:
        logger.error(f"Error in trivia setup: {e}")
        embed = create_embed(
            "❌ Setup Error",
            "Sorry, couldn't start trivia right now! Please try again later.",
            discord.Color.red()
        )
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="weather", description="Get detailed weather information for any city.")
@app_commands.describe(city="Name of the city to get weather for")
@rate_limit
async def weather_command(interaction: discord.Interaction, city: str):
    """Enhanced weather command with comprehensive information."""
    # Input validation
    city = city.strip()
    if not city or len(city) < Config.MIN_CITY_NAME_LENGTH:
        embed = create_embed(
            "❌ Invalid Input",
            "Please provide a valid city name!",
            discord.Color.red()
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    
    if len(city) > Config.MAX_CITY_NAME_LENGTH:
        embed = create_embed(
            "❌ City Name Too Long",
            f"City name must be less than {Config.MAX_CITY_NAME_LENGTH} characters!",
            discord.Color.red()
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    
    await interaction.response.defer()
    
    try:
        # Show loading message
        embed = create_embed(
            "🔍 Fetching Weather...",
            f"Getting weather data for **{city}**...",
            discord.Color.yellow()
        )
        await interaction.edit_original_response(embed=embed)
        
        async with python_weather.Client(unit=python_weather.IMPERIAL) as client:
            weather = await client.get(city)
            embed = await create_weather_embed(weather, interaction.user)
            await interaction.edit_original_response(embed=embed)
            
        logger.info(f"Weather data provided for {city} to user {interaction.user}")
        
    except RequestError as e:
        logger.error(f"Weather API error (status {e.status}): {str(e)}")
        embed = create_embed(
            "🌐 API Error",
            f"Weather service returned an error for '{city}'. Please check the city name and try again.",
            discord.Color.red()
        )
        embed.add_field(name="Error Code", value=str(e.status), inline=True)
        await interaction.edit_original_response(embed=embed)
        
    except Error as e:
        logger.error(f"Weather lookup error: {str(e)}")
        embed = create_embed(
            "❌ Weather Error",
            f"Couldn't get weather data for '{city}'. Please try a different city name.",
            discord.Color.red()
        )
        await interaction.edit_original_response(embed=embed)
        
    except Exception as e:
        logger.error(f"Unexpected weather error: {str(e)}")
        embed = create_embed(
            "💥 Unexpected Error",
            "Something went wrong while fetching weather data. Please try again later.",
            discord.Color.red()
        )
        await interaction.edit_original_response(embed=embed)

async def create_weather_embed(weather, user: discord.User) -> discord.Embed:
    """Create an enhanced weather embed with comprehensive information."""
    # Get weather emoji and create title
    weather_emoji = getattr(weather.kind, 'emoji', '🌤️')
    date_str = weather.datetime.strftime('%A, %B %d, %Y')
    
    title = f"{weather_emoji} Weather in {weather.location}"
    description = f"**{weather.description}** • **{weather.temperature}°F**\n{date_str}"
    
    # Choose color based on temperature
    temp = weather.temperature
    if temp >= 80:
        color = discord.Color.red()
    elif temp >= 60:
        color = discord.Color.orange()
    elif temp >= 40:
        color = discord.Color.blue()
    else:
        color = discord.Color.dark_blue()
    
    embed = create_embed(title, description, color)
    
    # Location details
    if weather.region and weather.country:
        embed.add_field(
            name="📍 Location", 
            value=f"{weather.region}, {weather.country}", 
            inline=False
        )
    
    # Temperature information
    embed.add_field(name="🌡️ Temperature", value=f"{weather.temperature}°F", inline=True)
    embed.add_field(name="🤚 Feels Like", value=f"{weather.feels_like}°F", inline=True)
    embed.add_field(name="💧 Humidity", value=f"{weather.humidity}%", inline=True)
    
    # Wind information
    wind_info = f"{weather.wind_speed} mph"
    if weather.wind_direction:
        direction_str = str(weather.wind_direction)
        if hasattr(weather.wind_direction, "emoji"):
            direction_str += f" {weather.wind_direction.emoji}"
        wind_info += f" {direction_str}"
    embed.add_field(name="💨 Wind", value=wind_info, inline=True)
    
    # Additional weather data
    embed.add_field(name="🌧️ Precipitation", value=f"{weather.precipitation} in", inline=True)
    embed.add_field(name="🔽 Pressure", value=f"{weather.pressure} inHg", inline=True)
    
    if weather.visibility:
        embed.add_field(name="👁️ Visibility", value=f"{weather.visibility} miles", inline=True)
    
    if weather.ultraviolet:
        uv_text = str(weather.ultraviolet)
        if hasattr(weather.ultraviolet, "index"):
            uv_index = weather.ultraviolet.index
            uv_text = f"{uv_index}/10"
            # Add UV warning emoji based on index
            if uv_index >= 8:
                uv_text += " ⚠️"
            elif uv_index >= 6:
                uv_text += " 🟡"
        embed.add_field(name="☀️ UV Index", value=uv_text, inline=True)
    
    # Forecast information
    if weather.daily_forecasts:
        forecast_text = ""
        for i, day_forecast in enumerate(weather.daily_forecasts[:4]):  # Show 4 days
            if i >= 4:  # Limit to prevent embed length issues
                break
                
            day_name = get_day_name(i, day_forecast)
            day_emoji = getattr(getattr(day_forecast, 'kind', None), 'emoji', '🌤️')
            
            # Get temperature info
            temp_info = get_temperature_info(day_forecast)
            
            # Get description
            description = ""
            if hasattr(day_forecast, 'description'):
                description = day_forecast.description
            elif hasattr(day_forecast, 'kind'):
                description = str(day_forecast.kind)
            
            day_text = f"{day_emoji} **{day_name}**: {description}"
            if temp_info:
                day_text += f" • {temp_info}"
            
            forecast_text += day_text + "\n"
        
        if forecast_text:
            embed.add_field(name="📅 Forecast", value=forecast_text, inline=False)
    
    # Footer with timestamp and user
    embed.set_footer(
        text=f"Requested by {user.display_name} • {weather.datetime.strftime('%I:%M %p')}",
        icon_url=user.display_avatar.url
    )
    
    return embed

def get_day_name(index: int, day_forecast) -> str:
    """Get appropriate day name for forecast."""
    if index == 0:
        return "Today"
    elif index == 1:
        return "Tomorrow"
    else:
        if hasattr(day_forecast, 'date'):
            return day_forecast.date.strftime('%A')
        return f"Day {index + 1}"

def get_temperature_info(day_forecast) -> str:
    """Extract temperature information from day forecast."""
    # Try different possible attribute names
    temp_high = (getattr(day_forecast, 'highest', None) or 
                getattr(day_forecast, 'high', None) or 
                getattr(day_forecast, 'max_temperature', None) or
                getattr(day_forecast, 'temperature', None))
    
    temp_low = (getattr(day_forecast, 'lowest', None) or 
               getattr(day_forecast, 'low', None) or 
               getattr(day_forecast, 'min_temperature', None))
    
    if temp_high is not None:
        if temp_low is not None:
            return f"H: {temp_high}°F, L: {temp_low}°F"
        else:
            return f"{temp_high}°F"
    
    return "Temperature data unavailable"

# ========== Enhanced Bot Events ==========
@bot.event
async def on_ready():
    """Enhanced bot startup event."""
    logger.info(f'🤖 {bot.user} has connected to Discord!')
    logger.info(f'📊 Connected to {len(bot.guilds)} guilds with {len(bot.users)} users')
    
    # Initialize bot state
    await bot_state.initialize()
    
    # Sync commands
    try:
        synced = await bot.tree.sync()
        logger.info(f'✅ Synced {len(synced)} slash command(s)')
    except Exception as e:
        logger.error(f'❌ Failed to sync commands: {e}')
    
    # Set bot status
    activity = discord.Activity(
        type=discord.ActivityType.listening, 
        name=f"/help • {len(bot.guilds)} servers"
    )
    await bot.change_presence(activity=activity)
    
    # Start background tasks
    if not status_update_task.is_running():
        status_update_task.start()

@bot.event
async def on_guild_join(guild):
    """Handle joining a new guild."""
    logger.info(f'📥 Joined guild: {guild.name} (ID: {guild.id})')
    
    # Update status
    activity = discord.Activity(
        type=discord.ActivityType.listening, 
        name=f"/help • {len(bot.guilds)} servers"
    )
    await bot.change_presence(activity=activity)
    
    # Try to send a welcome message
    if guild.system_channel:
        embed = create_embed(
            "👋 Hello!",
            f"Thanks for adding me to **{guild.name}**!\n\n"
            "• Use `/help` to see all available commands\n"
            "• Use `/trivia` to start a fun trivia game\n"
            "• Use `/weather <city>` to get weather information\n\n"
            "Have fun! 🎉",
            discord.Color.green()
        )
        try:
            await guild.system_channel.send(embed=embed)
        except discord.Forbidden:
            pass  # Can't send messages to that channel

@bot.event
async def on_guild_remove(guild):
    """Handle leaving a guild."""
    logger.info(f'📤 Left guild: {guild.name} (ID: {guild.id})')
    
    # Clean up any guild-specific data
    guild_key = str(guild.id)
    if guild_key in bot_state.music_queues:
        del bot_state.music_queues[guild_key]
    
    # Update status
    activity = discord.Activity(
        type=discord.ActivityType.listening, 
        name=f"/help • {len(bot.guilds)} servers"
    )
    await bot.change_presence(activity=activity)

@bot.event
async def on_command_error(ctx, error):
    """Enhanced error handling."""
    logger.error(f'Command error in {ctx.command}: {error}', exc_info=error)

@bot.event
async def on_application_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Handle application command errors."""
    logger.error(f'Slash command error: {error}', exc_info=error)
    
    if isinstance(error, app_commands.CommandOnCooldown):
        embed = create_embed(
            "⏰ Cooldown",
            f"This command is on cooldown. Try again in {error.retry_after:.1f} seconds.",
            discord.Color.orange()
        )
    elif isinstance(error, app_commands.MissingPermissions):
        embed = create_embed(
            "🔒 Missing Permissions",
            "You don't have permission to use this command.",
            discord.Color.red()
        )
    else:
        embed = create_embed(
            "💥 Command Error",
            "An unexpected error occurred. Please try again later.",
            discord.Color.red()
        )
    
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Could not send error message: {e}")

@bot.event
async def on_voice_state_update(member, before, after):
    """Handle voice state changes for music cleanup."""
    if not VOICE_AVAILABLE:
        return
        
    # If bot was disconnected from voice, clean up
    if member == bot.user and before.channel is not None and after.channel is None:
        guild_key = str(before.channel.guild.id)
        if guild_key in bot_state.music_queues:
            bot_state.music_queues[guild_key].clear()
            logger.info(f"Cleaned up music queue for guild {guild_key}")

# ========== Background Tasks ==========
@tasks.loop(minutes=30)
async def status_update_task():
    """Update bot status periodically."""
    activities = [
        discord.Activity(type=discord.ActivityType.listening, name=f"/help • {len(bot.guilds)} servers"),
        discord.Activity(type=discord.ActivityType.playing, name="trivia games"),
        discord.Activity(type=discord.ActivityType.watching, name="the weather"),
    ]
    
    activity = random.choice(activities)
    await bot.change_presence(activity=activity)

# ========== Cleanup and Shutdown Handlers ==========
async def cleanup_resources():
    """Comprehensive resource cleanup."""
    logger.info("🧹 Starting cleanup...")
    
    # Close HTTP session
    await bot_state.cleanup()
    
    # Disconnect from all voice channels (if voice is available)
    if VOICE_AVAILABLE:
        for guild in bot.guilds:
            if guild.voice_client:
                await guild.voice_client.disconnect()
    
    # Clear music queues
    bot_state.music_queues.clear()
    
    logger.info("✅ Cleanup completed")

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    logger.info(f"📥 Received signal {signum}, shutting down gracefully...")
    asyncio.create_task(cleanup_resources())
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

@bot.event
async def on_disconnect():
    """Handle bot disconnect."""
    logger.info("🔌 Bot disconnected")
    await cleanup_resources()

# ========== Enhanced Bot Startup Function ==========
def start_discord_bot():
    """Start Discord bot with enhanced error handling and logging."""
    try:
        logger.info("🚀 Starting Discord bot...")
        
        # Check if we're running in a deployment environment
        is_deployment = os.getenv("DEPLOYMENT") == "true" or "gunicorn" in os.environ.get("SERVER_SOFTWARE", "")
        
        if is_deployment:
            logger.info("🔧 Running in deployment mode")
            # In deployment, just run the bot without additional setup
            bot.run(DISCORD_TOKEN, log_handler=None)  # Disable discord.py's default logging
        else:
            logger.info("🔧 Running in development mode")
            bot.run(DISCORD_TOKEN)
            
    except discord.LoginFailure:
        logger.critical("❌ Invalid Discord token! Check your .env file.")
        sys.exit(1)
    except discord.HTTPException as e:
        logger.critical(f"❌ Discord HTTP error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("⏹️ Bot shutdown requested by user")
    except Exception as e:
        logger.critical(f"💥 Bot startup failed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        # Ensure cleanup happens
        logger.info("🔄 Running final cleanup...")
        try:
            asyncio.run(cleanup_resources())
        except Exception as cleanup_error:
            logger.error(f"Error during cleanup: {cleanup_error}")

# ========== Application Entry Points ==========
if __name__ != "__main__":
    # When imported as module (for deployment)
    Thread(target=start_discord_bot, daemon=False).start()

# Flask app for external access
application = app

if __name__ == "__main__":
    # When run directly
    import atexit
    atexit.register(lambda: asyncio.run(cleanup_resources()))
    
    # Start Flask server in background
    Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=int(os.getenv("PORT", 5000)),
            debug=False,
            use_reloader=False
        ),
        daemon=True
    ).start()
    
    logger.info(f"🌐 Flask server starting on port {os.getenv('PORT', 5000)}")
    
    # Start Discord bot in main thread
    start_discord_bot()