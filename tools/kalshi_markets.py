import json
from .kalshi_auth import kalshi_get


def tool_search_kalshi_markets(pk, api_key_id, base_url, keywords):
    """Search open Kalshi markets by keywords using the events API.

    Uses events endpoint with with_nested_markets=true to discover markets,
    since the flat /markets endpoint doesn't always return weather markets.
    Falls back to the flat /markets endpoint if events search finds nothing.
    """
    try:
        kws = [k.upper() for k in keywords]
        matched = []

        # Strategy 1: Use the events API (works reliably on both demo and prod)
        for kw in kws:
            r = kalshi_get(
                pk, api_key_id, base_url, "/trade-api/v2/events",
                params={
                    "status": "open",
                    "series_ticker": kw,
                    "with_nested_markets": "true",
                    "limit": "50",
                },
            )
            if r.status_code != 200:
                continue
            events = r.json().get("events", [])
            for event in events:
                for m in event.get("markets", []):
                    if m.get("status") in ("active", "open", None):
                        matched.append(
                            {
                                "ticker": m["ticker"],
                                "title": m.get("title", ""),
                                "event_ticker": event.get("event_ticker", ""),
                                "close_time": m.get("close_time", ""),
                                "yes_bid": m.get("yes_bid"),
                                "yes_ask": m.get("yes_ask"),
                                "last_price": m.get("last_price"),
                                "volume": m.get("volume"),
                                "status": m.get("status", ""),
                            }
                        )

        # Strategy 2: Fall back to flat markets search if events returned nothing
        if not matched:
            cursor = None
            total_scanned = 0
            for _ in range(50):
                params = {"limit": "100", "status": "open"}
                if cursor:
                    params["cursor"] = cursor
                r = kalshi_get(pk, api_key_id, base_url, "/trade-api/v2/markets", params)
                if r.status_code != 200:
                    break
                data = r.json()
                markets = data.get("markets", [])
                total_scanned += len(markets)
                for m in markets:
                    txt = (m.get("ticker", "") + " " + m.get("title", "")).upper()
                    if any(kw in txt for kw in kws):
                        matched.append(
                            {
                                "ticker": m["ticker"],
                                "title": m["title"],
                                "close_time": m.get("close_time", ""),
                                "yes_bid": m.get("yes_bid"),
                                "yes_ask": m.get("yes_ask"),
                                "last_price": m.get("last_price"),
                                "volume": m.get("volume"),
                                "status": m.get("status", ""),
                            }
                        )
                cursor = data.get("cursor")
                if not cursor or not markets:
                    break
                if matched and len(matched) >= 20:
                    break

        return json.dumps(
            {
                "keywords": keywords,
                "matched_count": len(matched),
                "matched": matched,
            }
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_get_orderbook(pk, api_key_id, base_url, ticker):
    """Fetch live orderbook for a Kalshi market ticker."""
    try:
        r = kalshi_get(
            pk, api_key_id, base_url, f"/trade-api/v2/markets/{ticker}/orderbook"
        )
        if r.status_code == 200:
            return json.dumps({"ticker": ticker, "orderbook": r.json()})
        return json.dumps({"error": f"HTTP {r.status_code}", "body": r.text[:500]})
    except Exception as e:
        return json.dumps({"error": str(e)})


# Tool definitions for the Claude API
MARKET_TOOL_DEFINITIONS = [
    {
        "name": "search_kalshi_markets",
        "description": (
            "Search open Kalshi weather markets by series ticker. Pass the series "
            "ticker as a keyword (e.g. 'KXHIGHCHI' for Chicago high temp, 'KXLOWTCHI' "
            "for Chicago low temp). Returns all events and their individual market "
            "contracts with current bid/ask prices. "
            "City codes: CHI, NYC, MIA, LAX, AUS, DEN, PHIL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Series tickers to search (e.g. ['KXHIGHCHI', 'KXLOWTCHI'])",
                }
            },
            "required": ["keywords"],
        },
    },
    {
        "name": "get_orderbook",
        "description": (
            "Fetch the live orderbook for a specific Kalshi market ticker "
            "to see current bid/ask prices and depth."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Market ticker"}
            },
            "required": ["ticker"],
        },
    },
]
