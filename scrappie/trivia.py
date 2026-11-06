import asyncio
import html
import random
import time
from typing import List, NamedTuple, Optional, Tuple

import discord
from discord import app_commands
from discord.ui import Button, View

from . import db
from .bot import bot
from .config import Config
from .log import logger
from .state import bot_state
from .utils import RequesterView, create_embed, guild_required, rate_limit, safe_api_request, storage_required

# Clip lengths that keep every embed inside Discord's limits even at 10 questions
QUESTION_LIMIT = 1000
ANSWER_LIMIT = 300
SUMMARY_QUESTION_LIMIT = 120
SUMMARY_ANSWER_LIMIT = 100

# OpenTDB category ids (https://opentdb.com/api_category.php)
CATEGORY_CHOICES = [
    app_commands.Choice(name="General Knowledge", value=9),
    app_commands.Choice(name="Entertainment: Books", value=10),
    app_commands.Choice(name="Entertainment: Film", value=11),
    app_commands.Choice(name="Entertainment: Music", value=12),
    app_commands.Choice(name="Entertainment: Musicals & Theatres", value=13),
    app_commands.Choice(name="Entertainment: Television", value=14),
    app_commands.Choice(name="Entertainment: Video Games", value=15),
    app_commands.Choice(name="Entertainment: Board Games", value=16),
    app_commands.Choice(name="Science & Nature", value=17),
    app_commands.Choice(name="Science: Computers", value=18),
    app_commands.Choice(name="Science: Mathematics", value=19),
    app_commands.Choice(name="Mythology", value=20),
    app_commands.Choice(name="Sports", value=21),
    app_commands.Choice(name="Geography", value=22),
    app_commands.Choice(name="History", value=23),
    app_commands.Choice(name="Politics", value=24),
    app_commands.Choice(name="Art", value=25),
    app_commands.Choice(name="Celebrities", value=26),
    app_commands.Choice(name="Animals", value=27),
    app_commands.Choice(name="Vehicles", value=28),
    app_commands.Choice(name="Entertainment: Comics", value=29),
    app_commands.Choice(name="Science: Gadgets", value=30),
    app_commands.Choice(name="Entertainment: Japanese Anime & Manga", value=31),
    app_commands.Choice(name="Entertainment: Cartoon & Animations", value=32),
]

DIFFICULTY_CHOICES = [
    app_commands.Choice(name="Easy", value="easy"),
    app_commands.Choice(name="Medium", value="medium"),
    app_commands.Choice(name="Hard", value="hard"),
]

class RoundSettings(NamedTuple):
    amount: int
    category_id: Optional[int]
    difficulty: Optional[str]
    category_name: str
    difficulty_name: str

ALREADY_RUNNING = "You already have a trivia round running! Finish it first."

def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"

class TriviaView(View):
    def __init__(self, user_id: int, question_data: dict):
        super().__init__(timeout=Config.TRIVIA_TIMEOUT)
        self.user_id = user_id
        self.correct = html.unescape(question_data['correct_answer'])
        self.options = [html.unescape(a) for a in question_data['incorrect_answers']] + [self.correct]
        random.shuffle(self.options)
        self.selected: Optional[int] = None  # 1-based option pressed; None until then
        self.answered = False

        for idx in range(1, len(self.options) + 1):
            btn = Button(label=str(idx), style=discord.ButtonStyle.primary, custom_id=f"option_{idx}", emoji="🔢")
            btn.callback = self.create_callback(idx)
            self.add_item(btn)

    @staticmethod
    def _option_index(button: Button) -> int:
        return int(button.custom_id.split('_')[1])

    @property
    def mark(self) -> str:
        if self.selected is None:
            return "⏰"
        return "✅" if self.options[self.selected - 1] == self.correct else "❌"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your question! Use `/trivia`.", ephemeral=True)
            return False
        return True

    def create_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            if self.answered:
                return await interaction.response.send_message("Already answered!", ephemeral=True)
            self.answered = True
            self.selected = idx
            self.stop()  # Wakes the round loop, which edits the message
            await interaction.response.defer()
        return callback

    def reveal(self):
        """Disable the buttons and colour them; the selected option is None on a timeout."""
        self.answered = True
        for child in self.children:
            child.disabled = True
            option_idx = self._option_index(child)

            if self.options[option_idx - 1] == self.correct:
                child.style = discord.ButtonStyle.success
                child.emoji = "✅"
            elif option_idx == self.selected:
                child.style = discord.ButtonStyle.danger
                child.emoji = "❌"
            else:
                child.style = discord.ButtonStyle.secondary

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        # Never reaches bot.tree.error. The round carries on: a pressed answer already stopped
        # the view, and otherwise the timeout does.
        logger.error(f"Trivia button error: {error}", exc_info=error)

