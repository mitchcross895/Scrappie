"""Destiny 2 weekly raid post, /weekly and /item. Only lifecycle.py imports this, and only when BUNGIE_API_KEY is set."""
import datetime
import json
import os
import re
import unicodedata
from datetime import timezone
from functools import lru_cache
from pathlib import Path
from typing import List, NamedTuple, Optional

import discord
from discord import app_commands
from discord.ext import tasks

from .bot import bot
from .config import Config
from .log import logger
from .state import bot_state
from .utils import create_embed, rate_limit, safe_api_request

DATA_DIR = Path(__file__).with_name("data")
WEEKLY_MARKER = "Destiny 2 weekly reset"  # Footer start: how the duplicate check finds the bot's own post
REQUIRED_PERMISSIONS = ("view_channel", "send_messages", "embed_links", "read_message_history")
INTRINSIC_CATEGORY = 3956125808
DAMAGE_TYPES = {1: "Kinetic", 2: "Arc", 3: "Solar", 4: "Void", 6: "Stasis", 7: "Strand"}
ROTATION_GAP = 4  # The two weekly slots sit this many places apart on the rotation list
MAX_CHOICES = 25
MAX_CHOICE_LENGTH = 100

class ItemEntry(NamedTuple):
    key: str  # Normalised name
    name: str
    label: str  # e.g. "Hand Cannon" or "Hunter Helmet"
    hashes: List[int]  # Highest manifest index first; nothing marks a copy as current

# ---- Bundled data (lazy, cached) ----

@lru_cache(maxsize=1)
def load_raids() -> dict:
    return json.loads((DATA_DIR / "raids.json").read_text(encoding="utf-8"))

@lru_cache(maxsize=1)
def load_items() -> List[ItemEntry]:
    rows = json.loads((DATA_DIR / "items.json").read_text(encoding="utf-8"))
    return [ItemEntry(normalise(name), name, label, hashes) for name, label, hashes in rows]

@lru_cache(maxsize=1)
def items_by_hash() -> dict:
    return {entry.hashes[0]: entry for entry in load_items()}

# ---- Bungie API ----

async def bungie_get(path: str) -> Optional[dict]:
    """GET a Platform path and return its Response, or None. Paths are fixed strings plus integer hashes."""
    key = os.getenv("BUNGIE_API_KEY")
    if not key:
        return None
    data = await safe_api_request(Config.BUNGIE_API_ROOT + path, headers={"X-API-Key": key}, allow_redirects=False)
    if not data:
        return None  # safe_api_request logged it
    if data.get("ErrorCode") != 1 or "Response" not in data:
        logger.warning(f"Bungie {path} failed: ErrorCode {data.get('ErrorCode')} {data.get('ErrorStatus')}")
        return None
    return data["Response"]

# ---- Pure logic: time ----

