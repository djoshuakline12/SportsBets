import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.weatherapi.com/v1"

# Venues for outdoor sports (NFL, MLB). Expand as needed.
VENUE_LOCATIONS = {
    # NFL stadiums (outdoor)
    "Arrowhead Stadium": "Kansas City, MO",
    "Lambeau Field": "Green Bay, WI",
    "Soldier Field": "Chicago, IL",
    "Highmark Stadium": "Orchard Park, NY",
    "Empower Field at Mile High": "Denver, CO",
    "TIAA Bank Field": "Jacksonville, FL",
    "Raymond James Stadium": "Tampa, FL",
    "Hard Rock Stadium": "Miami Gardens, FL",
    "Bank of America Stadium": "Charlotte, NC",
    "Nissan Stadium": "Nashville, TN",
    "Lumen Field": "Seattle, WA",
    "Levi's Stadium": "Santa Clara, CA",
    "Lincoln Financial Field": "Philadelphia, PA",
    "MetLife Stadium": "East Rutherford, NJ",
    "Paycor Stadium": "Cincinnati, OH",
    "Cleveland Browns Stadium": "Cleveland, OH",
    "Acrisure Stadium": "Pittsburgh, PA",
    "M&T Bank Stadium": "Baltimore, MD",
    "FedExField": "Landover, MD",
}

# Sports that are meaningfully affected by weather
OUTDOOR_SPORTS = {"americanfootball_nfl", "baseball_mlb"}


async def get_weather_for_location(location: str, date: str) -> dict | None:
    """Fetch weather forecast for a location and date.

    Args:
        location: City name or "City, State" string
        date: Date string in YYYY-MM-DD format
    """
    if not settings.weather_api_key:
        return None

    url = f"{BASE_URL}/forecast.json"
    params = {
        "key": settings.weather_api_key,
        "q": location,
        "dt": date,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        forecast_day = data.get("forecast", {}).get("forecastday", [{}])[0]
        day = forecast_day.get("day", {})

        return {
            "location": location,
            "date": date,
            "temp_f": day.get("avgtemp_f"),
            "wind_mph": day.get("maxwind_mph"),
            "precip_in": day.get("totalprecip_in"),
            "humidity": day.get("avghumidity"),
            "condition": day.get("condition", {}).get("text", ""),
            "snow_cm": day.get("totalsnow_cm", 0),
        }
    except Exception as e:
        logger.error(f"Weather fetch failed for {location}: {e}")
        return None


def calculate_weather_factor(weather: dict | None, sport: str) -> float:
    """Return a weather adjustment factor between -0.15 and +0.05.

    Negative = conditions hurt scoring/predictability.
    Positive = ideal conditions slightly favor better team.
    Zero = neutral or indoor sport.
    """
    if not weather or sport not in OUTDOOR_SPORTS:
        return 0.0

    factor = 0.0
    wind = weather.get("wind_mph", 0) or 0
    precip = weather.get("precip_in", 0) or 0
    temp = weather.get("temp_f", 70) or 70
    snow = weather.get("snow_cm", 0) or 0

    # High wind reduces passing/kicking accuracy in NFL
    if wind > 20:
        factor -= 0.05
    elif wind > 15:
        factor -= 0.02

    # Rain/snow increases unpredictability
    if precip > 0.5:
        factor -= 0.05
    elif precip > 0.1:
        factor -= 0.02

    if snow > 2:
        factor -= 0.05

    # Extreme cold
    if temp < 20:
        factor -= 0.03
    elif temp < 32:
        factor -= 0.01

    # Ideal conditions slightly favor better team
    if wind < 5 and precip == 0 and 55 < temp < 80:
        factor += 0.02

    return max(-0.15, min(0.05, factor))
