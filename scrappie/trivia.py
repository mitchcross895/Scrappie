import html
import random
from typing import Any, Dict, List

import discord
from discord.ui import Button, Select, View

from .bot import bot
from .config import Config
from .log import logger
from .state import bot_state
from .utils import create_embed, rate_limit, safe_api_request

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

    @staticmethod
    def _option_index(button: Button) -> int:
        return int(button.custom_id.split('_')[1])

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
            option_idx = self._option_index(child)
            option_text = self.options[option_idx - 1]

            if option_text == self.correct:
                child.style = discord.ButtonStyle.success
                child.emoji = "✅"
            elif option_idx == idx:
                child.style = discord.ButtonStyle.danger
                child.emoji = "❌"
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
            option_idx = self._option_index(child)
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
        self.stop()
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

async def fetch_categories() -> List[Dict[str, Any]]:
    if bot_state.trivia_categories:
        return bot_state.trivia_categories

    data = await safe_api_request(Config.TRIVIA_CATEGORIES_API)

    if data and data.get("trivia_categories"):
        bot_state.trivia_categories = data["trivia_categories"]
        return bot_state.trivia_categories

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

def _trivia_params(category_id: str, difficulty: str) -> dict:
    params = {}
    if category_id != "0":
        params["category"] = category_id
    if difficulty != "any":
        params["difficulty"] = difficulty
    return params

def _question_embed(question_data: dict, options: List[str], user: discord.abc.User) -> discord.Embed:
    question_text = html.unescape(question_data["question"])
    category = question_data["category"]
    difficulty_level = question_data["difficulty"]

    difficulty_emojis = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}
    difficulty_emoji = difficulty_emojis.get(difficulty_level, "⚪")

    embed = create_embed("🧠 Trivia Question", f"**{question_text}**", discord.Color.blurple())
    embed.add_field(name="📂 Category", value=category, inline=True)
    embed.add_field(name=f"{difficulty_emoji} Difficulty", value=difficulty_level.capitalize(), inline=True)
    embed.add_field(name="⏱️ Time", value=f"{Config.TRIVIA_TIMEOUT}s", inline=True)

    for idx, option in enumerate(options, 1):
        embed.add_field(name=f"{idx}️⃣ Option {idx}", value=option, inline=False)

    embed.set_footer(text=f"Requested by {user.display_name}", icon_url=user.display_avatar.url)
    return embed

async def fetch_and_display_trivia(interaction: discord.Interaction, category_id: str = "0", difficulty: str = "any"):
    data = await safe_api_request(Config.TRIVIA_API, _trivia_params(category_id, difficulty))

    if not data or data.get("response_code") != 0 or not data.get("results"):
        embed = create_embed("😅 No Questions", "Try different settings!", discord.Color.orange())
        return await interaction.edit_original_response(embed=embed, view=None)

    question_data = data["results"][0]
    view = TriviaView(interaction.user.id, question_data)
    embed = _question_embed(question_data, view.options, interaction.user)

    msg = await interaction.edit_original_response(embed=embed, view=view)
    view.message = msg

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