def latest_reset(now: datetime.datetime) -> datetime.datetime:
    """The most recent Tuesday 17:00 UTC at or before now."""
    reset = now.replace(hour=Config.WEEKLY_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    reset -= datetime.timedelta(days=(now.weekday() - Config.WEEKLY_RESET_WEEKDAY) % 7)
    return reset if reset <= now else reset - datetime.timedelta(days=7)

def parse_time(value: Optional[str]) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None

# ---- Pure logic: raids ----

def clean_modifiers(hashes: list, data: dict) -> tuple:
    """Return (display names, unknown hash count). Names are deduplicated, order kept."""
    names = []
    unknown = 0
    for modifier in hashes:
        name = data["modifiers"].get(str(modifier))
        if name is None:
            unknown += modifier not in data["ignored_modifiers"]
        elif name not in names:
            names.append(name)
    return names, unknown

def featured_raids(milestones: dict, data: dict) -> List[dict]:
    """Raids holding a Weekly Raid Challenge objective, in rotation order."""
    weekly = set(data["weekly_objectives"])
    featured = []
    for milestone_hash, raid in data["raids"].items():
        milestone = milestones.get(milestone_hash) or {}
        activities = milestone.get("activities") or []
        if not any(weekly & set(a.get("challengeObjectiveHashes") or []) for a in activities):
            continue
        standard, master, unknown = [], [], 0
        for activity in activities:
            names, missing = clean_modifiers(activity.get("modifierHashes") or [], data)
            unknown += missing
            if activity.get("activityHash") in raid["master_activities"]:
                master = names
            else:
                standard = names
        has_master = any(a.get("activityHash") in raid["master_activities"] for a in activities)
        if unknown:
            logger.warning(f"{raid['name']}: {unknown} modifier(s) not in raids.json; regenerate the Destiny data")
        featured.append({
            "raid": raid,
            "start": parse_time(milestone.get("startDate")),
            "standard": standard,
            "master": [n for n in master if n not in standard],
            "has_master": has_master,
        })
    return sorted(featured, key=lambda f: data["rotation"].index(f["raid"]["name"]))

def is_fresh(featured: List[dict], reset: datetime.datetime) -> bool:
    return bool(featured) and all(f["start"] is not None and f["start"] >= reset for f in featured)

def forecast_next(names: List[str], rotation: List[str]) -> Optional[List[str]]:
    """Next week's pair: each slot moves one place. None unless the current pair fits the known pattern."""
    if len(set(names)) != 2 or not set(names) <= set(rotation):
        return None
    first, second = sorted(rotation.index(n) for n in names)
    if second - first not in (ROTATION_GAP, len(rotation) - ROTATION_GAP):
        return None
    return [rotation[(first + 1) % len(rotation)], rotation[(second + 1) % len(rotation)]]

def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"

def _modifier_text(entry: dict) -> str:
    lines = [f"**Modifiers:** {', '.join(entry['standard']) or 'None listed'}"]
    if entry["has_master"]:
        lines.append(f"**Master adds:** {', '.join(entry['master']) or 'None listed'}")
    return "\n".join(lines)

def _weapon_text(weapons: List[dict]) -> str:
    return ", ".join(
        f"**{w['name']}** (Exotic {w['type']})" if w["exotic"] else f"{w['name']} ({w['type']})" for w in weapons
    )

def weekly_spec(featured: List[dict], start: datetime.datetime, forecast: Optional[List[str]], footer: str) -> dict:
    """Plain data for one embed. Fields stay under Discord's limits; the worst pair is under 3,000 characters."""
    next_reset = start + datetime.timedelta(days=7)
    fields = []
    for entry in featured:
        raid, armor = entry["raid"], entry["raid"]["armor_set"]
        pieces = "\n".join(f"**{cls}:** {', '.join(names)}" for cls, names in armor["pieces"].items())
        bonuses = "\n".join(f"**{b['pieces']}-piece, {b['name']}:** {b['description']}" for b in armor["bonuses"])
        fields.append((raid["name"], _modifier_text(entry)))
        fields.append(("Weapons", _weapon_text(raid["weapons"])))
        fields.append((f"Armor: {armor['name']}", pieces))
        fields.append(("Set bonuses", bonuses))
    if forecast:
        fields.append(("Next week (predicted)", f"{' and '.join(forecast)}. Predicted from the community rotation pattern, not confirmed by Bungie."))
    return {
        "title": "Destiny 2 Weekly Reset",
        "description": f"Featured raids from <t:{int(start.timestamp())}:F>. Next reset <t:{int(next_reset.timestamp())}:R>.",
        "fields": [(clip(name, 256), clip(value, 1024)) for name, value in fields],
        "footer": clip(footer, 2048),
    }

def weekly_from_milestones(milestones: dict, footer: str, reset: Optional[datetime.datetime] = None) -> Optional[dict]:
    """Build the post, or return None if nothing is featured or (with reset) the data is stale."""
    data = load_raids()
    featured = featured_raids(milestones, data)
    if not featured or (reset is not None and not is_fresh(featured, reset)):
        return None
    start = reset or featured[0]["start"] or latest_reset(datetime.datetime.now(timezone.utc))
    return weekly_spec(featured, start, forecast_next([f["raid"]["name"] for f in featured], data["rotation"]), footer)

# ---- Pure logic: items ----

def normalise(text: str) -> str:
    """Accent, punctuation and case-insensitive form: jotunn finds Jotunn with its accent, praedyths finds Praedyth's."""
    stripped = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", "", stripped).casefold().strip()

def search_items(query: str, items: List[ItemEntry], limit: int = MAX_CHOICES) -> List[ItemEntry]:
    """Exact matches, then prefix, then word start, then substring."""
    needle = normalise(query)
    if not needle:
        return []
    ranked = []
    for entry in items:
        if needle in entry.key:
            if entry.key == needle:
                rank = 0
            elif entry.key.startswith(needle):
                rank = 1
            elif f" {needle}" in entry.key:
                rank = 2
            else:
                rank = 3
            ranked.append((rank, entry.key, entry.label, entry))
    ranked.sort(key=lambda row: row[:3])
    return [row[3] for row in ranked[:limit]]

def resolve_item(text: str, items: List[ItemEntry], by_hash: dict) -> Optional[ItemEntry]:
    """Autocomplete sends the primary hash as a string; typed text falls back to the best search hit."""
    if text.isdecimal() and int(text) in by_hash:
        return by_hash[int(text)]
    matches = search_items(text, items, limit=1)
    return matches[0] if matches else None

def choice_name(entry: ItemEntry) -> str:
    return clip(f"{entry.name} ({entry.label})", MAX_CHOICE_LENGTH)

def intrinsic_hash(item: dict) -> Optional[int]:
    sockets = item.get("sockets") or {}
    entries = sockets.get("socketEntries") or []
    for category in sockets.get("socketCategories") or []:
        if category.get("socketCategoryHash") == INTRINSIC_CATEGORY:
            for index in category.get("socketIndexes") or []:
                if index < len(entries) and entries[index].get("singleInitialItemHash"):
                    return entries[index]["singleInitialItemHash"]
    return None

def image_url(path: Optional[str]) -> Optional[str]:
    return Config.BUNGIE_ROOT + path if path and path.startswith("/") else None

def item_spec(item: dict, perk: Optional[dict], other_versions: int) -> dict:
    display = item.get("displayProperties") or {}
    fields = [("Type", item.get("itemTypeAndTierDisplayName") or item.get("itemTypeDisplayName") or "Unknown")]
    damage = DAMAGE_TYPES.get(item.get("defaultDamageType"))
    if damage:
        fields.append(("Damage", damage))
    perk_display = (perk or {}).get("displayProperties") or {}
    if perk_display.get("name"):
        fields.append((f"Intrinsic: {perk_display['name']}", perk_display.get("description") or "No description."))
    return {
        "title": clip(display.get("name") or "Unknown item", 256),
        "description": clip(item.get("flavorText") or "", 4096),
        "fields": [(clip(n, 256), clip(v, 1024)) for n, v in fields],
        "footer": f"Showing one of {other_versions + 1} versions" if other_versions else "",
        "thumbnail": image_url(display.get("icon")),
        "image": image_url(item.get("screenshot")),
    }

# ---- Discord ----

def build_embed(spec: dict) -> discord.Embed:
    embed = create_embed(spec["title"], spec["description"], discord.Color.purple())
    for name, value in spec["fields"]:
        embed.add_field(name=name, value=value, inline=False)
    if spec.get("footer"):
        embed.set_footer(text=spec["footer"])
    if spec.get("thumbnail"):
        embed.set_thumbnail(url=spec["thumbnail"])
    if spec.get("image"):
        embed.set_image(url=spec["image"])
    return embed

def error_embed(text: str) -> discord.Embed:
    return create_embed("\u274c Error", text, discord.Color.red())

async def _item_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    """Local only, so it needs no rate limit and can always answer inside Discord's 3 seconds."""
    return [app_commands.Choice(name=choice_name(e), value=str(e.hashes[0])) for e in search_items(current, load_items())]

@app_commands.command(name="item", description="Look up a Destiny 2 weapon or armor piece.")
@app_commands.describe(name="Item name")
@app_commands.autocomplete(name=_item_autocomplete)
@rate_limit
async def item_command(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    entry = resolve_item(name, load_items(), items_by_hash())
    if entry is None:
        return await interaction.followup.send(embed=error_embed("No weapon or armor matches that name."))
    item = await bungie_get(f"/Destiny2/Manifest/DestinyInventoryItemDefinition/{entry.hashes[0]}/")
    if item is None:
        return await interaction.followup.send(embed=error_embed("Couldn't reach Bungie. Try again later."))
    perk_hash = intrinsic_hash(item) if item.get("itemType") == 3 else None
    perk = await bungie_get(f"/Destiny2/Manifest/DestinyInventoryItemDefinition/{perk_hash}/") if perk_hash else None
    await interaction.followup.send(embed=build_embed(item_spec(item, perk, len(entry.hashes) - 1)))

@app_commands.command(name="weekly", description="Preview this week's Destiny 2 raid post (only you see it).")
@rate_limit
async def weekly_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    milestones = await bungie_get("/Destiny2/Milestones/")
    spec = weekly_from_milestones(milestones, "Preview of the weekly post") if milestones else None
    embed = build_embed(spec) if spec else error_embed("Couldn't get this week's raids. Try again later.")
    await interaction.followup.send(embed=embed, ephemeral=True)

def log_once(reset: datetime.datetime, reason: str, message: str) -> None:
    """Log each reason once per reset, so a 10-minute loop can't spam."""
    if bot_state.weekly_log_reset != reset:
        bot_state.weekly_log_reset = reset
        bot_state.weekly_logged.clear()
    if reason not in bot_state.weekly_logged:
        bot_state.weekly_logged.add(reason)
        logger.warning(f"Weekly post: {message}")

def channel_id() -> Optional[int]:
    value = (os.getenv("WEEKLY_RESET_CHANNEL_ID") or "").strip()
    return int(value) if value.isdecimal() else None

async def get_weekly_channel(reset: datetime.datetime):
    """The channel if it is usable; otherwise log once and return None."""
    target = channel_id()
    try:
        channel = bot.get_channel(target) or await bot.fetch_channel(target)
    except (discord.HTTPException, discord.InvalidData) as e:
        log_once(reset, "channel", f"can't reach channel {target}: {e}")
        return None
    guild = getattr(channel, "guild", None)
    if guild is None or not hasattr(channel, "history"):
        log_once(reset, "channel", f"channel {target} is not a server text channel")
        return None
    permissions = channel.permissions_for(guild.me)
    missing = [p for p in REQUIRED_PERMISSIONS if not getattr(permissions, p)]
    if missing:
        log_once(reset, "permissions", f"missing {', '.join(missing)} in channel {target}")
        return None
    return channel

async def already_posted(channel, reset: datetime.datetime) -> bool:
    async for message in channel.history(after=reset, limit=Config.WEEKLY_HISTORY_LIMIT):
        if message.author == bot.user and any((e.footer.text or "").startswith(WEEKLY_MARKER) for e in message.embeds):
            return True
    return False

async def run_weekly_check(now: datetime.datetime) -> None:
    reset = latest_reset(now)
    if bot_state.weekly_posted_reset == reset:
        return
    if now - reset > datetime.timedelta(hours=Config.WEEKLY_CATCHUP_HOURS):
        log_once(reset, "late", f"no post for {reset:%Y-%m-%d}, catch-up window passed")
        return
    channel = await get_weekly_channel(reset)
    if channel is None:
        return
    if await already_posted(channel, reset):
        bot_state.weekly_posted_reset = reset
        return
    milestones = await bungie_get("/Destiny2/Milestones/")
    spec = weekly_from_milestones(milestones, f"{WEEKLY_MARKER} {reset:%Y-%m-%d}", reset) if milestones else None
    if spec is None:
        log_once(reset, "not_ready", "Bungie data not ready for this reset, will retry")
        return
    try:
        await channel.send(embed=build_embed(spec))
    except discord.HTTPException as e:
        log_once(reset, "send", f"send failed: {e}")
        return
    bot_state.weekly_posted_reset = reset
    logger.info(f"Weekly post sent for {reset:%Y-%m-%d}")

@tasks.loop(minutes=Config.WEEKLY_CHECK_MINUTES)
async def weekly_reset_loop():
    try:
        await run_weekly_check(datetime.datetime.now(timezone.utc))
    except Exception as e:  # An uncaught exception would end the loop
        logger.error(f"Weekly post check failed: {e}", exc_info=True)

def setup() -> None:
    """Called once by lifecycle.run() after load_dotenv(), before the bot starts and syncs."""
    if not all((DATA_DIR / name).is_file() for name in ("raids.json", "items.json")):
        logger.error("Destiny features disabled: scrappie/data files are missing")
        return
    if bot.tree.get_command("item") is None:
        bot.tree.add_command(item_command)
        bot.tree.add_command(weekly_command)
    if channel_id() is None:
        logger.info("Weekly Destiny post disabled: WEEKLY_RESET_CHANNEL_ID is unset or not a number")
        return
    bot_state.weekly_reset_task = weekly_reset_loop