import asyncio
import random
from collections import deque

import discord
import yt_dlp
from discord import app_commands
from discord.ext import tasks

from .bot import bot
from .config import Config
from .log import logger
from .state import bot_state, queue_key
from .utils import create_embed, format_duration, rate_limit

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
    def __init__(self, source, *, data):
        super().__init__(source, 0.5)
        self.duration = data.get('duration')

    @classmethod
    async def from_url(cls, url, *, loop=None):
        loop = loop or asyncio.get_event_loop()

        try:
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
        except Exception as e:
            logger.error(f"yt-dlp extraction failed for {url}: {e}")
            raise

        if 'entries' in data:
            data = data['entries'][0]

        filename = data['url']
        ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn'
        }

        try:
            source = discord.FFmpegPCMAudio(filename, **ffmpeg_options)
        except Exception as e:
            logger.error(f"FFmpeg failed: {e}")
            raise

        return cls(source, data=data)

def is_active(voice_client) -> bool:
    return voice_client.is_playing() or voice_client.is_paused()

async def connected_voice_client(interaction: discord.Interaction):
    """Return the guild's connected voice client, or None after replying with the error."""
    voice_client = interaction.guild.voice_client  # Raises in DMs, reaching on_app_command_error
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("❌ Not in voice channel!", ephemeral=True)
        return None
    return voice_client

def _make_after_play(player, title, voice_client, guild_key, channel):
    def after_play(error):
        if error:
            logger.error(f"Playback error for {title}: {error}")

        try:
            player.cleanup()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

        fut = asyncio.run_coroutine_threadsafe(
            play_next_song(voice_client, guild_key, channel),
            bot.loop
        )
        try:
            fut.result(timeout=5)
        except Exception as e:
            logger.error(f"Error scheduling next: {e}")
    return after_play

def _now_playing_embed(title, player, guild_key) -> discord.Embed:
    embed = create_embed("🎵 Now Playing", f"**{title}**", discord.Color.blue())
    if player.duration:
        embed.add_field(name="Duration", value=format_duration(int(player.duration)), inline=True)
    if bot_state.music_queues[guild_key]:  # KeyError if on_voice_state_update popped it: caught by play_next_song
        embed.add_field(name="Up Next", value=f"{len(bot_state.music_queues[guild_key])} songs", inline=True)
    return embed

async def play_next_song(voice_client, guild_key, channel):
    try:
        if not voice_client or not voice_client.is_connected():
            return

        if guild_key not in bot_state.music_queues or not bot_state.music_queues[guild_key]:
            await asyncio.sleep(3)
            if voice_client.is_connected():
                await voice_client.disconnect()
                embed = create_embed("👋 Queue Finished", "All songs played. Disconnecting...", discord.Color.blue())
                await channel.send(embed=embed)
            return

        video_url, title = bot_state.music_queues[guild_key].popleft()

        try:
            player = await asyncio.wait_for(
                YTDLSource.from_url(video_url, loop=bot.loop),
                timeout=30.0
            )

            after_play = _make_after_play(player, title, voice_client, guild_key, channel)

            if voice_client.is_playing():
                voice_client.stop()

            voice_client.play(player, after=after_play)
            bot_state.now_playing[guild_key] = title

            await channel.send(embed=_now_playing_embed(title, player, guild_key))

        except Exception as e:
            logger.error(f"Error playing {title}: {e}")
            embed = create_embed("❌ Playback Error", f"Couldn't play **{title}**. Skipping...", discord.Color.red())
            await channel.send(embed=embed)
            await asyncio.sleep(1)
            if bot_state.music_queues.get(guild_key):
                await play_next_song(voice_client, guild_key, channel)

    except Exception as e:
        logger.exception(f"Unexpected error in play_next_song: {e}")
        if guild_key in bot_state.music_queues:
            bot_state.music_queues[guild_key].clear()
        embed = create_embed("💥 Fatal Error", "Music playback failed. Queue cleared.", discord.Color.dark_red())
        await channel.send(embed=embed)

async def _ensure_voice_connection(interaction: discord.Interaction):
    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    if voice_client is None:
        voice_client = await voice_channel.connect()
    elif voice_channel != voice_client.channel:
        await voice_client.move_to(voice_channel)
    return voice_client

def _classify_query(song_query: str):
    """Return (is_url, is_playlist_url)."""
    is_url = song_query.startswith(('http://', 'https://'))
    is_playlist = 'playlist' in song_query.lower() or 'list=' in song_query
    return is_url, is_playlist and is_url

async def _enqueue_playlist(interaction: discord.Interaction, song_query: str, voice_client, guild_key: str) -> bool:
    """Queue a playlist URL. Return False if yt-dlp says it isn't one, so /play falls through to a single track."""
    ydl_options_flat = {"extract_flat": True, "quiet": True, "no_warnings": True}
    results = await search_ytdlp_async(song_query, ydl_options_flat)

    if results.get("_type") != "playlist":
        return False

    entries = [e for e in results.get("entries", []) if e]

    if not entries:
        await interaction.followup.send("❌ No videos in playlist.")
        return True

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

    if not is_active(voice_client):
        await play_next_song(voice_client, guild_key, interaction.channel)
    return True

