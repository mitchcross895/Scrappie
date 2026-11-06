"""/remind and /reminders. Delivery is at-most-once: a row is deleted before its send, so a success can't repeat after a restart and a failure can't repeat at all."""
import re
import time
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import tasks

from . import db
from .bot import bot
from .config import Config
from .log import logger
from .state import bot_state
from .utils import RequesterView, create_embed, rate_limit, storage_down, storage_required

UNITS = {
    **dict.fromkeys(("m", "min", "mins", "minute", "minutes"), 60),
    **dict.fromkeys(("h", "hr", "hrs", "hour", "hours"), 3600),
    **dict.fromkeys(("d", "day", "days"), 86400),
}
DURATION_PATTERN = re.compile(r"([0-9]{1,6})\s*([a-z]+)", re.IGNORECASE)
LIST_TEXT_LIMIT = 80

def parse_duration(text: str) -> Optional[int]:
    """Seconds, or None if malformed or out of bounds. One number and one unit only."""
    match = DURATION_PATTERN.fullmatch(text.strip())
    if not match or match[2].lower() not in UNITS:
        return None
    seconds = int(match[1]) * UNITS[match[2].lower()]
    return seconds if Config.REMINDER_MIN_SECONDS <= seconds <= Config.REMINDER_MAX_SECONDS else None

def reminder_text(row: db.Reminder, now: int) -> str:
    text = f"<@{row.user_id}> ⏰ Reminder: {row.message}"
    if now - row.due_at > Config.REMINDER_LATE_SECONDS:
        text += f"\n(Late: this was due <t:{row.due_at}:R>, the bot was offline.)"
    return text

async def deliver(row: db.Reminder, now: int) -> None:
    """Never raises. Original channel, then DM, then dropped and logged."""
    text = reminder_text(row, now)
    mentions = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(row.user_id)], replied_user=False)
    if row.channel_id is not None:
        try:
            await bot.get_partial_messageable(row.channel_id).send(text, allowed_mentions=mentions)
            return
        except Exception as e:  # Forbidden, NotFound (including a guild the bot left) or anything else: the row is already gone
            logger.info(f"Reminder {row.id}: channel {row.channel_id} failed ({type(e).__name__}), trying DM")
    try:
        user = bot.get_user(row.user_id) or await bot.fetch_user(row.user_id)
        await user.send(text, allowed_mentions=mentions)
    except Exception as e:
        logger.warning(f"Reminder {row.id} for user {row.user_id} dropped: DM failed ({type(e).__name__})")

async def run_reminder_check(now: int) -> None:
    if not db.available():
        return
    for row in await db.due_reminders(now, Config.REMINDER_BATCH_SIZE):
        if await db.take_reminder(row.id):  # Committed before the send; False means cancelled meanwhile
            await deliver(row, now)

@tasks.loop(seconds=Config.REMINDER_POLL_SECONDS)
async def reminder_loop():
    try:
        await run_reminder_check(int(time.time()))
    except db.StorageError:
        pass  # Already logged by db; the next tick retries
    except Exception as e:  # An uncaught exception would end the loop
        logger.error(f"Reminder check failed: {e}", exc_info=True)

bot_state.reminder_task = reminder_loop

@bot.tree.command(name="remind", description="Set a reminder, like 10m, 2h or 1d (up to 30 days).")
@app_commands.describe(duration="How long from now: 10m, 2h, 1d", message="What to remind you about")
@app_commands.rename(duration="time")
@rate_limit
@storage_required
async def remind_command(interaction: discord.Interaction, duration: app_commands.Range[str, 1, 20], message: app_commands.Range[str, 1, Config.REMINDER_MESSAGE_MAX]):
    seconds = parse_duration(duration)
    if seconds is None:
        embed = create_embed("❌ Invalid Time", "Use one number and unit, like 10m, 2h or 1d. From 1 minute to 30 days.", discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    due_at = int(time.time()) + seconds
    channel_id = interaction.channel_id if interaction.guild_id is not None else None  # None delivers by DM
    if await db.add_reminder(interaction.user.id, channel_id, due_at, message) is None:
        embed = create_embed("❌ Too Many Reminders", f"You can have {Config.REMINDER_MAX_ACTIVE} active. Cancel one with `/reminders`.", discord.Color.red())
    else:
        embed = create_embed("⏰ Reminder Set", f"<t:{due_at}:F> (<t:{due_at}:R>)", discord.Color.green())
    await interaction.followup.send(embed=embed, ephemeral=True)

def _one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"

def _list_embed(rows: List[db.Reminder]) -> discord.Embed:
    lines = [f"<t:{r.due_at}:R> - {_one_line(r.message, LIST_TEXT_LIMIT)}" for r in rows]
    return create_embed("⏰ Your Reminders", "\n".join(lines) or "No active reminders.", discord.Color.blue())

def _options(rows: List[db.Reminder]) -> List[discord.SelectOption]:
    return [discord.SelectOption(label=_one_line(r.message, 100), value=str(r.id)) for r in rows[:25]]

class ReminderListView(RequesterView):
    def __init__(self, interaction: discord.Interaction, rows: List[db.Reminder]):
        super().__init__(interaction)
        self.picker = discord.ui.Select(placeholder="Cancel a reminder", options=_options(rows))
        self.picker.callback = self.pick
        self.add_item(self.picker)

    async def pick(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            await db.cancel_reminder(int(self.picker.values[0]), interaction.user.id)  # False just means it already fired
            rows = await db.list_reminders(interaction.user.id)
        except db.StorageError:
            return await storage_down(interaction)
        if rows:
            self.picker.options = _options(rows)
            await interaction.edit_original_response(embed=_list_embed(rows), view=self)
        else:
            self.stop()
            await interaction.edit_original_response(embed=_list_embed(rows), view=None)

@bot.tree.command(name="reminders", description="List your reminders and cancel them.")
@rate_limit
@storage_required
async def reminders_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    rows = await db.list_reminders(interaction.user.id)
    kwargs = {"view": ReminderListView(interaction, rows)} if rows else {}  # followup.send rejects view=None
    await interaction.followup.send(embed=_list_embed(rows), ephemeral=True, **kwargs)
