#!/usr/bin/env python3
"""
Kalshi Weather Agent -- Settlement Checker
==========================================
Checks if yesterday's bets won or lost by fetching observed temps from NWS.
Run manually or via LaunchAgent each morning at 9 AM CT.

Usage:
  python3 settle.py              # settle yesterday's trades
  python3 settle.py --date 2026-02-12  # settle a specific date
"""

import re
import sys
import time
import argparse
import datetime
import requests
import sqlite3

from dotenv import load_dotenv
load_dotenv()

from zoneinfo import ZoneInfo
from config import CITY_CONFIGS, NWS_HEADERS
from tools.trade_log import _get_db, update_settlement
from tools.notify import notify_settlements, notify_error

CST = ZoneInfo("America/Chicago")

# Ticker format: KX(HIGH|LOWT)(CITY)-YYMMM DD-([BT])(VALUE)
# Examples:
#   KXHIGHCHI-26FEB12-B38.5  -> Chicago high, between 38-39F
#   KXHIGHCHI-26FEB12-T38    -> Chicago high, >38F
#   KXLOWTNYC-26FEB12-B27.5  -> NYC low, between 27-28F
#   KXLOWTNYC-26FEB12-T27    -> NYC low, >27F (for HIGH) or <27F (for LOW)
TICKER_RE = re.compile(r"KX(HIGH|LOWT)([A-Z]+)-\d+[A-Z]+\d+-([BT])([\d\.]+)")


