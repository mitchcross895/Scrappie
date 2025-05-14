import os
import re
import logging
import random
import html
import requests
import discord
import aiohttp
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button, Select
from flask import Flask
from openai import OpenAI
from spellchecker import SpellChecker
import randfacts    
from dotenv import load_dotenv
from threading import Thread
import python_weather
import asyncio
import datetime
from python_weather.errors import Error, RequestError
from typing import Dict, Optional, Any

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s: %(message)s")

load_dotenv()
DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DESTINY_OAUTH_URL = os.getenv("DESTINY_OAUTH_URL")
DESTINY_OAUTH_ID = os.getenv("DESTINY_OAUTH_ID")
DESTINY_OAUTH_SECRET = os.getenv("DESTINY_OAUTH_SECRET")

if not all([DISCORD_TOKEN, OPENAI_API_KEY]):
    logging.error("Missing one or more required environment variables!")
    exit(1)

app = Flask(__name__)

@app.route('/')
def home():
    return "Discord Bot is Running!"

SPELL = SpellChecker()
SPELL.word_frequency.load_text_file(
    os.path.join(os.path.dirname(__file__), "addedwords.txt")
)
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

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

class TriviaView(View):
    def __init__(self, user_id: int, options: list[str], correct: str, category: str, difficulty: str):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.correct = correct
        self.options = options
        self.message = None
        self.category = category
        self.difficulty = difficulty
        
        for idx, text in enumerate(options, start=1):
            btn = Button(label=str(idx), style=discord.ButtonStyle.primary, custom_id=str(idx))
            btn.callback = self.create_callback(idx, text)
            self.add_item(btn)
    
    def create_callback(self, idx: int, option: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message(
                    "This isn't your question! Start your own trivia with /trivia", 
                    ephemeral=True
                )
            
            for child in self.children:
                child.disabled = True
                if self.options[int(child.custom_id)-1] == self.correct:
                    child.style = discord.ButtonStyle.success
                elif child.custom_id == str(idx):
                    child.style = discord.ButtonStyle.danger
            
            await interaction.message.edit(view=self)
            
            if option == self.correct:
                embed = discord.Embed(
                    title="🎉 Correct!", 
                    description=f"Well done, {interaction.user.mention}!",
                    color=discord.Color.green()
                )
            else:
                embed = discord.Embed(
                    title="❌ Incorrect",
                    description=f"{interaction.user.mention} answered **{option}**, but the correct answer was **{self.correct}**.",
                    color=discord.Color.red()
                )
            
            embed.add_field(name="Difficulty", value=self.difficulty.capitalize())
            embed.add_field(name="Category", value=self.category)
            
            await interaction.response.send_message(embed=embed)
            self.stop()
        
        return callback
    
    async def on_timeout(self):
        if self.message is None:
            return
            
        for child in self.children:
            child.disabled = True
            if self.options[int(child.custom_id)-1] == self.correct:
                child.style = discord.ButtonStyle.success
        
        embed = self.message.embeds[0]
        embed.title = "⏰ Trivia Expired"
        embed.color = discord.Color.dark_gray()
        
        try:
            await self.message.edit(embed=embed, view=self)
            await self.message.reply(f"Time's up! The correct answer was **{self.correct}**.")
        except Exception as e:
            logging.error(f"Error updating expired trivia: {e}")


class TriviaCategorySelect(discord.ui.Select):
    def __init__(self, categories):
        options = [
            discord.SelectOption(label="Random", description="Any category", value="0")
        ]
        for category in categories[:24]:  
            options.append(discord.SelectOption(label=category["name"], value=str(category["id"])))
        super().__init__(
            placeholder="Select a category...",
            min_values=1,
            max_values=1,
            options=options
        )


class TriviaDifficultySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Random", description="Any difficulty", value="any"),
            discord.SelectOption(label="Easy",   description="Simple questions",   value="easy"),
            discord.SelectOption(label="Medium", description="Moderate difficulty", value="medium"),
            discord.SelectOption(label="Hard",   description="Challenging questions",value="hard"),
        ]
        super().__init__(
            placeholder="Select difficulty...",
            min_values=1,
            max_values=1,
            options=options
        )


