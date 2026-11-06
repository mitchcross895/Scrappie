import asyncio
import random
import time
from collections import deque

import discord
import yt_dlp
from discord import app_commands
from discord.ext import tasks

from . import db
from .bot import bot
from .config import Config
from .log import logger
from .state import bot_state, queue_key
from .utils import BotView, RequesterView, create_embed, format_duration, guild_required, rate_limit, storage_required

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

def _pause_playback(voice_client, guild_key):
    voice_client.pause()  # Fires no after_play, so loop and skip state are untouched
    timing = bot_state.track_timing.get(guild_key)
    if timing:
        timing["paused_at"] = time.monotonic()

def _resume_playback(voice_client, guild_key):
    voice_client.resume()
    timing = bot_state.track_timing.get(guild_key)
    if timing and timing["paused_at"] is not None:
        timing["started"] += time.monotonic() - timing["paused_at"]
        timing["paused_at"] = None

def _elapsed_seconds(guild_key) -> int:
    timing = bot_state.track_timing[guild_key]
    end = timing["paused_at"] if timing["paused_at"] is not None else time.monotonic()
    return int(end - timing["started"])

async def connected_voice_client(interaction: discord.Interaction):
    """Return the guild's connected voice client, or None after replying with the error."""
    voice_client = interaction.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("❌ Not in voice channel!", ephemeral=True)
        return None
    return voice_client

def _log_next_song_failure(fut):
    if not fut.cancelled() and fut.exception():
        logger.error(f"Error scheduling next: {fut.exception()}")

def _make_after_play(player, title, voice_client, guild_key, channel):
    def after_play(error):
        if error:
            logger.error(f"Playback error for {title}: {error}")

        try:
            player.cleanup()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

        fut = asyncio.run_coroutine_threadsafe(play_next_song(voice_client, guild_key, channel), bot.loop)
        fut.add_done_callback(_log_next_song_failure)
    return after_play

def _now_playing_embed(title, player, guild_key) -> discord.Embed:
    embed = create_embed("🎵 Now Playing", f"**{title}**", discord.Color.blue())
    if player.duration:
        embed.add_field(name="Duration", value=format_duration(int(player.duration)), inline=True)
    queue = bot_state.music_queues.get(guild_key)
    if queue:
        embed.add_field(name="Up Next", value=f"{len(queue)} songs", inline=True)
    return embed

class NowPlayingView(BotView):
    def __init__(self, voice_client, guild_key: str, title: str):
        super().__init__(timeout=None)  # Lost on restart, which is acceptable
        self.voice_client, self.guild_key, self.title = voice_client, guild_key, title
        self.sync_pause_button()

    def sync_pause_button(self):
        paused = self.voice_client.is_paused()
        self.toggle.label, self.toggle.emoji = ("Resume", "\u25b6\ufe0f") if paused else ("Pause", "\u23f8\ufe0f")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.voice_client.is_connected() or bot_state.now_playing.get(self.guild_key) != self.title:
            await interaction.response.send_message("That song has finished.", ephemeral=True)
            return False
        voice = getattr(interaction.user, "voice", None)
        if not voice or voice.channel != self.voice_client.channel:
            await interaction.response.send_message("\u274c Join my voice channel to use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.voice_client.is_paused():
            _resume_playback(self.voice_client, self.guild_key)
        elif self.voice_client.is_playing():
            _pause_playback(self.voice_client, self.guild_key)
        else:
            return await interaction.response.send_message("\u274c Nothing playing!", ephemeral=True)
        self.sync_pause_button()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary, emoji="\u23ed\ufe0f")
    async def skip(self, interaction, button):
        await _send_skip(interaction, self.voice_client)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="\u23f9\ufe0f")
    async def stop_button(self, interaction, button):
        await _send_stop(interaction, self.voice_client)

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.secondary, emoji="\U0001F500")
    async def shuffle(self, interaction, button):
        await _send_shuffle(interaction)

async def _sync_pause_button(guild_key):
    entry = bot_state.now_playing_messages.get(guild_key)
    if entry:
        entry[1].sync_pause_button()
        try:
            await entry[0].edit(view=entry[1])
        except Exception as e:
            logger.warning(f"Couldn't update Now Playing buttons: {e}")

async def _post_now_playing(voice_client, guild_key, channel, title, player):
    await bot_state.retire_now_playing(guild_key)
    view = NowPlayingView(voice_client, guild_key, title)
    message = await channel.send(embed=_now_playing_embed(title, player, guild_key), view=view)
    bot_state.now_playing_messages[guild_key] = (message, view)
    if bot_state.now_playing.get(guild_key) != title:  # The session ended during the send
        await bot_state.retire_now_playing(guild_key)