def _trivia_params(amount: int, category_id: Optional[int], difficulty: Optional[str]) -> dict:
    params = {"amount": amount}
    if category_id is not None:
        params["category"] = category_id
    if difficulty is not None:
        params["difficulty"] = difficulty
    return params

def _response_code(data: Optional[dict]) -> Optional[int]:
    return data.get("response_code") if data else None

async def _spaced_request(params: dict) -> Optional[dict]:
    # OpenTDB limits each IP to about one request per 5 seconds
    async with bot_state.trivia_api_lock:
        wait = bot_state.trivia_last_call + Config.TRIVIA_API_SPACING - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return await safe_api_request(Config.TRIVIA_API, params)
        finally:
            bot_state.trivia_last_call = time.monotonic()

async def fetch_round(amount: int, category_id: Optional[int], difficulty: Optional[str]) -> Optional[dict]:
    """Fetch a whole round in one request, retrying once if OpenTDB is busy."""
    params = _trivia_params(amount, category_id, difficulty)
    data = await _spaced_request(params)
    if _response_code(data) in (None, 5):  # None covers HTTP 429; 5 is OpenTDB's rate limit code
        data = await _spaced_request(params)
    return data

def _failure_text(data: Optional[dict], amount: int) -> str:
    code = _response_code(data)
    if code in (None, 5):
        return "OpenTDB is busy right now. Try again in a moment."
    if code == 1:  # OpenTDB returns nothing rather than a partial round
        return f"OpenTDB doesn't have {amount} question(s) for those settings. Try fewer, or a different category or difficulty."
    return "Try different settings!"

def _set_requester(embed: discord.Embed, user: discord.abc.User):
    embed.set_footer(text=f"Requested by {user.display_name}", icon_url=user.display_avatar.url)

def _question_embed(question_data: dict, options: List[str], user: discord.abc.User, number: int, total: int) -> discord.Embed:
    question_text = _clip(html.unescape(question_data["question"]), QUESTION_LIMIT)
    category = html.unescape(question_data["category"])
    difficulty_level = question_data["difficulty"]

    difficulty_emojis = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}
    difficulty_emoji = difficulty_emojis.get(difficulty_level, "⚪")

    embed = create_embed(f"🧠 Trivia Question {number}/{total}", f"**{question_text}**", discord.Color.blurple())
    embed.add_field(name="📂 Category", value=category, inline=True)
    embed.add_field(name=f"{difficulty_emoji} Difficulty", value=difficulty_level.capitalize(), inline=True)
    embed.add_field(name="⏱️ Time", value=f"{Config.TRIVIA_TIMEOUT}s", inline=True)

    for idx, option in enumerate(options, 1):
        embed.add_field(name=f"{idx}️⃣ Option {idx}", value=_clip(option, ANSWER_LIMIT), inline=False)

    _set_requester(embed, user)
    return embed

