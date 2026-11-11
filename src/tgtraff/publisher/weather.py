"""Погода для утреннего автопоста (спека §8).

Open-Meteo: без ключа, без регистрации, свободная лицензия — на пилоте это
ровно то, что нужно. Транспорт внедряется: в тестах вместо сети подставляются
записанные ответы.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("tgtraff.weather")

API_URL = "https://api.open-meteo.com/v1/forecast"

# Коды погоды WMO → человеческий язык и эмодзи.
WEATHER_CODES: dict[int, tuple[str, str]] = {
    0: ("ясно", "☀️"),
    1: ("малооблачно", "🌤"),
    2: ("переменная облачность", "⛅️"),
    3: ("пасмурно", "☁️"),
    45: ("туман", "🌫"),
    48: ("изморозь", "🌫"),
    51: ("морось", "🌦"),
    53: ("морось", "🌦"),
    55: ("сильная морось", "🌦"),
    61: ("небольшой дождь", "🌦"),
    63: ("дождь", "🌧"),
    65: ("сильный дождь", "🌧"),
    66: ("ледяной дождь", "🌧"),
    67: ("ледяной дождь", "🌧"),
    71: ("небольшой снег", "🌨"),
    73: ("снег", "🌨"),
    75: ("сильный снег", "❄️"),
    77: ("снежная крупа", "🌨"),
    80: ("ливень", "🌧"),
    81: ("ливень", "🌧"),
    82: ("сильный ливень", "⛈"),
    85: ("снегопад", "🌨"),
    86: ("сильный снегопад", "❄️"),
    95: ("гроза", "⛈"),
    96: ("гроза с градом", "⛈"),
    99: ("гроза с градом", "⛈"),
}


@dataclass(frozen=True)
class Forecast:
    t_min: float
    t_max: float
    code: int
    wind: float = 0.0
    precipitation: float = 0.0


def describe_weather(code: int) -> tuple[str, str]:
    return WEATHER_CODES.get(int(code), ("без осадков", "🌤"))


async def _httpx_transport(params: dict) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(API_URL, params=params)
        r.raise_for_status()
        return r.json()


class WeatherClient:
    def __init__(self, transport=None):
        self._transport = transport or _httpx_transport

    async def forecast_for(self, lat: float, lon: float, tz: str) -> Forecast:
        data = await self._transport({
            "latitude": lat, "longitude": lon, "timezone": tz, "forecast_days": 1,
            "daily": ("temperature_2m_max,temperature_2m_min,weather_code,"
                      "wind_speed_10m_max,precipitation_sum"),
        })
        daily = data["daily"]
        return Forecast(
            t_min=float(daily["temperature_2m_min"][0]),
            t_max=float(daily["temperature_2m_max"][0]),
            code=int(daily["weather_code"][0]),
            wind=float(daily.get("wind_speed_10m_max", [0])[0] or 0),
            precipitation=float(daily.get("precipitation_sum", [0])[0] or 0),
        )


def temp(value: float) -> str:
    """«+17», «−3», «0» — как пишут в прогнозах, со знаком."""
    rounded = int(round(value))
    if rounded > 0:
        return f"+{rounded}"
    if rounded < 0:
        return f"−{abs(rounded)}"
    return "0"
