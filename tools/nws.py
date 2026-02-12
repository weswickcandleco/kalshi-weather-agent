import json
import datetime
import requests
from zoneinfo import ZoneInfo
from config import NWS_HEADERS, CST, CITY_CONFIGS

# Cache resolved NWS gridpoint forecast URLs
_gridpoint_cache = {}


def _resolve_forecast_url(city_code):
    """Resolve a city code to its NWS hourly forecast URL via the points API."""
    if city_code in _gridpoint_cache:
        return _gridpoint_cache[city_code]

    cfg = CITY_CONFIGS.get(city_code)
    if not cfg:
        return None

    try:
        r = requests.get(
            f"https://api.weather.gov/points/{cfg['lat']},{cfg['lon']}",
            headers=NWS_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        url = r.json()["properties"]["forecastHourly"]
        _gridpoint_cache[city_code] = url
        return url
    except Exception:
        return None


def tool_get_nws_forecast(target_date_str, city="CHI"):
    """Fetch NWS hourly forecast for a given city and date. Returns JSON."""
    try:
        cfg = CITY_CONFIGS.get(city)
        if not cfg:
            return json.dumps({"error": f"Unknown city code: {city}. Valid: {list(CITY_CONFIGS.keys())}"})

        local_tz = ZoneInfo(cfg["tz"])

        # Resolve forecast URL
        forecast_url = _resolve_forecast_url(city)
        if not forecast_url:
            return json.dumps({"error": f"Could not resolve NWS gridpoint for {city}"})

        r = requests.get(forecast_url, headers=NWS_HEADERS, timeout=15)
        r.raise_for_status()
        periods = r.json()["properties"]["periods"]
        target = datetime.date.fromisoformat(target_date_str)
        hourly = []
        for p in periods:
            dt = datetime.datetime.fromisoformat(
                p["startTime"].replace("Z", "+00:00")
            ).astimezone(local_tz)
            if dt.date() == target:
                hourly.append(
                    {
                        "hour": dt.strftime("%I:%M %p %Z"),
                        "iso_time": dt.isoformat(),
                        "temp_f": p["temperature"],
                        "wind": p.get("windSpeed", ""),
                        "short_desc": p.get("shortForecast", ""),
                    }
                )

        temps = [h["temp_f"] for h in hourly]
        summary = {}
        if temps:
            summary = {
                "predicted_high_f": max(temps),
                "predicted_low_f": min(temps),
                "high_hour": next(h["hour"] for h in hourly if h["temp_f"] == max(temps)),
                "low_hour": next(h["hour"] for h in hourly if h["temp_f"] == min(temps)),
            }

        return json.dumps({
            "source": "NWS_API",
            "city": city,
            "city_name": cfg["name"],
            "station": cfg["station"],
            "date": target_date_str,
            "summary": summary,
            "hourly": hourly,
        })
    except Exception as e:
        return json.dumps({"error": str(e), "city": city})


def tool_get_current_conditions(city="CHI"):
    """Get live observed temp at a city's NWS station right now."""
    try:
        cfg = CITY_CONFIGS.get(city)
        if not cfg:
            return json.dumps({"error": f"Unknown city code: {city}. Valid: {list(CITY_CONFIGS.keys())}"})

        local_tz = ZoneInfo(cfg["tz"])
        station_url = f"https://api.weather.gov/stations/{cfg['station']}/observations/latest"

        r = requests.get(station_url, headers=NWS_HEADERS, timeout=10)
        r.raise_for_status()
        props = r.json()["properties"]
        temp_c = props.get("temperature", {}).get("value")
        temp_f = round(temp_c * 9 / 5 + 32, 1) if temp_c is not None else None
        now = datetime.datetime.now(local_tz)
        return json.dumps(
            {
                "city": city,
                "city_name": cfg["name"],
                "station": cfg["station"],
                "current_time": now.strftime("%I:%M %p %Z on %A %B %d"),
                "current_temp_f": temp_f,
                "wind_mph": props.get("windSpeed", {}).get("value"),
                "description": props.get("textDescription", ""),
                "observed_at": props.get("timestamp", ""),
            }
        )
    except Exception as e:
        return json.dumps(
            {
                "error": str(e),
                "city": city,
            }
        )


# Tool definitions for the Claude API
NWS_TOOL_DEFINITIONS = [
    {
        "name": "get_nws_forecast",
        "description": (
            "Fetch the official NWS hourly temperature forecast for a city on a given date. "
            "Returns hour-by-hour temps in Fahrenheit plus predicted high/low summary. "
            "NWS data IS the official settlement source for Kalshi weather markets. "
            "Available cities: CHI, NYC, MIA, LAX, AUS, DEN, PHIL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_date": {
                    "type": "string",
                    "description": "ISO date, e.g. '2026-02-12'",
                },
                "city": {
                    "type": "string",
                    "description": "City code: CHI, NYC, MIA, LAX, AUS, DEN, or PHIL. Defaults to CHI.",
                    "default": "CHI",
                },
            },
            "required": ["target_date"],
        },
    },
    {
        "name": "get_current_conditions",
        "description": (
            "Get the current observed temperature at a city's NWS station right now. "
            "Use this to check if today's high or low has already occurred. "
            "Available cities: CHI, NYC, MIA, LAX, AUS, DEN, PHIL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City code: CHI, NYC, MIA, LAX, AUS, DEN, or PHIL. Defaults to CHI.",
                    "default": "CHI",
                },
            },
        },
    },
]