def _apply_loop_mode(guild_key: str):
    """Runs before the empty-queue check, so a looped last song keeps playing. Always re-reads the deque: /shuffle replaces it."""
    finished = bot_state.current_tracks.pop(guild_key, None)
    skipped = guild_key in bot_state.skip_requested
    bot_state.skip_requested.discard(guild_key)
    mode = bot_state.loop_modes.get(guild_key)
    queue = bot_state.music_queues.get(guild_key)
    if finished is None or queue is None:
        return
    if mode == "song" and not skipped:
        queue.appendleft(finished)
    elif mode == "queue":
        queue.append(finished)

async def _idle_disconnect(voice_client, guild_key, channel):
    await asyncio.sleep(Config.MUSIC_IDLE_SECONDS)
    bot_state.idle_tasks.pop(guild_key, None)  # First: disconnecting fires on_voice_state_update, which must not cancel this task mid-send
    if not voice_client.is_connected() or is_active(voice_client) or bot_state.music_queues.get(guild_key):
        return
    try:
        await voice_client.disconnect()
        await channel.send(embed=create_embed("👋 Queue Finished", "Nothing left to play. Disconnecting...", discord.Color.blue()))
    except Exception as e:
        logger.error(f"Idle disconnect error: {e}")

async def _go_idle(voice_client, guild_key, channel):
    """Nothing to play: forget the finished song and start the timer."""
    if is_active(voice_client) or not voice_client.is_connected() or bot.is_closed():
        return
    bot_state.current_tracks.pop(guild_key, None)
    bot_state.now_playing.pop(guild_key, None)
    bot_state.track_timing.pop(guild_key, None)
    bot_state.cancel_idle(guild_key)
    bot_state.idle_tasks[guild_key] = asyncio.create_task(_idle_disconnect(voice_client, guild_key, channel))
    await bot_state.retire_now_playing(guild_key)

async def play_next_song(voice_client, guild_key, channel):
    try:
        bot_state.cancel_idle(guild_key)
        if not voice_client or not voice_client.is_connected():
            return

        _apply_loop_mode(guild_key)

        if not bot_state.music_queues.get(guild_key):
            await _go_idle(voice_client, guild_key, channel)
            return

        track = bot_state.music_queues[guild_key].popleft()
        video_url, title = track

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
            bot_state.current_tracks[guild_key] = track  # Only after play: a track that fails is dropped, so loops always end

            bot_state.track_timing[guild_key] = {"started": time.monotonic(), "paused_at": None, "duration": player.duration}
            await _post_now_playing(voice_client, guild_key, channel, title, player)

        except Exception as e:
            logger.error(f"Error playing {title}: {e}")
            embed = create_embed("❌ Playback Error", f"Couldn't play **{title}**. Skipping...", discord.Color.red())
            await channel.send(embed=embed)
            await asyncio.sleep(1)
            if bot_state.music_queues.get(guild_key):
                await play_next_song(voice_client, guild_key, channel)
            else:
                await _go_idle(voice_client, guild_key, channel)

    except Exception as e:
        logger.exception(f"Unexpected error in play_next_song: {e}")
        if guild_key in bot_state.music_queues:
            bot_state.music_queues[guild_key].clear()
        await _go_idle(voice_client, guild_key, channel)
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
@guild_required
@rate_limit
async def play_command(interaction: discord.Interaction, song_query: str):
    await interaction.response.defer()

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("❌ You must be in a voice channel.")

    voice_client = await _ensure_voice_connection(interaction)  # Outside the try: failures reach on_app_command_error

    guild_key = queue_key(interaction.guild_id)
    bot_state.cancel_idle(guild_key)
    bot_state.music_queues.setdefault(guild_key, deque())

    is_url, is_playlist_url = _classify_query(song_query)

    try:
        if is_playlist_url and await _enqueue_playlist(interaction, song_query, voice_client, guild_key):
            return
        await _enqueue_single_track(interaction, song_query, is_url, voice_client, guild_key)

    except Exception as e:
        logger.error(f"Error in play command: {e}")
        await interaction.followup.send("❌ Couldn't play that. Try a different search.")
    finally:
        if not bot_state.music_queues.get(guild_key):  # A failed request must not leave the bot connected with no timer
            await _go_idle(voice_client, guild_key, interaction.channel)