def _reveal_embed(view: TriviaView, question_data: dict, user: discord.abc.User, number: int, total: int, score: int) -> discord.Embed:
    answer = _clip(view.correct, ANSWER_LIMIT)
    if view.mark == "✅":
        title, line, color = "🎉 Correct!", f"Well done, {user.mention}! Answer: **{answer}**", discord.Color.green()
    elif view.mark == "❌":
        title, line, color = "❌ Incorrect", f"{user.mention}, correct: **{answer}**", discord.Color.red()
    else:
        title, line, color = "⏰ Time's Up!", f"⏰ Time's up! Answer: **{answer}**", discord.Color.dark_gray()

    embed = _question_embed(question_data, view.options, user, number, total)
    embed.title = title
    embed.color = color
    embed.description = f"{embed.description}\n\n{line}"
    embed.add_field(name="🏆 Score", value=f"{score}/{number}", inline=False)
    return embed

def _summary_embed(results: List[Tuple[str, str, str]], total: int, category_name: str, difficulty_name: str, user: discord.abc.User) -> discord.Embed:
    played = len(results)
    marks = [mark for mark, _, _ in results]
    score = marks.count("✅")

    lines = []
    if played < total:
        lines.append(f"Ended early after {played} of {total} questions: {Config.TRIVIA_MAX_TIMEOUTS} timeouts in a row.\n")
    for number, (mark, question, answer) in enumerate(results, 1):
        lines.append(f"{mark} {number}. {_clip(question, SUMMARY_QUESTION_LIMIT)}\n**{_clip(answer, SUMMARY_ANSWER_LIMIT)}**")

    embed = create_embed("🏁 Trivia Results", "\n".join(lines), discord.Color.blurple())
    embed.add_field(name="🏆 Score", value=f"{score}/{played} ({round(100 * score / played)}%)", inline=True)
    embed.add_field(name="📂 Category", value=category_name, inline=True)
    embed.add_field(name="⚡ Difficulty", value=difficulty_name, inline=True)
    embed.add_field(name="⏱️ Timeouts", value=str(marks.count("⏰")), inline=True)
    _set_requester(embed, user)
    return embed

async def _edit_round(interaction: discord.Interaction, **kwargs) -> bool:
    """Edit the round's message. False means it can't be edited (e.g. deleted), so the round ends."""
    try:
        await interaction.edit_original_response(**kwargs)
        return True
    except Exception as e:
        logger.warning(f"Trivia round ended, could not edit its message: {e}")
        return False

async def _ask_questions(interaction: discord.Interaction, questions: List[dict], settings: RoundSettings, results: list):
    user = interaction.user
    total = len(questions)
    score = 0
    timeouts_in_a_row = 0

    for number, question_data in enumerate(questions, 1):
        view = TriviaView(user.id, question_data)
        embed = _question_embed(question_data, view.options, user, number, total)
        if not await _edit_round(interaction, embed=embed, view=view):
            return

        await view.wait()
        view.reveal()
        score += view.mark == "✅"
        timeouts_in_a_row = timeouts_in_a_row + 1 if view.mark == "⏰" else 0
        results.append((view.mark, html.unescape(question_data["question"]), view.correct))

        embed = _reveal_embed(view, question_data, user, number, total, score)
        if not await _edit_round(interaction, embed=embed, view=view):
            return
        await asyncio.sleep(Config.TRIVIA_REVEAL_SECONDS)

        if timeouts_in_a_row >= Config.TRIVIA_MAX_TIMEOUTS:
            break

    await _edit_round(interaction, embed=_summary_embed(results, total, settings.category_name, settings.difficulty_name, user), view=PlayAgainView(interaction, settings))

async def _record_round(interaction: discord.Interaction, results: List[Tuple[str, str, str]]):
    """Never raises: storage must not touch a round. Checks availability first; DMs are not recorded."""
    if not results or not db.available() or interaction.guild_id is None:
        return
    marks = [mark for mark, _, _ in results]
    try:
        await db.record_trivia_round(interaction.guild_id, interaction.user.id, [m == "✅" for m in marks], len(marks) - marks.count("⏰"))
    except Exception as e:
        logger.warning(f"Trivia score not saved for guild {interaction.guild_id} ({type(e).__name__})")

