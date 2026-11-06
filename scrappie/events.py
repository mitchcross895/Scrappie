import random

import discord
from discord import app_commands
from discord.ext import tasks

from . import db
from .bot import bot
from .log import logger
from .state import bot_state, queue_key
from .utils import create_embed

def servers_activity() -> discord.Activity:
    return discord.Activity(type=discord.ActivityType.listening, name=f"/help • {len(bot.guilds)} servers")

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

    await bot.change_presence(activity=servers_activity())

    if not status_update_task.is_running():
        status_update_task.start()

    # Start cleanup task if music is available
    queue_cleanup = bot_state.queue_cleanup_task
    if queue_cleanup is not None and not queue_cleanup.is_running():
        queue_cleanup.start()

    weekly_reset = bot_state.weekly_reset_task
    if weekly_reset is not None and not weekly_reset.is_running():
        weekly_reset.start()

    reminder = bot_state.reminder_task
    if reminder is not None and not reminder.is_running():
        reminder.start()

@bot.event
async def on_guild_join(guild):
    logger.info(f'📥 Joined: {guild.name}')
    await bot.change_presence(activity=servers_activity())

    if guild.system_channel:
        embed = create_embed("👋 Hello!", "Thanks for adding me!\n\nUse `/help` to see commands.", discord.Color.green())
        try:
            await guild.system_channel.send(embed=embed)
        except discord.Forbidden:
            pass

@bot.event
async def on_guild_remove(guild):
    logger.info(f'📤 Left: {guild.name}')
    guild_key = queue_key(guild.id)
    bot_state.music_queues.pop(guild_key, None)
    bot_state.end_music_session(guild_key)
    await bot_state.retire_now_playing(guild_key)
    if db.available():
        try:
            await db.forget_guild(guild.id)
        except db.StorageError:
            logger.warning(f"Couldn't delete stored data for guild {guild.id}")
    await bot.change_presence(activity=servers_activity())

@bot.event
async def on_command_error(ctx, error):
    logger.error(f'Command error: {error}', exc_info=error)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    command_name = interaction.command.name if interaction.command else "unknown"
    logger.error(f"Slash command error in {command_name}: {error}", exc_info=error)

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
    if member != bot.user:
        return

    if before.channel and not after.channel:
        guild_key = queue_key(before.channel.guild.id)
        bot_state.music_queues.pop(guild_key, None)
        bot_state.end_music_session(guild_key)
        await bot_state.retire_now_playing(guild_key)

@tasks.loop(minutes=30)
async def status_update_task():
    activities = [
        servers_activity(),
        discord.Activity(type=discord.ActivityType.playing, name="trivia games"),
        discord.Activity(type=discord.ActivityType.watching, name="the weather"),
    ]
    await bot.change_presence(activity=random.choice(activities))