class TriviaSetupView(View):
    def __init__(self, interaction: discord.Interaction, categories):
        super().__init__(timeout=60)
        self.interaction = interaction
        self.category = "0"
        self.difficulty = "any"

        self.category_select = TriviaCategorySelect(categories)
        self.category_select.callback = self.category_callback
        self.add_item(self.category_select)

        self.difficulty_select = TriviaDifficultySelect()
        self.difficulty_select.callback = self.difficulty_callback
        self.add_item(self.difficulty_select)

        self.start_button = Button(label="Start Trivia", style=discord.ButtonStyle.success)
        self.start_button.callback = self.start_callback
        self.add_item(self.start_button)
    
    async def category_callback(self, interaction: discord.Interaction):
        self.category = self.category_select.values[0]
        await interaction.response.defer()
    
    async def difficulty_callback(self, interaction: discord.Interaction):
        self.difficulty = self.difficulty_select.values[0]
        await interaction.response.defer()
    
    async def start_callback(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await fetch_and_display_trivia(interaction, category_id=self.category, difficulty=self.difficulty)
    
    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.interaction.edit_original_response(view=self)
        except Exception:
            pass


async def fetch_categories():
    """Fetch available trivia categories from the API"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://opentdb.com/api_category.php") as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("trivia_categories", [])
    except Exception as e:
        logging.error(f"Failed to fetch trivia categories: {e}")
    # Fallback if API is unavailable
    return [
        {"id": 9,  "name": "General Knowledge"},
        {"id": 21, "name": "Sports"},
        {"id": 22, "name": "Geography"},
        {"id": 23, "name": "History"}
    ]


async def fetch_and_display_trivia(interaction, category_id="0", difficulty="any"):
    """Fetch and display a trivia question with the given parameters"""
    url = "https://opentdb.com/api.php?amount=1&type=multiple"
    if category_id != "0":
        url += f"&category={category_id}"
    if difficulty != "any":
        url += f"&difficulty={difficulty}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return await interaction.followup.send(
                        "Sorry, the trivia service is unavailable right now. Please try again later."
                    )
                data = await response.json()
                if data["response_code"] != 0 or not data["results"]:
                    return await interaction.followup.send(
                        "No trivia questions found with those parameters. Try different options!"
                    )
                result    = data["results"][0]
                question  = html.unescape(result["question"])
                correct   = html.unescape(result["correct_answer"])
                incorrect = [html.unescape(ans) for ans in result["incorrect_answers"]]
                category  = result["category"]
                difficulty = result["difficulty"]

                options = incorrect + [correct]
                random.shuffle(options)

                view = TriviaView(interaction.user.id, options, correct, category, difficulty)
                embed = discord.Embed(
                    title=f"Trivia Time! ({difficulty.capitalize()})",
                    description=question,
                    color=discord.Color.blue()
                )
                embed.set_author(name=category)
                embed.set_footer(text=f"Requested by {interaction.user.display_name}")

                for idx, opt in enumerate(options, start=1):
                    embed.add_field(name=f"{idx}.", value=opt, inline=False)

                msg = await interaction.followup.send(embed=embed, view=view)
                view.message = msg
    except Exception as e:
        logging.error(f"Trivia error: {e}")
        await interaction.followup.send(
            "Sorry, something went wrong while fetching your trivia question. Please try again."
        )


ITEM_DEFINITIONS = {}
DESTINY_API_KEY = os.getenv("DESTINY_API_KEY")  
DEFAULT_MEMBERSHIP_TYPE = os.getenv("DEFAULT_MEMBERSHIP_TYPE")
DEFAULT_MEMBERSHIP_ID = os.getenv("DEFAULT_MEMBERSHIP_ID")
DEFAULT_CHARACTER_ID = os.getenv("DEFAULT_CHARACTER_ID")

async def fetch_destiny_definitions(definition_type: str) -> Dict:
    """
    Fetch and cache Destiny 2 manifest definitions
    
    Args:
        definition_type: The type of definition to fetch (e.g., "DestinyInventoryItemDefinition")
        
    Returns:
        Dictionary of definitions or empty dict if failed
    """
    global ITEM_DEFINITIONS
    
    if definition_type in ITEM_DEFINITIONS:
        return ITEM_DEFINITIONS[definition_type]
    
    try:
        base_url = "https://www.bungie.net/Platform"
        manifest_url = f"{base_url}/Destiny2/Manifest/"
        
        headers = {
            "X-API-Key": DESTINY_API_KEY
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(manifest_url, headers=headers) as response:
                if response.status != 200:
                    logging.error(f"Failed to get manifest: {response.status}")
                    return {}
                
                manifest_data = await response.json()
                
                content_paths = manifest_data["Response"]["jsonWorldContentPaths"]["en"]
                if definition_type not in content_paths:
                    logging.error(f"Definition type {definition_type} not found in manifest")
                    return {}
                
                definition_url = f"https://www.bungie.net{content_paths[definition_type]}"
                
                async with session.get(definition_url) as def_response:
                    if def_response.status != 200:
                        logging.error(f"Failed to get {definition_type}: {def_response.status}")
                        return {}
                    
                    definitions = await def_response.json()
                    ITEM_DEFINITIONS[definition_type] = definitions
                    logging.info(f"Cached {len(definitions)} {definition_type} definitions")
                    return definitions
                    
    except Exception as e:
        logging.error(f"Error fetching definitions: {e}")
        return {}

async def get_item_name(item_hash: int) -> str:
    """
    Get the display name of an item from its hash
    
    Args:
        item_hash: The hash ID of the item
        
    Returns:
        Item name or "Unknown Item" if not found
    """
    try:
        if isinstance(item_hash, str):
            try:
                item_hash = int(item_hash)
            except ValueError:
                return "Unknown Item"
                
        if item_hash < 0:
            item_hash = item_hash & 0xFFFFFFFF
            
        if "DestinyInventoryItemDefinition" in ITEM_DEFINITIONS:
            item_def = ITEM_DEFINITIONS["DestinyInventoryItemDefinition"].get(str(item_hash))
            if item_def:
                return item_def.get("displayProperties", {}).get("name", "Unknown Item")
        
        base_url = "https://www.bungie.net/Platform"
        item_url = f"{base_url}/Destiny2/Manifest/DestinyInventoryItemDefinition/{item_hash}/"
        
        headers = {
            "X-API-Key": DESTINY_API_KEY
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(item_url, headers=headers) as response:
                if response.status != 200:
                    return "Unknown Item"
                
                item_data = await response.json()
                return item_data.get("Response", {}).get("displayProperties", {}).get("name", "Unknown Item")
                
    except Exception as e:
        logging.error(f"Error getting item name: {e}")
        return "Unknown Item"

async def get_item_details(item_hash: int) -> Dict[str, Any]:
    """
    Get detailed information about an item
    
    Args:
        item_hash: The hash ID of the item
        
    Returns:
        Dictionary with item details including name, type, tier, and icon
    """
    try:
        if isinstance(item_hash, str):
            try:
                item_hash = int(item_hash)
            except ValueError:
                return {"name": "Unknown Item", "type": "Unknown", "tier": "Unknown", "icon": ""}
                
        if item_hash < 0:
            item_hash = item_hash & 0xFFFFFFFF
            
        item_def = None
        if "DestinyInventoryItemDefinition" in ITEM_DEFINITIONS:
            item_def = ITEM_DEFINITIONS["DestinyInventoryItemDefinition"].get(str(item_hash))
        
        if not item_def:
            base_url = "https://www.bungie.net/Platform"
            item_url = f"{base_url}/Destiny2/Manifest/DestinyInventoryItemDefinition/{item_hash}/"
            
            headers = {
                "X-API-Key": DESTINY_API_KEY
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(item_url, headers=headers) as response:
                    if response.status != 200:
                        return {"name": "Unknown Item", "type": "Unknown", "tier": "Unknown", "icon": ""}
                    
                    item_data = await response.json()
                    item_def = item_data.get("Response", {})
        
        if not item_def:
            return {"name": "Unknown Item", "type": "Unknown", "tier": "Unknown", "icon": ""}
            
        display_props = item_def.get("displayProperties", {})
        item_type = item_def.get("itemTypeDisplayName", "")
        item_tier = item_def.get("itemTypeAndTierDisplayName", "")
        item_icon = display_props.get("icon", "")
        if item_icon and not item_icon.startswith("http"):
            item_icon = f"https://www.bungie.net{item_icon}"
            
        return {
            "name": display_props.get("name", "Unknown Item"),
            "description": display_props.get("description", ""),
            "type": item_type,
            "tier": item_tier,
            "icon": item_icon,
            "hash": item_hash
        }
                
    except Exception as e:
        logging.error(f"Error getting item details: {e}")
        return {"name": "Unknown Item", "type": "Unknown", "tier": "Unknown", "icon": ""}

def categorize_item(item_details: Dict[str, Any]) -> str:
    """
    Categorize an item based on its details
    
    Args:
        item_details: Dictionary of item information
        
    Returns:
        Category string: "weapon", "armor", or "other"
    """
    name = item_details.get("name", "").lower()
    item_type = item_details.get("type", "").lower()
    
    weapon_types = ["auto rifle", "scout rifle", "pulse rifle", "hand cannon", "sidearm", 
                   "submachine gun", "shotgun", "sniper rifle", "fusion rifle", "rocket launcher",
                   "grenade launcher", "machine gun", "sword", "bow", "trace rifle", "linear fusion rifle",
                   "glaive"]
    
    if any(weapon in item_type for weapon in weapon_types) or "weapon" in item_type:
        return "weapon"
    
    armor_types = ["helmet", "gauntlet", "chest", "boot", "cloak", "mark", "bond", "arms", "chest armor",
                  "leg armor", "class item", "helmet armor"]
    
    if any(armor in item_type or armor in name for armor in armor_types) or "armor" in item_type:
        return "armor"
    
    return "other"

async def get_user_destiny_profile(user_id: int) -> Dict[str, Any]:
    """
    Get a user's saved Destiny profile from database or cache
    
    Args:
        user_id: Discord user ID
    
    Returns:
        Dictionary with membership_type, membership_id, and character_id
    """
    # This would ideally connect to a database to get user-specific saved profiles
    # For now, we'll use the defaults from .env as a placeholder
    return {
        "membership_type": DEFAULT_MEMBERSHIP_TYPE and int(DEFAULT_MEMBERSHIP_TYPE),
        "membership_id": DEFAULT_MEMBERSHIP_ID,
        "character_id": DEFAULT_CHARACTER_ID
    }

@app_commands.command(name="xur", description="Get Xûr's inventory for the weekend.")
async def xur_slash(
    interaction: discord.Interaction, 
    membership_type: Optional[int] = None, 
    membership_id: Optional[str] = None, 
    character_id: Optional[str] = None
):
    await interaction.response.defer()
    
    XUR_HASH = 2190858386
    
    if not all([membership_type, membership_id, character_id]):
        embed = discord.Embed(
            title="⚠️ Missing Parameters",
            description="To get Xûr's inventory, I need your Destiny 2 account information.",
            color=discord.Color.yellow()
        )
        embed.add_field(
            name="Command Usage", 
            value="`/xur [membership_type] [membership_id] [character_id]`", 
            inline=False
        )
        embed.add_field(
            name="Register Your Profile",
            value="Use `/register_destiny` to save your profile for future commands.",
            inline=False
        )
        embed.add_field(
            name="Setup .env File",
            value="You can set up default values in your .env file:\n```\nDEFAULT_MEMBERSHIP_TYPE=3\nDEFAULT_MEMBERSHIP_ID=your_id\nDEFAULT_CHARACTER_ID=your_char_id\n```",
            inline=False
        )
        embed.add_field(
            name="Membership Types",
            value="1: Xbox\n2: PSN\n3: Steam\n4: Blizzard\n5: Stadia\n6: Epic\n10: Demon\n254: BungieNext",
            inline=False
        )
        embed.add_field(
            name="How to find your IDs",
            value="Visit Bungie.net, sign in, and check your profile URL for membership ID.\nFor character ID, you'll need to use the Bungie API or a third-party tool.",
            inline=False
        )
        return await interaction.followup.send(embed=embed)
    
    try:
        await fetch_destiny_definitions("DestinyInventoryItemDefinition")
        
        base_url = "https://www.bungie.net/Platform"
        vendor_url = f"{base_url}/Destiny2/{membership_type}/Profile/{membership_id}/Character/{character_id}/Vendors/{XUR_HASH}/"
        
      
        components = "400,401,402"  
        vendor_url += f"?components={components}"
        
        headers = {
            "X-API-Key": DESTINY_API_KEY
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(vendor_url, headers=headers) as response:
                if response.status != 200:
                    error_data = await response.json()
                    error_message = error_data.get("Message", "Unknown error")
                    error_code = error_data.get("ErrorCode", 0)
                    
              
                    if error_code == 1601:  
                        return await interaction.followup.send("❌ Error: Invalid membership ID or type.")
                    elif error_code == 1643:  
                        return await interaction.followup.send("❌ Error: Character not found.")
                    elif error_code == 1627:  
                        return await interaction.followup.send("❌ Xûr is not available right now. He appears Friday through Tuesday reset.")
                    else:
                        return await interaction.followup.send(f"❌ API Error ({error_code}): {error_message}")
                
                data = await response.json()
                vendor_data = data.get("Response", {})
                
                if not vendor_data or "vendor" not in vendor_data:
                    return await interaction.followup.send("❌ Could not retrieve Xûr's inventory. He might not be available right now.")
                
                embed = discord.Embed(
                    title="🧙‍♂️ Xûr, Agent of the Nine",
                    description="*His will is not his own; he comes to bring gifts of the Nine.*",
                    color=discord.Color.dark_purple()
                )
                
                embed.set_thumbnail(url="https://www.bungie.net/common/destiny2_content/icons/e5656aa18ef40d4e6f5c9d8775cc177b.png")
                
                vendor_info = vendor_data.get("vendor", {}).get("data", {})
                refresh_date = vendor_info.get("nextRefreshDate", "Unknown")
                if refresh_date and refresh_date != "Unknown":
                    try:
                        refresh_date = datetime.datetime.strptime(refresh_date, "%Y-%m-%dT%H:%M:%SZ")
                        refresh_date = refresh_date.strftime("%A, %B %d at %H:%M UTC")
                    except Exception as e:
                        logging.error(f"Error formatting date: {e}")
                    
                embed.set_footer(text=f"Next refresh: {refresh_date}")
                
                sales_data = vendor_data.get("sales", {}).get("data", {})
                sales_items = sales_data.get("saleItems", {})
                
                categories = vendor_data.get("categories", {}).get("data", {}).get("categories", [])
                location = "Unknown"
                for category in categories:
                    if "location" in category.get("displayProperties", {}).get("name", "").lower():
                        location = category.get("displayProperties", {}).get("description", "Unknown")
                        break
                
                if location and location != "Unknown":
                    embed.add_field(
                        name="📍 Current Location",
                        value=location,
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="📍 Possible Locations",
                        value="• Tower Hangar\n• EDZ (Winding Cove)\n• Nessus (Watcher's Grave)",
                        inline=False
                    )
                
                if sales_items:
                    weapons = []
                    armor = []
                    other_items = []
                    
                    for item_hash, item_data in sales_items.items():
                        if "itemHash" not in item_data:
                            continue
                            
                        item_hash_id = item_data["itemHash"]
                        item_details = await get_item_details(item_hash_id)
                        
                        cost_text = "Free"
                        if "costs" in item_data and item_data["costs"]:
                            cost_items = []
                            for cost in item_data["costs"]:
                                quantity = cost.get("quantity", 0)
                                currency_hash = cost.get("itemHash", 0)
                                currency_name = await get_item_name(currency_hash)
                                cost_items.append(f"{quantity} {currency_name}")
                            
                            cost_text = ", ".join(cost_items)
                        
                        icon_url = item_details.get("icon", "")
                        item_name = item_details.get("name", "Unknown Item")
                        item_tier = item_details.get("tier", "")
                        
                        item_entry = f"**{item_name}**"
                        if item_tier:
                            item_entry += f" • *{item_tier}*"
                        item_entry += f"\nCost: {cost_text}"
                        
                        category = categorize_item(item_details)
                        
                        if category == "weapon":
                            weapons.append((item_entry, icon_url))
                        elif category == "armor":
                            armor.append((item_entry, icon_url))
                        else:
                            other_items.append((item_entry, icon_url))
                    
                    def format_category(items, name, emoji):
                        if not items:
                            return
                        
                        value = "\n\n".join([entry for entry, _ in items[:3]])
                        embed.add_field(
                            name=f"{emoji} {name} ({len(items)})",
                            value=value,
                            inline=False
                        )
                    
                    format_category(weapons, "Weapons", "🔫")
                    format_category(armor, "Armor", "🛡️")
                    format_category(other_items, "Other Items", "📦")
                    
                    total_items = len(weapons) + len(armor) + len(other_items)
                    shown_items = min(3, len(weapons)) + min(3, len(armor)) + min(3, len(other_items))
                    
                    if total_items > shown_items:
                        embed.add_field(
                            name="📑 And more...",
                            value=f"{total_items - shown_items} additional items not shown",
                            inline=False
                        )
                else:
                    embed.add_field(
                        name="No Items Found",
                        value="Could not retrieve Xûr's inventory items.",
                        inline=False
                    )
                
                await interaction.followup.send(embed=embed)
                
    except Exception as e:
        logging.error(f"Error retrieving Xûr data: {e}")
        await interaction.followup.send(f"❌ Error retrieving Xûr data: {str(e)}")

@bot.tree.command(name="fact", description="Get a random fact.")
async def fact_slash(interaction: discord.Interaction):
    await interaction.response.send_message(f"Did you know? {randfacts.get_fact()}")

@bot.tree.command(name="wiki", description="Search the Terraria Wiki for an entity page.")
async def wiki_slash(interaction: discord.Interaction, query: str):
    url  = f"https://terraria.wiki.gg/wiki/{query.replace(' ', '_')}"
    resp = requests.get(url)
    if resp.status_code == 200:
        await interaction.response.send_message(f"Here's the page: {url}")
    else:
        await interaction.response.send_message(f"No page found for **{query}**.")

@bot.tree.command(name="ping", description="Check the bot's latency.")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message("pong")

@bot.tree.command(name="number", description="Generate a random number between two values.")
async def number_slash(interaction: discord.Interaction, min_num: int, max_num: int):
    if min_num > max_num:
        return await interaction.response.send_message(
            "Invalid range! First number must be ≤ second.", ephemeral=True
        )
    await interaction.response.send_message(f"Here is your number: {random.randint(min_num, max_num)}")

@bot.tree.command(name="coin", description="Flip a coin.")
async def coin_slash(interaction: discord.Interaction):
    await interaction.response.send_message("Heads" if random.randint(0,1)==0 else "Tails")

@bot.tree.command(name="trivia", description="Answer a multiple choice trivia question.")
async def trivia_slash(interaction: discord.Interaction):
    await interaction.response.defer()
    categories = await fetch_categories()
    view       = TriviaSetupView(interaction, categories)
    embed      = discord.Embed(
        title="Trivia Setup",
        description="Choose a category and difficulty for your trivia question!",
        color=discord.Color.blue()
    )
    await interaction.followup.send(embed=embed, view=view)

@bot.tree.command(name="ask", description="Ask OpenAI a question")
async def ask_slash(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp   = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":question}],
            max_tokens=50
        )
        await interaction.followup.send(resp.choices[0].message.content)
    except Exception as e:
        logging.error(f"OpenAI error: {e}")
        await interaction.followup.send("Sorry, couldn't process that request.")
\

@bot.tree.command(name="weather", description="Look up the weather of your desired city.")
async def weather_slash(interaction: discord.Interaction, city: str):
    await interaction.response.defer()
    try:
        async with python_weather.Client(unit=python_weather.IMPERIAL) as client:
            weather = await client.get(city)
            weather_emoji = "🌤️"  
            if hasattr(weather.kind, "emoji"):
                weather_emoji = weather.kind.emoji
        
            embed = discord.Embed(
                title=f"{weather_emoji} Weather in {weather.location} - {weather.datetime.strftime('%A, %B %d')}",
                description=f"**{weather.description}**, {weather.temperature}°F",
                color=discord.Color.blue()
            )
            
            if weather.region and weather.country:
                embed.add_field(name="Location", value=f"{weather.region}, {weather.country}", inline=False)
            
            embed.add_field(name="Feels Like", value=f"{weather.feels_like}°F", inline=True)
            embed.add_field(name="Humidity", value=f"{weather.humidity}%", inline=True)
            
            wind_info = f"{weather.wind_speed} mph"
            if weather.wind_direction:
                direction_str = str(weather.wind_direction)
                wind_info += f" {direction_str}"
                if hasattr(weather.wind_direction, "emoji"):
                    wind_info += f" {weather.wind_direction.emoji}"
            embed.add_field(name="Wind", value=wind_info, inline=True)
            
            embed.add_field(name="Precipitation", value=f"{weather.precipitation} in", inline=True)
            embed.add_field(name="Pressure", value=f"{weather.pressure} in", inline=True)
            embed.add_field(name="Visibility", value=f"{weather.visibility} mi", inline=True)
            
            if weather.ultraviolet:
                uv_text = str(weather.ultraviolet)
                if hasattr(weather.ultraviolet, "index"):
                    uv_text = f"{uv_text} ({weather.ultraviolet.index})"
                embed.add_field(name="UV Index", value=uv_text, inline=True)
            
            if weather.daily_forecasts:
                forecast_text = ""
                
                for i, day_forecast in enumerate(weather.daily_forecasts[:3]):
                    if i == 0:
                        day_name = "Today"
                    elif i == 1:
                        day_name = "Tomorrow"
                    else:
                        if hasattr(day_forecast, 'date'):
                            day_name = day_forecast.date.strftime('%A')
                        else:
                            day_name = f"Day {i+1}"
                    
                    day_emoji = "🌤️"
                    if hasattr(day_forecast, "kind") and hasattr(day_forecast.kind, "emoji"):
                        day_emoji = day_forecast.kind.emoji
                    
                    day_text = f"{day_emoji} **{day_name}**: "
                    
                    if hasattr(day_forecast, 'description'):
                        day_text += f"{day_forecast.description}, "
                    elif hasattr(day_forecast, 'kind'):
                        day_text += f"{day_forecast.kind}, "
                    
                    logging.info(f"Day {i} forecast attributes: {dir(day_forecast)}")
                    
                    temp_high = None
                    if hasattr(day_forecast, 'highest'):
                        temp_high = day_forecast.highest
                    elif hasattr(day_forecast, 'high'):
                        temp_high = day_forecast.high
                    elif hasattr(day_forecast, 'temperature'):
                        temp_high = day_forecast.temperature
                    
                    temp_low = None
                    if hasattr(day_forecast, 'lowest'):
                        temp_low = day_forecast.lowest
                    elif hasattr(day_forecast, 'low'):
                        temp_low = day_forecast.low
                    
                    if temp_high is not None:
                        day_text += f"High: {temp_high}°F"
                        if temp_low is not None:
                            day_text += f", Low: {temp_low}°F"
                    else:
                        attrs = vars(day_forecast)
                        logging.info(f"Day {i} forecast dict: {attrs}")
                        
                        for attr_name, attr_value in attrs.items():
                            if 'temp' in attr_name.lower() or 'high' in attr_name.lower() or 'low' in attr_name.lower():
                                day_text += f"{attr_name}: {attr_value}°F, "
                    
                    forecast_text += day_text + "\n"
                
                embed.add_field(name="Forecast", value=forecast_text, inline=False)
            
            embed.set_footer(text=f"Data provided by python_weather • {weather.datetime.strftime('%H:%M')}")
            
            await interaction.followup.send(embed=embed)

    except RequestError as e:
        logging.error(f"Weather lookup error (status {e.status}): {str(e)}")
        await interaction.followup.send(f"Couldn't fetch weather for '{city}'. Server returned status code: {e.status}")
    except Error as e:
        logging.error(f"Weather lookup error: {str(e)}")
        await interaction.followup.send(f"Error getting weather for '{city}': {str(e)}")
    except Exception as e:
        logging.error(f"Unexpected error in weather command: {str(e)}")
        await interaction.followup.send(f"Couldn't fetch weather for '{city}'. Please try a valid city name.")

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    words = re.findall(r"[\w']+", message.content)
    miss  = SPELL.unknown(words)
    if miss:
        logging.debug(f"Misspelled words detected: {miss}")
        await message.channel.send(random.choice(MISSPELL_REPLIES))
    await bot.process_commands(message)

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user}')
    try:
        await bot.tree.sync()
        logging.info("Slash commands synced.")
    except Exception as e:
        logging.error(f"Error syncing commands: {e}")

def start_discord_bot():
    """Run the Discord bot (blocking)."""
    bot.run(DISCORD_TOKEN)

if __name__ != "__main__":
    Thread(target=start_discord_bot, daemon=False).start()

application = app

if __name__ == "__main__":
    Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=int(os.getenv("PORT", 5000)),
            debug=False,
            use_reloader=False
        ),
        daemon=False
    ).start()
    start_discord_bot()