def get_pending_trades(target_date):
    """Get all pending filled trades for a specific date."""
    db = _get_db()
    rows = db.execute(
        """SELECT * FROM trades
           WHERE target_date = ? AND settlement_result = 'pending'
           AND filled = 1 AND dry_run = 0""",
        (target_date,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def fetch_observed_temps(city_code, target_date):
    """Fetch observed high and low temps from NWS for a city on a given date.

    Uses the NWS observations API to get all readings for the day,
    then extracts the max and min temperatures.

    Returns: (observed_high_f, observed_low_f) or raises on failure.
    """
    cfg = CITY_CONFIGS.get(city_code)
    if not cfg:
        raise ValueError(f"Unknown city code: {city_code}")

    station = cfg["station"]
    tz = ZoneInfo(cfg["tz"])

    date = datetime.date.fromisoformat(target_date)
    start_dt = datetime.datetime.combine(date, datetime.time(0, 0), tzinfo=tz)
    end_dt = datetime.datetime.combine(date, datetime.time(23, 59, 59), tzinfo=tz)

    url = f"https://api.weather.gov/stations/{station}/observations"
    params = {
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
    }

    # Retry up to 3 times
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=NWS_HEADERS, timeout=20)
            r.raise_for_status()
            break
        except Exception as e:
            if attempt == 2:
                raise ValueError(f"NWS API failed after 3 attempts for {city_code}: {e}")
            time.sleep(5 * (attempt + 1))

    features = r.json().get("features", [])
    temps_f = []

    for obs in features:
        temp_c = obs.get("properties", {}).get("temperature", {}).get("value")
        if temp_c is not None:
            temp_f = temp_c * 9 / 5 + 32
            temps_f.append(temp_f)

    if not temps_f:
        raise ValueError(f"No temperature observations for {city_code} on {target_date}")

    return (max(temps_f), min(temps_f))


def parse_ticker(ticker):
    """Parse a Kalshi weather ticker into its components.

    Returns dict with:
        temp_type: 'HIGH' or 'LOWT'
        city: city code (e.g. 'CHI')
        bet_type: 'B' (between/range) or 'T' (threshold)
        value: float (e.g. 38.5 or 38)
    """
    m = TICKER_RE.search(ticker)
    if not m:
        return None
    return {
        "temp_type": m.group(1),
        "city": m.group(2),
        "bet_type": m.group(3),
        "value": float(m.group(4)),
    }


def evaluate_bet(parsed_ticker, side, observed_high, observed_low, title=""):
    """Determine if a bet won or lost based on observed temps.

    Args:
        parsed_ticker: dict from parse_ticker()
        side: 'yes' or 'no'
        observed_high: observed high temp (F)
        observed_low: observed low temp (F)
        title: yes_sub_title from Kalshi (e.g. "56° or above", "47° or below")
            Used to determine threshold direction for T contracts.

    Returns: 'win' or 'loss'
    """
    temp_type = parsed_ticker["temp_type"]
    bet_type = parsed_ticker["bet_type"]
    value = parsed_ticker["value"]

    # Which observed temp matters?
    if temp_type == "HIGH":
        observed = observed_high
    else:  # LOWT
        observed = observed_low

    # Round observed to nearest integer for settlement comparison
    # NWS reports full precision, Kalshi uses integer thresholds
    observed_int = round(observed)

    # Determine if the condition is TRUE
    if bet_type == "B":
        # Between (inclusive): B38.5 means 38-39 range
        low_bound = int(value)
        high_bound = low_bound + 1
        condition_true = low_bound <= observed_int <= high_bound
    else:  # T = Threshold
        # Direction determined by title (yes_sub_title from Kalshi)
        # "X° or below" → YES wins if observed < threshold
        # "X° or above" → YES wins if observed > threshold (default)
        if "below" in title.lower():
            condition_true = observed_int < int(value)
        else:
            condition_true = observed_int > int(value)

    # Apply side
    if side == "yes":
        return "win" if condition_true else "loss"
    else:  # side == "no"
        return "loss" if condition_true else "win"


def settle_date(target_date):
    """Settle all pending trades for a given date.

    Returns list of result dicts for notification.
    """
    trades = get_pending_trades(target_date)
    if not trades:
        print(f"No pending trades for {target_date}.")
        return []

    print(f"Found {len(trades)} pending trade(s) for {target_date}.")

    # Group trades by city to minimize API calls
    cities_needed = set()
    for t in trades:
        parsed = parse_ticker(t["ticker"])
        if parsed:
            cities_needed.add(parsed["city"])

    # Fetch observed temps for each city
    observed = {}
    for city in cities_needed:
        try:
            high, low = fetch_observed_temps(city, target_date)
            observed[city] = (high, low)
            print(f"  {city}: observed high={high:.1f}F, low={low:.1f}F")
        except Exception as e:
            print(f"  {city}: ERROR fetching observations: {e}")
            notify_error("settle.py", f"Failed to fetch observed temps for {city} on {target_date}: {e}")

    # Evaluate each trade
    results = []
    for t in trades:
        parsed = parse_ticker(t["ticker"])
        if not parsed:
            print(f"  SKIP: Could not parse ticker {t['ticker']}")
            notify_error("settle.py", f"Could not parse ticker: {t['ticker']}")
            continue

        city = parsed["city"]
        if city not in observed:
            print(f"  SKIP: No observations for {city}")
            continue

        obs_high, obs_low = observed[city]
        obs_temp = obs_high if parsed["temp_type"] == "HIGH" else obs_low

        result = evaluate_bet(parsed, t["side"], obs_high, obs_low, title=t.get("title", ""))
        payout = 100 * t["contracts"] if result == "win" else 0
        net_cents = payout - t["cost_cents"]

        # Update database
        update_settlement(t["ticker"], target_date, result, payout)

        print(f"  {result.upper()}: {t['ticker']} | observed={obs_temp:.1f}F | "
              f"payout=${payout/100:.2f} | net=${net_cents/100:.2f}")

        results.append({
            "ticker": t["ticker"],
            "city": city,
            "side": t["side"],
            "cost_cents": t["cost_cents"],
            "contracts": t["contracts"],
            "observed_temp": round(obs_temp),
            "result": result,
            "payout_cents": payout,
            "net_cents": net_cents,
        })

    return results


def main():
    parser = argparse.ArgumentParser(description="Kalshi Weather Agent -- Settlement Checker")
    parser.add_argument("--date", type=str, default=None,
                        help="Date to settle (ISO format). Default: yesterday.")
    args = parser.parse_args()

    now = datetime.datetime.now(CST)
    if args.date:
        target_date = args.date
    else:
        yesterday = (now - datetime.timedelta(days=1)).date()
        target_date = yesterday.isoformat()

    # Safety: don't settle today or future dates (observations aren't complete)
    today = now.date()
    settle_date_obj = datetime.date.fromisoformat(target_date)
    if settle_date_obj >= today:
        print(f"ERROR: Cannot settle {target_date} -- it's today or in the future.")
        print(f"Observations aren't complete until the day is over.")
        print(f"The scheduled LaunchAgent runs at 9 AM and settles yesterday automatically.")
        sys.exit(1)

    print(f"Settlement check for {target_date}")
    print(f"Time: {now.strftime('%I:%M %p CT')}")
    print(f"{'-' * 40}")

    try:
        results = settle_date(target_date)
        if results:
            notify_settlements(results, target_date)
            wins = sum(1 for r in results if r["result"] == "win")
            losses = sum(1 for r in results if r["result"] == "loss")
            net = sum(r["net_cents"] for r in results) / 100
            sign = "+" if net >= 0 else ""
            print(f"\nDone: {wins}W-{losses}L | Net: {sign}${net:.2f}")
        else:
            print("Nothing to settle.")
    except Exception as e:
        print(f"ERROR: {e}")
        notify_error("settle.py", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