async def _send_stop(interaction, voice_client):
    guild_key = queue_key(interaction.guild_id)
    if is_active(voice_client):
        voice_client.stop()
    if guild_key in bot_state.music_queues:
        bot_state.music_queues[guild_key].clear()
    bot_state.end_music_session(guild_key)  # Same tick as stop(): after_play's call runs later and finds nothing to loop

    embed = create_embed("⏹️ Music Stopped", "Playback stopped and queue cleared.", discord.Color.orange())
    await interaction.response.send_message(embed=embed)
    await bot_state.retire_now_playing(guild_key)
    await _go_idle(voice_client, guild_key, interaction.channel)  # No after_play when nothing was playing

@bot.tree.command(name="stop", description="Stop and clear queue.")
@guild_required
async def stop_command(interaction: discord.Interaction):
    voice_client = await connected_voice_client(interaction)
    if voice_client is None:
        return
    await _send_stop(interaction, voice_client)

@bot.tree.command(name="leave", description="Leave voice channel.")
@guild_required
async def leave_command(interaction: discord.Interaction):
    voice_client = await connected_voice_client(interaction)
    if voice_client is None:
        return

    channel_name = voice_client.channel.name
    guild_key = queue_key(interaction.guild_id)
    bot_state.end_music_session(guild_key)
    await voice_client.disconnect()

    if guild_key in bot_state.music_queues:
        bot_state.music_queues[guild_key].clear()

    embed = create_embed("👋 Left Voice Channel", f"Disconnected from **{channel_name}**", discord.Color.blue())
    await interaction.response.send_message(embed=embed)
    await bot_state.retire_now_playing(guild_key)