async def play_round(interaction: discord.Interaction, questions: List[dict], settings: RoundSettings):
    results = []
    try:
        await _ask_questions(interaction, questions, settings, results)
    finally:  # Also runs on the early returns after a failed edit
        await _record_round(interaction, results)

async def run_round(interaction: discord.Interaction, settings: RoundSettings):
    data = await fetch_round(settings.amount, settings.category_id, settings.difficulty)

    if _response_code(data) != 0 or not data.get("results"):
        embed = create_embed("\U0001F605 No Questions", _failure_text(data, settings.amount), discord.Color.orange())
        await _edit_round(interaction, embed=embed, view=None)
        return

    await play_round(interaction, data["results"], settings)

class PlayAgainView(RequesterView):
    def __init__(self, interaction: discord.Interaction, settings: RoundSettings):
        super().__init__(interaction)
        self.settings = settings

    @discord.ui.button(label="Play Again", style=discord.ButtonStyle.success)
    async def play_again(self, interaction: discord.Interaction, button: Button):
        user_id = interaction.user.id
        if user_id in bot_state.active_trivia:
            return await interaction.response.send_message(ALREADY_RUNNING, ephemeral=True)
        if bot_state.is_rate_limited(user_id):  # @rate_limit only wraps commands
            embed = create_embed("Rate Limited", "You're making requests too quickly. Please wait.", discord.Color.red())
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        bot_state.active_trivia.add(user_id)
        try:
            await interaction.response.defer(thinking=True)  # A new message: the results stay readable, and it becomes this interaction's original
            await self.close()
            await run_round(interaction, self.settings)
        finally:
            bot_state.active_trivia.discard(user_id)

@bot.tree.command(name="trivia", description="Play trivia game.")
@app_commands.describe(category="Question category (default: any)", difficulty="Question difficulty (default: any)", questions="Number of questions (default: 5)")
@app_commands.choices(category=CATEGORY_CHOICES, difficulty=DIFFICULTY_CHOICES)
@rate_limit
async def trivia_command(
    interaction: discord.Interaction,
    category: Optional[app_commands.Choice[int]] = None,
    difficulty: Optional[app_commands.Choice[str]] = None,
    questions: app_commands.Range[int, 1, Config.TRIVIA_MAX_QUESTIONS] = Config.TRIVIA_DEFAULT_QUESTIONS,
):
    user_id = interaction.user.id
    if user_id in bot_state.active_trivia:
        return await interaction.response.send_message(ALREADY_RUNNING, ephemeral=True)

    bot_state.active_trivia.add(user_id)
    try:
        await interaction.response.defer()

        category_id = category.value if category else None
        difficulty_value = difficulty.value if difficulty else None
        await run_round(interaction, RoundSettings(
            questions,
            category_id,
            difficulty_value,
            category.name if category else "Any Category",
            difficulty.name if difficulty else "Any Difficulty",
        ))
    finally:
        bot_state.active_trivia.discard(user_id)

@bot.tree.command(name="leaderboard", description="Show this server's trivia leaderboard.")
@guild_required
@rate_limit
@storage_required
async def leaderboard_command(interaction: discord.Interaction):
    # No defer: a public defer would make a storage failure public, and the read is capped below 3 seconds
    top, mine, rank = await db.leaderboard(interaction.guild_id, interaction.user.id)
    lines = [f"**{i}.** <@{r.user_id}> - {r.points} pts, best streak {r.best_streak}" for i, r in enumerate(top, 1)]
    embed = create_embed("🏆 Trivia Leaderboard", "\n".join(lines) or "No scores yet. Play `/trivia`!", discord.Color.gold())
    you = f"#{rank} with {mine.points} pts, streak {mine.current_streak} (best {mine.best_streak})" if mine else "No points yet"
    embed.add_field(name="Your rank", value=you, inline=False)
    await interaction.response.send_message(embed=embed)
