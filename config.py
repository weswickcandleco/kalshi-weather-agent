from zoneinfo import ZoneInfo

# Timezone
CST = ZoneInfo("America/Chicago")
EST = ZoneInfo("America/New_York")

# NWS API (legacy single-city -- kept for backward compat)
NWS_HOURLY_URL = "https://api.weather.gov/gridpoints/LOT/74,71/forecast/hourly"
NWS_STATION_URL = "https://api.weather.gov/stations/KMDW/observations/latest"
NWS_HEADERS = {"User-Agent": "KalshiWeatherAgent/1.0"}

# Multi-city configurations
# Each entry: NWS station, lat/lon for gridpoint resolution, Kalshi ticker prefixes
CITY_CONFIGS = {
    "CHI": {
        "name": "Chicago Midway",
        "station": "KMDW",
        "lat": 41.786,
        "lon": -87.752,
        "high_ticker": "KXHIGHCHI",
        "low_ticker": "KXLOWTCHI",
        "tz": "America/Chicago",
    },
    "NYC": {
        "name": "New York (Central Park)",
        "station": "KNYC",
        "lat": 40.7789,
        "lon": -73.9692,
        "high_ticker": "KXHIGHNYC",
        "low_ticker": "KXLOWTNYC",
        "tz": "America/New_York",
    },
    "MIA": {
        "name": "Miami",
        "station": "KMIA",
        "lat": 25.7959,
        "lon": -80.287,
        "high_ticker": "KXHIGHMIA",
        "low_ticker": "KXLOWTMIA",
        "tz": "America/New_York",
    },
    "LAX": {
        "name": "Los Angeles",
        "station": "KLAX",
        "lat": 33.9425,
        "lon": -118.4081,
        "high_ticker": "KXHIGHLAX",
        "low_ticker": "KXLOWTLAX",
        "tz": "America/Los_Angeles",
    },
    "AUS": {
        "name": "Austin",
        "station": "KAUS",
        "lat": 30.1945,
        "lon": -97.6699,
        "high_ticker": "KXHIGHAUS",
        "low_ticker": "KXLOWTAUS",
        "tz": "America/Chicago",
    },
    "DEN": {
        "name": "Denver",
        "station": "KDEN",
        "lat": 39.8561,
        "lon": -104.6737,
        "high_ticker": "KXHIGHDEN",
        "low_ticker": "KXLOWTDEN",
        "tz": "America/Denver",
    },
    "PHIL": {
        "name": "Philadelphia",
        "station": "KPHL",
        "lat": 39.8721,
        "lon": -75.2411,
        "high_ticker": "KXHIGHPHI",
        "low_ticker": "KXLOWTPHI",
        "tz": "America/New_York",
    },
}

# Kalshi API base URLs
KALSHI_PROD_BASE = "https://api.elections.kalshi.com"
KALSHI_DEMO_BASE = "https://demo-api.kalshi.co"

# Risk management (enforced in place_order tool, not overridable by the agent)
MAX_BET_DOLLARS = 5.00
MAX_CONTRACTS_PER_ORDER = 5
MIN_EDGE_CENTS = 5
MAX_RUN_DOLLARS = 8.00  # max total spend per agent run (keeps $2 reserve on a $10 account)

# Agent limits
MAX_AGENT_TURNS = 30

# Claude model for the agentic loop
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