async def _enqueue_single_track(interaction: discord.Interaction, song_query: str, is_url: bool, voice_client, guild_key: str):
    query = f"ytsearch1:{song_query}" if not is_url else song_query
    ydl_options_info = {"extract_flat": False, "quiet": True, "no_warnings": True}
    results = await search_ytdlp_async(query, ydl_options_info)

    first_track = results['entries'][0] if 'entries' in results else results  # An empty search raises IndexError on purpose
    if not first_track:
        return await interaction.followup.send("❌ No results found.")

    video_url = first_track.get("webpage_url") or first_track.get("url")
    title = first_track.get("title", "Untitled")

    if not video_url:
        return await interaction.followup.send("❌ Could not get video URL.")

    bot_state.music_queues[guild_key].append((video_url, title))

    if is_active(voice_client):
        embed = create_embed("✅ Added to Queue", f"**{title}**", discord.Color.green())
        embed.add_field(name="Position", value=f"#{len(bot_state.music_queues[guild_key])}", inline=True)
    else:
        embed = create_embed("🎵 Starting Playback", f"**{title}**", discord.Color.blue())
        await play_next_song(voice_client, guild_key, interaction.channel)  # Runs before the followup: "Now Playing" comes first

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="play", description="Play a song or add it to queue. Supports playlists!")
@app_commands.describe(song_query="Search query or YouTube URL (supports playlists)")
@rate_limit
async def play_command(interaction: discord.Interaction, song_query: str):
    await interaction.response.defer()

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("❌ You must be in a voice channel.")

    voice_client = await _ensure_voice_connection(interaction)  # Outside the try: failures reach on_app_command_error

    guild_key = queue_key(interaction.guild_id)
    bot_state.music_queues.setdefault(guild_key, deque())

    is_url, is_playlist_url = _classify_query(song_query)

    try:
        if is_playlist_url and await _enqueue_playlist(interaction, song_query, voice_client, guild_key):
            return
        await _enqueue_single_track(interaction, song_query, is_url, voice_client, guild_key)

    except Exception as e:
        logger.error(f"Error in play command: {e}")
        await interaction.followup.send("❌ Couldn't play that. Try a different search.")

@bot.tree.command(name="stop", description="Stop and clear queue.")
async def stop_command(interaction: discord.Interaction):
    voice_client = await connected_voice_client(interaction)
    if voice_client is None:
        return

    if is_active(voice_client):
        voice_client.stop()

    guild_key = queue_key(interaction.guild_id)
    if guild_key in bot_state.music_queues:
        bot_state.music_queues[guild_key].clear()

    embed = create_embed("⏹️ Music Stopped", "Playback stopped and queue cleared.", discord.Color.orange())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leave", description="Leave voice channel.")
async def leave_command(interaction: discord.Interaction):
    voice_client = await connected_voice_client(interaction)
    if voice_client is None:
        return

    channel_name = voice_client.channel.name
    await voice_client.disconnect()

    guild_key = queue_key(interaction.guild_id)
    if guild_key in bot_state.music_queues:
        bot_state.music_queues[guild_key].clear()

    embed = create_embed("👋 Left Voice Channel", f"Disconnected from **{channel_name}**", discord.Color.blue())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="queue", description="View music queue.")
async def queue_command(interaction: discord.Interaction):
    guild_key = queue_key(interaction.guild_id)
    voice_client = interaction.guild.voice_client
    is_playing = voice_client and is_active(voice_client)
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
    voice_client = await connected_voice_client(interaction)
    if voice_client is None:
        return

    if not is_active(voice_client):
        return await interaction.response.send_message("❌ Nothing playing!", ephemeral=True)

    voice_client.stop()
    embed = create_embed("⏭️ Skipped", "Skipped to next song!", discord.Color.blue())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="shuffle", description="Shuffle the music queue.")
async def shuffle_command(interaction: discord.Interaction):
    guild_key = queue_key(interaction.guild_id)
    queue = bot_state.music_queues.get(guild_key)

    if not queue:
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
        embed.add_field(name="Up Next", value=upcoming, inline=False)

        await interaction.response.send_message(embed=embed)
    except Exception as e:
        logger.error(f"Error shuffling queue: {e}")
        embed = create_embed("❌ Shuffle Error", "Couldn't shuffle the queue.", discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)

@tasks.loop(hours=Config.QUEUE_CLEANUP_HOURS)
async def cleanup_inactive_queues():
    """Drop queues for guilds the bot left or has no voice client in."""
    logger.info("Running music queue cleanup...")
    cleaned = 0
    for guild_id in list(bot_state.music_queues.keys()):
        guild = bot.get_guild(int(guild_id))
        if not guild or not guild.voice_client:
            bot_state.music_queues.pop(guild_id, None)
            bot_state.now_playing.pop(guild_id, None)
            cleaned += 1

    if cleaned > 0:
        logger.info(f"Cleaned up {cleaned} inactive music queues")

bot_state.queue_cleanup_task = cleanup_inactive_queues
