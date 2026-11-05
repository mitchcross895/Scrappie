"""Destiny 2 weekly raid and dungeon post, /weekly and /item. Only lifecycle.py imports this, and only when BUNGIE_API_KEY is set."""
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
UNCONFIRMED_MODIFIERS = "**Modifiers:** Shown once Bungie confirms this dungeon"
OMITTED_PIECES = "Use /item to look up each piece."
PREDICTED_NOTE = "Predicted from the community rotation pattern, not confirmed by Bungie."
DUNGEON_LOGS = {
    "no_account": "dungeon account not configured, using predicted dungeons",
    "call_failed": "couldn't read the dungeon account, using predicted dungeons",
    "no_data": "no character activity data (check account privacy), using predicted dungeons",
    "empty": "no dungeon challenges on any character, using predicted dungeons",
    "partial": "only one dungeon visible, using predicted dungeons",
    "cached": "profile data predates the reset, using predicted dungeons",
    "stale": "live dungeons match last week's expected pair, using predicted dungeons",
    "disagree": "live dungeons differ from the cycle, live wins; check DUNGEON_CYCLE",
}

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

async def bungie_get(path: str, quiet: bool = False) -> Optional[dict]:
    """GET a Platform path and return its Response, or None. Paths are fixed strings plus integers.
    quiet logs nothing: profile paths hold the membership ID, so the caller logs a fixed message."""
    key = os.getenv("BUNGIE_API_KEY")
    if not key:
        return None
    data = await safe_api_request(Config.BUNGIE_API_ROOT + path, headers={"X-API-Key": key}, allow_redirects=False, quiet=quiet)
    if not data:
        return None  # safe_api_request logged it unless quiet
    if data.get("ErrorCode") != 1 or "Response" not in data:
        if not quiet:
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

def _activity_fields(title: str, modifiers: str, weapons: str, armor: dict) -> List[tuple]:
    pieces = "\n".join(f"**{cls}:** {', '.join(names)}" for cls, names in armor["pieces"].items())
    bonuses = "\n".join(f"**{b['pieces']}-piece, {b['name']}:** {b['description']}" for b in armor["bonuses"])
    return [(title, modifiers), ("Weapons", weapons), (f"Armor: {armor['name']}", pieces), ("Set bonuses", bonuses)]

