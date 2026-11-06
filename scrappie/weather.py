from typing import Any, NamedTuple, Optional

import discord
import python_weather
from discord import app_commands
from python_weather.errors import Error, RequestError

from .bot import bot
from .config import Config
from .log import logger
from .utils import create_embed, rate_limit

class Units(NamedTuple):
    client: Any; temp: str; speed: str; rain: str; pressure: str; distance: str; celsius: bool

UNITS = {
    "fahrenheit": Units(python_weather.IMPERIAL, "\u00b0F", "mph", "in", "inHg", "mi", False),
    "celsius": Units(python_weather.METRIC, "\u00b0C", "km/h", "mm", "hPa", "km", True),
}
UNIT_CHOICES = [app_commands.Choice(name="Fahrenheit", value="fahrenheit"), app_commands.Choice(name="Celsius", value="celsius")]

@bot.tree.command(name="weather", description="Get weather information.")
@app_commands.describe(city="City name", units="Temperature units (default: Fahrenheit)")
@app_commands.choices(units=UNIT_CHOICES)
@rate_limit
async def weather_command(interaction: discord.Interaction, city: str, units: Optional[app_commands.Choice[str]] = None):
    city = city.strip()
    if not city:
        embed = create_embed("❌ Invalid Input", "Provide valid city name!", discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    if len(city) > Config.MAX_CITY_NAME_LENGTH:
        embed = create_embed("❌ Too Long", f"Max {Config.MAX_CITY_NAME_LENGTH} chars!", discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    await interaction.response.defer()
    selected = UNITS[units.value if units else "fahrenheit"]

    try:
        embed = create_embed("🔍 Fetching...", f"Getting weather for **{city}**...", discord.Color.yellow())
        await interaction.edit_original_response(embed=embed)

        async with python_weather.Client(unit=python_weather.IMPERIAL) as client:
            weather = await client.get(city, unit=selected.client)
            embed = create_weather_embed(weather, interaction.user, selected)
            await interaction.edit_original_response(embed=embed)

    except RequestError as e:
        logger.error(f"Weather API error: {e}")
        embed = create_embed("🌐 API Error", f"Error for '{city}'. Check city name.", discord.Color.red())
        await interaction.edit_original_response(embed=embed)
    except Error as e:
        logger.error(f"Weather error: {e}")
        embed = create_embed("❌ Weather Error", f"Couldn't get data for '{city}'.", discord.Color.red())
        await interaction.edit_original_response(embed=embed)
    except Exception as e:
        logger.error(f"Unexpected weather error: {e}")
        embed = create_embed("💥 Error", "Something went wrong. Try later.", discord.Color.red())
        await interaction.edit_original_response(embed=embed)

def _temperature_color(temp, units: Units = UNITS["fahrenheit"]) -> discord.Color:
    temp_f = temp * 9 / 5 + 32 if units.celsius else temp  # One set of thresholds, so both units agree
    return discord.Color.red() if temp_f >= 80 else discord.Color.orange() if temp_f >= 60 else discord.Color.blue() if temp_f >= 40 else discord.Color.dark_blue()

def _wind_text(weather, units: Units) -> str:
    wind_info = f"{weather.wind_speed} {units.speed}"
    if weather.wind_direction:
        direction = str(weather.wind_direction)
        if hasattr(weather.wind_direction, "emoji"):
            direction += f" {weather.wind_direction.emoji}"
        wind_info += f" {direction}"
    return wind_info

def _uv_text(ultraviolet) -> str:
    uv_text = str(ultraviolet)
    if hasattr(ultraviolet, "index"):
        uv_index = ultraviolet.index
        uv_text = f"{uv_index}/10" + (" ⚠️" if uv_index >= 8 else " 🟡" if uv_index >= 6 else "")
    return uv_text

def _midday(day):
    """The hourly entry nearest noon, or None. DailyForecast has no kind or description of its own."""
    return min(day.hourly_forecasts, key=lambda hour: abs(hour.time.hour - 12), default=None)

def _forecast_line(i: int, day, units: Units) -> str:
    day_name = "Today" if i == 0 else "Tomorrow" if i == 1 else day.date.strftime('%A')
    midday = _midday(day)
    emoji = getattr(getattr(midday, 'kind', None), 'emoji', '\U0001F324\ufe0f')
    desc = getattr(midday, 'description', "")
    temps = f"H: {day.highest_temperature}{units.temp}, L: {day.lowest_temperature}{units.temp}"  # No `or`: 0 is a real temperature
    return f"{emoji} **{day_name}**: {desc} \u2022 {temps}\n" if desc else f"{emoji} **{day_name}**: {temps}\n"

def create_weather_embed(weather, user: discord.User, units: Units = UNITS["fahrenheit"]) -> discord.Embed:
    weather_emoji = getattr(weather.kind, 'emoji', '🌤️')
    date_str = weather.datetime.strftime('%A, %B %d, %Y')

    title = f"{weather_emoji} Weather in {weather.location}"
    description = f"**{weather.description}** • **{weather.temperature}{units.temp}**\n{date_str}"

    embed = create_embed(title, description, _temperature_color(weather.temperature, units))

    if weather.region and weather.country:
        embed.add_field(name="📍 Location", value=f"{weather.region}, {weather.country}", inline=False)

    embed.add_field(name="🌡️ Temperature", value=f"{weather.temperature}{units.temp}", inline=True)
    embed.add_field(name="🤚 Feels Like", value=f"{weather.feels_like}{units.temp}", inline=True)
    embed.add_field(name="💧 Humidity", value=f"{weather.humidity}%", inline=True)

    embed.add_field(name="💨 Wind", value=_wind_text(weather, units), inline=True)

    embed.add_field(name="🌧️ Precipitation", value=f"{weather.precipitation} {units.rain}", inline=True)
    embed.add_field(name="🔽 Pressure", value=f"{weather.pressure} {units.pressure}", inline=True)

    if weather.visibility:
        embed.add_field(name="👁️ Visibility", value=f"{weather.visibility} {units.distance}", inline=True)

    if weather.ultraviolet:
        embed.add_field(name="☀️ UV Index", value=_uv_text(weather.ultraviolet), inline=True)

    if weather.daily_forecasts:
        forecast_text = "".join(_forecast_line(i, day, units) for i, day in enumerate(weather.daily_forecasts[:4]))
        if forecast_text:
            embed.add_field(name="📅 Forecast", value=forecast_text, inline=False)

    embed.set_footer(text=f"Requested by {user.display_name} • {weather.datetime.strftime('%I:%M %p')}", icon_url=user.display_avatar.url)
    return embed
