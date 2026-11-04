import discord
import python_weather
from discord import app_commands
from python_weather.errors import Error, RequestError

from .bot import bot
from .config import Config
from .log import logger
from .utils import create_embed, rate_limit

@bot.tree.command(name="weather", description="Get weather information.")
@app_commands.describe(city="City name")
@rate_limit
async def weather_command(interaction: discord.Interaction, city: str):
    city = city.strip()
    if not city:
        embed = create_embed("❌ Invalid Input", "Provide valid city name!", discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    if len(city) > Config.MAX_CITY_NAME_LENGTH:
        embed = create_embed("❌ Too Long", f"Max {Config.MAX_CITY_NAME_LENGTH} chars!", discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    await interaction.response.defer()

    try:
        embed = create_embed("🔍 Fetching...", f"Getting weather for **{city}**...", discord.Color.yellow())
        await interaction.edit_original_response(embed=embed)

        async with python_weather.Client(unit=python_weather.IMPERIAL) as client:
            weather = await client.get(city)
            embed = create_weather_embed(weather, interaction.user)
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

def _temperature_color(temp) -> discord.Color:
    return discord.Color.red() if temp >= 80 else discord.Color.orange() if temp >= 60 else discord.Color.blue() if temp >= 40 else discord.Color.dark_blue()

def _wind_text(weather) -> str:
    wind_info = f"{weather.wind_speed} mph"
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

def _forecast_line(i: int, day) -> str:
    day_name = "Today" if i == 0 else "Tomorrow" if i == 1 else day.date.strftime('%A') if hasattr(day, 'date') else f"Day {i+1}"
    emoji = getattr(getattr(day, 'kind', None), 'emoji', '🌤️')

    desc = day.description if hasattr(day, 'description') else str(day.kind) if hasattr(day, 'kind') else ""

    temp_high = getattr(day, 'highest', None) or getattr(day, 'high', None) or getattr(day, 'temperature', None)
    temp_low = getattr(day, 'lowest', None) or getattr(day, 'low', None)

    temp_info = f"H: {temp_high}°F, L: {temp_low}°F" if temp_high and temp_low else f"{temp_high}°F" if temp_high else ""

    line = f"{emoji} **{day_name}**: {desc}"
    if temp_info:
        line += f" • {temp_info}"
    return line + "\n"

def create_weather_embed(weather, user: discord.User) -> discord.Embed:
    weather_emoji = getattr(weather.kind, 'emoji', '🌤️')
    date_str = weather.datetime.strftime('%A, %B %d, %Y')

    title = f"{weather_emoji} Weather in {weather.location}"
    description = f"**{weather.description}** • **{weather.temperature}°F**\n{date_str}"

    embed = create_embed(title, description, _temperature_color(weather.temperature))

    if weather.region and weather.country:
        embed.add_field(name="📍 Location", value=f"{weather.region}, {weather.country}", inline=False)

    embed.add_field(name="🌡️ Temperature", value=f"{weather.temperature}°F", inline=True)
    embed.add_field(name="🤚 Feels Like", value=f"{weather.feels_like}°F", inline=True)
    embed.add_field(name="💧 Humidity", value=f"{weather.humidity}%", inline=True)

    embed.add_field(name="💨 Wind", value=_wind_text(weather), inline=True)

    embed.add_field(name="🌧️ Precipitation", value=f"{weather.precipitation} in", inline=True)
    embed.add_field(name="🔽 Pressure", value=f"{weather.pressure} inHg", inline=True)

    if weather.visibility:
        embed.add_field(name="👁️ Visibility", value=f"{weather.visibility} mi", inline=True)

    if weather.ultraviolet:
        embed.add_field(name="☀️ UV Index", value=_uv_text(weather.ultraviolet), inline=True)

    if weather.daily_forecasts:
        forecast_text = "".join(_forecast_line(i, day) for i, day in enumerate(weather.daily_forecasts[:4]))
        if forecast_text:
            embed.add_field(name="📅 Forecast", value=forecast_text, inline=False)

    embed.set_footer(text=f"Requested by {user.display_name} • {weather.datetime.strftime('%I:%M %p')}", icon_url=user.display_avatar.url)
    return embed