def weekly_spec(featured: List[dict], start: datetime.datetime, forecast: Optional[List[str]], footer: str) -> dict:
    """Plain data for one embed. Fields stay under Discord's limits; the worst pair is under 3,000 characters."""
    next_reset = start + datetime.timedelta(days=7)
    fields = []
    for entry in featured:
        raid = entry["raid"]
        fields.extend(_activity_fields(raid["name"], _modifier_text(entry), _weapon_text(raid["weapons"]), raid["armor_set"]))
    if forecast:
        fields.append(("Next week's raids (predicted)", f"{' and '.join(forecast)}. {PREDICTED_NOTE}"))
    return {
        "title": "Destiny 2 Weekly Reset",
        "description": f"Featured activities from <t:{int(start.timestamp())}:F>. Next reset <t:{int(next_reset.timestamp())}:R>.",
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

# ---- Pure logic: dungeons ----

def cycle_pair(reset: datetime.datetime, data: dict, offset: int = 0) -> List[str]:
    """The pair for a reset by whole weeks from the anchor, in UTC. Offsets give the weeks either side."""
    weeks = (reset - parse_time(data["dungeon_cycle_anchor"])) // datetime.timedelta(days=7)
    return data["dungeon_cycle"][(weeks + offset) % len(data["dungeon_cycle"])]

def live_dungeons(profile: Optional[dict], data: dict) -> dict:
    """Dungeon name to modifiers, across all characters: one that has played can lose its challenges."""
    characters = ((profile or {}).get("characterActivities") or {}).get("data") or {}
    found = {}
    for character in characters.values():
        for activity in character.get("availableActivities") or []:
            names = {data["dungeon_objectives"].get(str((c.get("objective") or {}).get("objectiveHash")))
                     for c in activity.get("challenges") or []} - {None}
            for name in names:
                entry = found.setdefault(name, {"standard": [], "master": [], "has_master": False, "unknown": 0})
                labels, missing = clean_modifiers(activity.get("modifierHashes") or [], data)
                is_master = activity.get("activityHash") in data["dungeons"][name]["master_activities"]
                entry["unknown"] = max(entry["unknown"], missing)
                entry["has_master"] = entry["has_master"] or is_master
                target = entry["master"] if is_master else entry["standard"]
                target.extend(n for n in labels if n not in target)
    for entry in found.values():
        entry["master"] = [n for n in entry["master"] if n not in entry["standard"]]
    return found

def dungeon_section(profile: Optional[dict], reset: datetime.datetime, data: dict, settled: bool) -> dict:
    """This week's dungeons. settled means the wait is over, so a section that isn't confirmed can still post."""
    expected = cycle_pair(reset, data)
    live = live_dungeons(profile, data)
    minted = parse_time((profile or {}).get("responseMintedTimestamp"))
    if profile is None:
        reason = "call_failed"
    elif not (profile.get("characterActivities") or {}).get("data"):
        reason = "no_data"
    elif not live:
        reason = "empty"
    elif len(live) < 2:
        reason = "partial"
    elif minted is None or minted < reset:
        reason = "cached"
    elif set(live) == set(expected):
        reason = None
    elif set(live) == set(cycle_pair(reset, data, -1)):
        reason = "stale"
    else:
        reason = "disagree"
    ready = reason is None or settled
    if reason is None or (reason == "disagree" and settled):
        names = [n for n in data["dungeons"] if n in live]
        cycle = data["dungeon_cycle"]
        index = next((i for i, pair in enumerate(cycle) if set(pair) == set(names)), None)
        forecast = None if index is None else cycle[(index + 1) % len(cycle)]
        return {"status": "confirmed", "names": names, "entries": live, "forecast": forecast, "ready": ready, "reason": reason}
    return {"status": "predicted", "names": expected, "entries": {}, "forecast": cycle_pair(reset, data, 1), "ready": ready, "reason": reason}

def _dungeon_weapon_text(weapons: List[dict]) -> str:
    if not weapons:
        return "None credited to this dungeon in Bungie's data."
    text = _weapon_text(weapons)
    return text if len(weapons) > 1 else f"{text}. Bungie's data credits no other weapons to this dungeon."

def embed_length(spec: dict) -> int:
    return len(spec["title"]) + len(spec["description"]) + len(spec.get("footer") or "") + sum(len(n) + len(v) for n, v in spec["fields"])

def fit_embed(spec: dict, first_dungeon_field: int) -> dict:
    """Keep one message under the limit: dungeon piece names go first, then fields from the end."""
    fields = list(spec["fields"])
    fitted = {**spec, "fields": fields}
    for index in range(first_dungeon_field, len(fields)):
        if embed_length(fitted) <= Config.WEEKLY_EMBED_LIMIT:
            return fitted
        if fields[index][0].startswith("Armor: "):
            fields[index] = (fields[index][0], OMITTED_PIECES)
    while embed_length(fitted) > Config.WEEKLY_EMBED_LIMIT:
        fields.pop()
    return fitted

def add_dungeons(spec: dict, section: Optional[dict]) -> dict:
    """Append the dungeon fields to a raid spec. None leaves the raid post as it is."""
    if section is None:
        return spec
    data = load_raids()
    predicted = section["status"] == "predicted"
    fields = []
    for name in section["names"]:
        dungeon = data["dungeons"][name]
        title = f"Dungeon: {name} (predicted)" if predicted else f"Dungeon: {name}"
        modifiers = UNCONFIRMED_MODIFIERS if predicted else _modifier_text(section["entries"][name])
        fields.extend(_activity_fields(title, modifiers, _dungeon_weapon_text(dungeon["weapons"]), dungeon["armor_set"]))
    if section["forecast"]:
        fields.append(("Next week's dungeons (predicted)", f"{' and '.join(section['forecast'])}. {PREDICTED_NOTE}"))
    note = f"\nThis week's dungeons: {PREDICTED_NOTE}" if predicted else ""
    updated = {**spec, "description": spec["description"] + note,
               "fields": spec["fields"] + [(clip(n, 256), clip(v, 1024)) for n, v in fields]}
    return fit_embed(updated, len(spec["fields"]))

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

def item_spec(item: dict, perk: Optional[dict], other_versions: int, item_hash: int) -> dict:
    display = item.get("displayProperties") or {}
    fields = [("Type", item.get("itemTypeAndTierDisplayName") or item.get("itemTypeDisplayName") or "Unknown")]
    damage = DAMAGE_TYPES.get(item.get("defaultDamageType"))
    if damage:
        fields.append(("Damage", damage))
    perk_display = (perk or {}).get("displayProperties") or {}
    if perk_display.get("name"):
        fields.append((f"Intrinsic: {perk_display['name']}", perk_display.get("description") or "No description."))
    fields.append(("More info", f"[View on light.gg]({Config.LIGHT_GG_ITEM_URL.format(item_hash)})"))
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
    await interaction.followup.send(embed=build_embed(item_spec(item, perk, len(entry.hashes) - 1, entry.hashes[0])))

@app_commands.command(name="weekly", description="Preview this week's Destiny 2 raid and dungeon post (only you see it).")
@rate_limit
async def weekly_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    milestones = await bungie_get("/Destiny2/Milestones/")
    spec = weekly_from_milestones(milestones, "Preview of the weekly post") if milestones else None
    if spec:
        now = datetime.datetime.now(timezone.utc)
        reset = latest_reset(now)
        settled = now - reset >= datetime.timedelta(minutes=Config.WEEKLY_DUNGEON_WAIT_MINUTES)
        spec = add_dungeons(spec, await get_dungeons(reset, settled))
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

def profile_path() -> Optional[str]:
    """Read at call time, like the key. Digits only, so the path stays a fixed string plus integers."""
    kind = (os.getenv("DESTINY_MEMBERSHIP_TYPE") or "").strip()
    member = (os.getenv("DESTINY_MEMBERSHIP_ID") or "").strip()
    return f"/Destiny2/{kind}/Profile/{member}/?components=204" if kind.isdecimal() and member.isdecimal() else None

async def get_dungeons(reset: datetime.datetime, settled: bool) -> Optional[dict]:
    """Never raises and never logs the path. None means raids only."""
    path = profile_path()
    profile = await bungie_get(path, quiet=True) if path else None
    try:
        section = dungeon_section(profile, reset, load_raids(), settled or path is None)
    except Exception as e:  # Unexpected shape or old data must not stop the raid post
        log_once(reset, "dungeon_error", f"dungeon section failed ({type(e).__name__}), posting raids only")
        return None
    if section["ready"]:
        reason = section["reason"] if path else "no_account"
        if reason:
            log_once(reset, f"dungeon_{reason}", DUNGEON_LOGS[reason])
        if any(e["unknown"] for e in section["entries"].values()):
            log_once(reset, "dungeon_modifiers", "dungeon modifier(s) not in raids.json; regenerate the Destiny data")
    return section

def dungeon_wait_over(now: datetime.datetime, reset: datetime.datetime) -> bool:
    """The wait starts the first time raids are fresh, and can't run past the catch-up window."""
    if not bot_state.weekly_raids_ready or bot_state.weekly_raids_ready[0] != reset:
        bot_state.weekly_raids_ready = (reset, now)
    wait_end = bot_state.weekly_raids_ready[1] + datetime.timedelta(minutes=Config.WEEKLY_DUNGEON_WAIT_MINUTES)
    last_tick = reset + datetime.timedelta(hours=Config.WEEKLY_CATCHUP_HOURS, minutes=-2 * Config.WEEKLY_CHECK_MINUTES)
    return now >= min(wait_end, last_tick)

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
    section = await get_dungeons(reset, dungeon_wait_over(now, reset))
    if section is not None and not section["ready"]:
        return  # A later tick retries; dungeon_wait_over bounds this
    spec = add_dungeons(spec, section)
    try:
        await channel.send(embed=build_embed(spec))
    except discord.HTTPException as e:
        log_once(reset, "send", f"send failed: {e}")
        return
    bot_state.weekly_posted_reset = reset
    logger.info(f"Weekly post sent for {reset:%Y-%m-%d} (dungeons: {section['status'] if section else 'omitted'})")

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
    if profile_path() is None:
        logger.info("Live dungeon data disabled: DESTINY_MEMBERSHIP_TYPE or DESTINY_MEMBERSHIP_ID is unset, dungeons will be predicted")
    if channel_id() is None:
        logger.info("Weekly Destiny post disabled: WEEKLY_RESET_CHANNEL_ID is unset or not a number")
        return
    bot_state.weekly_reset_task = weekly_reset_loop