def _queue_page(guild_key, voice_client, page: int) -> tuple:
    """Return (embed, page, pages), read fresh on every press: songs leave the queue and /shuffle replaces the deque."""
    titles = [title for _, title in bot_state.music_queues.get(guild_key) or ()]
    is_playing = voice_client and is_active(voice_client)
    current_song = bot_state.now_playing.get(guild_key)
    if not is_playing and not titles:
        return create_embed("\U0001F4CB Queue Empty", "No songs. Use `/play` to add!", discord.Color.orange()), 0, 1
    size = Config.QUEUE_PAGE_SIZE
    pages = max(1, -(-len(titles) // size))
    page = max(0, min(page, pages - 1))
    embed = create_embed("\U0001F3B5 Music Queue", "", discord.Color.blue())
    if is_playing and current_song:
        embed.add_field(name="\u25b6\ufe0f Now Playing", value=current_song[:100], inline=False)
    start = page * size
    for idx, title in enumerate(titles[start:start + size], start + 1):
        embed.add_field(name=f"#{idx}", value=title[:100], inline=False)
    if pages > 1:
        embed.set_footer(text=f"Page {page + 1}/{pages}")
    return embed, page, pages

class QueueView(RequesterView):
    def __init__(self, interaction: discord.Interaction, pages: int):
        super().__init__(interaction)
        self.page = 0
        self._sync(pages)

    def _sync(self, pages: int):
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= pages - 1

    async def _show(self, interaction, page: int):
        embed, self.page, pages = _queue_page(queue_key(interaction.guild_id), interaction.guild.voice_client, page)
        self._sync(pages)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction, button):
        await self._show(interaction, self.page - 1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        await self._show(interaction, self.page + 1)

@bot.tree.command(name="queue", description="View music queue.")
@guild_required
async def queue_command(interaction: discord.Interaction):
    embed, _, pages = _queue_page(queue_key(interaction.guild_id), interaction.guild.voice_client, 0)
    if pages > 1:
        return await interaction.response.send_message(embed=embed, view=QueueView(interaction, pages))
    await interaction.response.send_message(embed=embed)

async def _send_skip(interaction, voice_client):
    if not is_active(voice_client):
        return await interaction.response.send_message("❌ Nothing playing!", ephemeral=True)

    bot_state.skip_requested.add(queue_key(interaction.guild_id))
    voice_client.stop()
    embed = create_embed("⏭️ Skipped", "Skipped to next song!", discord.Color.blue())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="skip", description="Skip current song.")
@guild_required
async def skip_command(interaction: discord.Interaction):
    voice_client = await connected_voice_client(interaction)
    if voice_client is None:
        return
    await _send_skip(interaction, voice_client)

async def _send_shuffle(interaction):
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

@bot.tree.command(name="shuffle", description="Shuffle the music queue.")
@guild_required
async def shuffle_command(interaction: discord.Interaction):
    await _send_shuffle(interaction)

LOOP_CHOICES = [app_commands.Choice(name="Off", value="off"), app_commands.Choice(name="Song", value="song"), app_commands.Choice(name="Queue", value="queue")]

@bot.tree.command(name="loop", description="Loop the current song or the whole queue.")
@app_commands.describe(mode="Off, the current song, or the whole queue")
@app_commands.choices(mode=LOOP_CHOICES)
@guild_required
async def loop_command(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    voice_client = await connected_voice_client(interaction)  # Connected only: the session end is what resets the mode
    if voice_client is None:
        return
    guild_key = queue_key(interaction.guild_id)
    if mode.value == "off":
        bot_state.loop_modes.pop(guild_key, None)
    else:
        bot_state.loop_modes[guild_key] = mode.value
    await interaction.response.send_message(embed=create_embed("🔁 Loop", f"Loop mode: **{mode.name}**", discord.Color.blue()))

@bot.tree.command(name="pause", description="Pause the current song.")
@guild_required
async def pause_command(interaction: discord.Interaction):
    voice_client = await connected_voice_client(interaction)
    if voice_client is None:
        return
    if voice_client.is_paused():
        return await interaction.response.send_message("Already paused.", ephemeral=True)
    if not voice_client.is_playing():
        return await interaction.response.send_message("\u274c Nothing playing!", ephemeral=True)
    guild_key = queue_key(interaction.guild_id)
    _pause_playback(voice_client, guild_key)
    await interaction.response.send_message(embed=create_embed("\u23f8\ufe0f Paused", "Use `/resume` to continue.", discord.Color.blue()))
    await _sync_pause_button(guild_key)

@bot.tree.command(name="resume", description="Resume the paused song.")
@guild_required
async def resume_command(interaction: discord.Interaction):
    voice_client = await connected_voice_client(interaction)
    if voice_client is None:
        return
    if not voice_client.is_paused():
        return await interaction.response.send_message("Not paused.", ephemeral=True)
    guild_key = queue_key(interaction.guild_id)
    _resume_playback(voice_client, guild_key)
    await interaction.response.send_message(embed=create_embed("\u25b6\ufe0f Resumed", "Playback resumed.", discord.Color.blue()))
    await _sync_pause_button(guild_key)

@bot.tree.command(name="nowplaying", description="Show the current song and how far along it is.")
@guild_required
async def nowplaying_command(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    guild_key = queue_key(interaction.guild_id)
    title = bot_state.now_playing.get(guild_key)
    if not voice_client or not is_active(voice_client) or not title or guild_key not in bot_state.track_timing:
        return await interaction.response.send_message("\u274c Nothing playing!", ephemeral=True)
    embed = create_embed("\U0001F3B5 Now Playing", f"**{title}**", discord.Color.blue())
    elapsed = format_duration(_elapsed_seconds(guild_key))
    duration = bot_state.track_timing[guild_key]["duration"]
    embed.add_field(name="Elapsed", value=f"{elapsed} / {format_duration(int(duration))}" if duration else elapsed, inline=True)
    embed.add_field(name="Loop", value=bot_state.loop_modes.get(guild_key, "off").capitalize(), inline=True)
    if voice_client.is_paused():
        embed.set_footer(text="Paused")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="remove", description="Remove a song from the queue.")
@app_commands.describe(position="Queue position to remove (see /queue)")
@guild_required
async def remove_command(interaction: discord.Interaction, position: app_commands.Range[int, 1]):
    queue = bot_state.music_queues.get(queue_key(interaction.guild_id))
    if not queue:
        embed = create_embed("\U0001F4CB Queue Empty", "No songs to remove. Use `/play` to add.", discord.Color.orange())
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    if position > len(queue):
        return await interaction.response.send_message(f"\u274c Position must be 1 to {len(queue)}.", ephemeral=True)
    _, title = queue[position - 1]
    del queue[position - 1]  # No await between read and delete
    await interaction.response.send_message(embed=create_embed("\U0001F5D1\ufe0f Removed", f"**{title[:100]}**", discord.Color.green()))

playlist_group = app_commands.Group(name="playlist", description="Save and load your own playlists.")

async def _playlist_autocomplete(interaction: discord.Interaction, current: str):
    """Not rate-limited: Discord calls it per keystroke. db bounds it below the 3 second limit, and any failure shows no choices."""
    try:
        rows = await db.list_playlists(interaction.user.id)
    except db.StorageError:
        return []
    needle = current.casefold()
    names = [name[:Config.AUTOCOMPLETE_NAME_MAX] for name, _ in rows if needle in name.casefold()]
    return [app_commands.Choice(name=n, value=n) for n in names[:Config.AUTOCOMPLETE_MAX_CHOICES]]

SAVE_REPLIES = {
    db.EXISTS: "❌ You already have a playlist with that name. Delete it first.",
    db.FULL: f"❌ You can have {Config.PLAYLIST_MAX_PER_USER} playlists. Delete one first.",
    db.INVALID: "❌ That name isn't valid.",
}

@playlist_group.command(name="save", description="Save the current song and queue as a playlist.")
@app_commands.describe(name="Playlist name")
@guild_required
@rate_limit
@storage_required
async def playlist_save(interaction: discord.Interaction, name: app_commands.Range[str, 1, Config.PLAYLIST_NAME_MAX]):
    guild_key = queue_key(interaction.guild_id)
    current = bot_state.current_tracks.get(guild_key)
    tracks = ([current] if current else []) + list(bot_state.music_queues.get(guild_key) or ())  # Snapshot before any await
    if not tracks:
        return await interaction.response.send_message("❌ Nothing is playing or queued.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    outcome = await db.save_playlist(interaction.user.id, name, tracks)
    if outcome != db.SAVED:
        return await interaction.followup.send(SAVE_REPLIES[outcome], ephemeral=True)
    saved = min(len(tracks), Config.PLAYLIST_MAX_TRACKS)
    note = f" (first {saved} of {len(tracks)})" if saved < len(tracks) else ""
    await interaction.followup.send(embed=create_embed("💾 Playlist Saved", f"Saved **{saved}** songs{note}.", discord.Color.green()), ephemeral=True)

@playlist_group.command(name="load", description="Add one of your playlists to the queue.")
@app_commands.describe(name="Playlist name")
@app_commands.autocomplete(name=_playlist_autocomplete)
@guild_required
@rate_limit
@storage_required
async def playlist_load(interaction: discord.Interaction, name: str):
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message("❌ You must be in a voice channel.", ephemeral=True)
    tracks = await db.get_playlist(interaction.user.id, name)  # Before the defer: a storage failure stays private
    if tracks is None:
        return await interaction.response.send_message("❌ You have no playlist with that name.", ephemeral=True)
    await interaction.response.defer()
    voice_client = await _ensure_voice_connection(interaction)  # Outside the try, like /play: failures reach on_app_command_error
    guild_key = queue_key(interaction.guild_id)
    bot_state.cancel_idle(guild_key)
    bot_state.music_queues.setdefault(guild_key, deque()).extend(tracks)  # Never keep this deque: /shuffle replaces it
    try:
        await interaction.followup.send(embed=create_embed("✅ Playlist Added", f"Added **{len(tracks)}** songs!", discord.Color.green()))
        if not is_active(voice_client):
            await play_next_song(voice_client, guild_key, interaction.channel)
    finally:
        if not bot_state.music_queues.get(guild_key):  # Same rule as /play
            await _go_idle(voice_client, guild_key, interaction.channel)

@playlist_group.command(name="list", description="List your playlists.")
@rate_limit
@storage_required
async def playlist_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    rows = await db.list_playlists(interaction.user.id)
    text = "\n".join(f"**{name}** - {count} songs" for name, count in rows) or "No playlists yet. Use `/playlist save`."
    await interaction.followup.send(embed=create_embed("💾 Your Playlists", text, discord.Color.blue()), ephemeral=True)

@playlist_group.command(name="delete", description="Delete one of your playlists.")
@app_commands.describe(name="Playlist name")
@app_commands.autocomplete(name=_playlist_autocomplete)
@rate_limit
@storage_required
async def playlist_delete(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    removed = await db.delete_playlist(interaction.user.id, name)
    await interaction.followup.send("🗑️ Playlist deleted." if removed else "❌ You have no playlist with that name.", ephemeral=True)

bot.tree.add_command(playlist_group)

@tasks.loop(hours=Config.QUEUE_CLEANUP_HOURS)
async def cleanup_inactive_queues():
    """Drop queues for guilds the bot left or has no voice client in."""
    logger.info("Running music queue cleanup...")
    cleaned = 0
    for guild_id in list(bot_state.music_queues.keys()):
        guild = bot.get_guild(int(guild_id))
        if not guild or not guild.voice_client:
            bot_state.music_queues.pop(guild_id, None)
            bot_state.end_music_session(guild_id)
            await bot_state.retire_now_playing(guild_id)
            cleaned += 1

    if cleaned > 0:
        logger.info(f"Cleaned up {cleaned} inactive music queues")

bot_state.queue_cleanup_task = cleanup_inactive_queues
