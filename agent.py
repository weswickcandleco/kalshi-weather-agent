#!/usr/bin/env python3
"""
Kalshi Weather Prediction Market Agent
=======================================
Fetches pre-bundled weather + market data from Cloudflare Worker,
sends it to Claude in a single call, and executes any bets.

Usage:
  python3 agent.py              # dry run (default -- no real orders)
  python3 agent.py --demo       # place orders on demo account
  python3 agent.py --live       # place orders with real money
  python3 agent.py --date 2026-02-15  # target a specific date
  python3 agent.py --cities CHI NYC MIA  # specific cities only
  python3 agent.py --history    # show trade history
"""

import os
import sys
import json
import time
import argparse
import datetime
import re

import requests
from dotenv import load_dotenv
import anthropic
from scipy.stats import norm

from config import (
    CST,
    KALSHI_PROD_BASE,
    KALSHI_DEMO_BASE,
    MAX_AGENT_TURNS,
    MAX_RUN_DOLLARS,
    CLAUDE_MODEL,
    CITY_CONFIGS,
    WORKER_URL,
)

from tools.kalshi_auth import load_private_key
from tools.kalshi_trading import (
    tool_get_account_balance,
    tool_place_order,
    TRADING_TOOL_DEFINITIONS,
)
from tools.trade_log import log_trade, log_run, print_history, get_trade_history, get_existing_tickers, get_city_bet_count
from tools.notify import notify_bets_placed, notify_error

# Tracks cumulative dollars committed in this run (reset each run)
_run_spend_cents = 0
# Valid Kalshi ticker: uppercase letters, digits, hyphens, dots
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-\.]{2,60}$")
# Max bets per city per day (hard limit Claude can't override)
MAX_BETS_PER_CITY = 2
# Tracks bets placed this run per city
_run_city_bets = {}

# Ticker parsing regex (same as settle.py)
_CONTRACT_RE = re.compile(r"KX(HIGH|LOWT)([A-Z]+)-\d+[A-Z]+\d+-([BT])([\d\.]+)")

# ---------------------------------------------------------------------------
# NWS Forecast Error Model
# ---------------------------------------------------------------------------
# Standard deviation of NWS Day-1 temperature forecast errors (Fahrenheit)
# by city and season. Derived from NSSL verification data, UW Baars/Mass study,
# and Washington Post/ForecastWatch analysis (2003-2024).

FORECAST_ERROR_SD = {
    #           Winter  Spring  Summer  Autumn
    "CHI":  {"winter": 7.0, "spring": 5.5, "summer": 4.0, "autumn": 5.5},
    "NYC":  {"winter": 6.0, "spring": 5.0, "summer": 3.5, "autumn": 5.0},
    "MIA":  {"winter": 4.0, "spring": 3.5, "summer": 3.0, "autumn": 3.5},
    "LAX":  {"winter": 4.5, "spring": 4.0, "summer": 3.5, "autumn": 5.0},
    "AUS":  {"winter": 6.0, "spring": 5.0, "summer": 4.0, "autumn": 5.0},
    "DEN":  {"winter": 8.0, "spring": 6.5, "summer": 4.5, "autumn": 6.0},
    "PHIL": {"winter": 6.0, "spring": 5.0, "summer": 3.5, "autumn": 5.0},
}
_DEFAULT_SD = {"winter": 6.0, "spring": 5.0, "summer": 3.5, "autumn": 5.0}


def _get_season(date_str):
    month = int(date_str[5:7])
    if month in (12, 1, 2):
        return "winter"
    elif month in (3, 4, 5):
        return "spring"
    elif month in (6, 7, 8):
        return "summer"
    else:
        return "autumn"


def _contract_prob(ticker, forecast_high, forecast_low, yes_sub_title=""):
    """Compute P(YES wins) for a contract given NWS forecast temps.

    Args:
        ticker: Kalshi contract ticker
        forecast_high: NWS predicted high temp (F)
        forecast_low: NWS predicted low temp (F)
        yes_sub_title: contract's yes_sub_title (e.g. "56° or above", "47° or below")
            Used to determine threshold direction for T contracts.

    Returns (probability, city_code) or (None, None) if ticker can't be parsed.
    """
    m = _CONTRACT_RE.search(ticker)
    if not m:
        return None, None
    temp_type = m.group(1)   # HIGH or LOWT
    city = m.group(2)        # CHI, NYC, etc.
    bet_type = m.group(3)    # B or T
    value = float(m.group(4))

    forecast = forecast_high if temp_type == "HIGH" else forecast_low
    if forecast is None:
        return None, city

    sd_table = FORECAST_ERROR_SD.get(city, _DEFAULT_SD)
    # Season is determined per-call from the target date (set in compute_contract_probabilities)
    sd = sd_table.get(_current_season, 3.0)

    if bet_type == "B":
        # Between: B54.5 means range [54, 55], wins if round(actual) in {54, 55}
        low_bound = int(value)
        high_bound = low_bound + 1
        prob = norm.cdf(high_bound + 0.5, forecast, sd) - norm.cdf(low_bound - 0.5, forecast, sd)
    else:
        # Threshold: direction determined by yes_sub_title
        # "X° or above" → P(YES) = P(temp > threshold) = 1 - CDF(threshold + 0.5)
        # "X° or below" → P(YES) = P(temp < threshold) = CDF(threshold - 0.5)
        threshold = int(value)
        if "below" in yes_sub_title.lower():
            # Lower tail: YES wins if temp < threshold
            prob = norm.cdf(threshold - 0.5, forecast, sd)
        else:
            # Upper tail (default): YES wins if temp > threshold
            prob = 1.0 - norm.cdf(threshold + 0.5, forecast, sd)

    return max(0.0, min(1.0, prob)), city


# Module-level season cache (set before computing probabilities)
_current_season = "winter"
# Ticker → yes_sub_title mapping (populated by compute_contract_probabilities)
_ticker_titles = {}
# City → (forecast_high, forecast_low) mapping
_city_forecasts = {}


def compute_contract_probabilities(bundle, target_date):
    """Attach model_prob, ev_yes, ev_no to each contract in the bundle."""
    global _current_season, _ticker_titles, _city_forecasts
    _current_season = _get_season(target_date)
    _ticker_titles = {}
    _city_forecasts = {}

    for code, city in bundle.get("cities", {}).items():
        w = city.get("weather", {})
        forecast_high = w.get("predicted_high_f")
        forecast_low = w.get("predicted_low_f")
        _city_forecasts[code] = (forecast_high, forecast_low)

        for mtype in ("high", "low"):
            mdata = city.get("markets", {}).get(mtype, {})
            for c in mdata.get("contracts", []):
                yes_sub = c.get("yes_sub_title", "")
                _ticker_titles[c["ticker"]] = yes_sub
                prob, _ = _contract_prob(c["ticker"], forecast_high, forecast_low, yes_sub)
                if prob is None:
                    continue
                c["model_prob"] = round(prob, 3)

                # Compute EV at crossing prices
                ob = c.get("orderbook") or {}
                yes_bids = ob.get("yes") or []
                no_bids = ob.get("no") or []

                # Cost to buy YES = cross the NO side (100 - best_no_bid)
                # Kalshi orderbook is sorted ascending by price; best bid = last entry
                if no_bids:
                    best_no_bid = no_bids[-1][0] if isinstance(no_bids[-1], list) else no_bids[-1]
                    cost_yes = 100 - best_no_bid
                    ev_yes = prob * (100 - cost_yes) - (1 - prob) * cost_yes
                    c["cost_yes"] = cost_yes
                    c["ev_yes"] = round(ev_yes, 1)
                else:
                    c["cost_yes"] = None
                    c["ev_yes"] = None

                # Cost to buy NO = cross the YES side (100 - best_yes_bid)
                if yes_bids:
                    best_yes_bid = yes_bids[-1][0] if isinstance(yes_bids[-1], list) else yes_bids[-1]
                    cost_no = 100 - best_yes_bid
                    ev_no = (1 - prob) * (100 - cost_no) - prob * cost_no
                    c["cost_no"] = cost_no
                    c["ev_no"] = round(ev_no, 1)
                else:
                    c["cost_no"] = None
                    c["ev_no"] = None

    sd_used = FORECAST_ERROR_SD.get(list(bundle.get("cities", {}).keys())[0] if bundle.get("cities") else "CHI", _DEFAULT_SD)
    print(f"[MODEL] Season: {_current_season}, example SD: {sd_used[_current_season]}F")


# ---------------------------------------------------------------------------
# Data fetching from Cloudflare Worker
# ---------------------------------------------------------------------------

def _fetch_worker(target_date, city_codes):
    """Single Worker call for a batch of cities."""
    params = {"date": target_date, "cities": ",".join(city_codes)}
    url = f"{WORKER_URL}/bundle"
    for attempt in range(2):
        try:
            r = requests.get(url, params=params, timeout=45)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 0:
                print(f"  Worker fetch failed ({e}), retrying...")
                time.sleep(2)
            else:
                raise


def fetch_bundle(target_date, cities):
    """Fetch data from Worker in batches of 3 (avoids Cloudflare 50-subrequest limit)."""
    BATCH_SIZE = 3
    merged = {"generated_at": None, "target_date": target_date, "cities": {}, "errors": []}
    for i in range(0, len(cities), BATCH_SIZE):
        batch = cities[i:i + BATCH_SIZE]
        print(f"  [FETCH] Batch {i // BATCH_SIZE + 1}: {', '.join(batch)}")
        result = _fetch_worker(target_date, batch)
        merged["generated_at"] = result.get("generated_at")
        merged["cities"].update(result.get("cities", {}))
        merged["errors"].extend(result.get("errors", []))
    return merged


def format_bundle_for_claude(bundle):
    """Convert the Worker JSON bundle into readable text for Claude's prompt."""
    lines = []
    for code, city in bundle.get("cities", {}).items():
        w = city.get("weather", {})
        lines.append(f"=== {city.get('city_name', code)} ({code}) ===")

        # Weather forecast
        high = w.get("predicted_high_f")
        low = w.get("predicted_low_f")
        current = w.get("current_temp_f")
        high_hour = w.get("high_hour", "")
        low_hour = w.get("low_hour", "")
        if w.get("error"):
            lines.append(f"FORECAST: ERROR - {w['error']}")
        else:
            lines.append(f"FORECAST: High {high}F ({high_hour or '?'}), Low {low}F ({low_hour or '?'})")
            if current is not None:
                lines.append(f"CURRENT OBS: {current}F (as of {w.get('observed_at', '?')})")

        # Hourly temps (condensed)
        hourly = w.get("hourly", [])
        if hourly:
            temps = [f"{h.get('time','')[11:16]}={h['temp_f']}F" for h in hourly[:24]]
            lines.append(f"HOURLY: {', '.join(temps)}")

        # Markets
        for mtype in ("high", "low"):
            mdata = city.get("markets", {}).get(mtype, {})
            contracts = mdata.get("contracts", [])
            series = mdata.get("series_ticker", "")
            label = "HIGH TEMP" if mtype == "high" else "LOW TEMP"
            if not contracts:
                lines.append(f"\n{label} MARKETS ({series}): No contracts found for this date")
                continue
            lines.append(f"\n{label} MARKETS ({series}):")
            for c in contracts:
                ob = c.get("orderbook") or {}
                yes_bids = ob.get("yes") or []
                no_bids = ob.get("no") or []
                ob_str = f"book: YES {yes_bids[:3]} NO {no_bids[:3]}" if ob else "no orderbook"

                # Model probability and EV (computed by compute_contract_probabilities)
                mp = c.get("model_prob")
                model_str = ""
                if mp is not None:
                    ev_y = c.get("ev_yes")
                    ev_n = c.get("ev_no")
                    ev_y_str = f"{ev_y:+.0f}c" if ev_y is not None else "N/A"
                    ev_n_str = f"{ev_n:+.0f}c" if ev_n is not None else "N/A"
                    model_str = f" | MODEL_PROB={mp:.2f} EV_YES={ev_y_str} EV_NO={ev_n_str}"

                lines.append(
                    f"  {c['ticker']} \"{c.get('yes_sub_title', c.get('title',''))}\" | "
                    f"yes_bid={c.get('yes_bid')} yes_ask={c.get('yes_ask')} "
                    f"last={c.get('last_price')} vol={c.get('volume')} | {ob_str}{model_str}"
                )

        lines.append("")  # blank line between cities

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def build_system_prompt(now, target_date, mode, dry_run):
    now_str = now.strftime("%I:%M %p CST on %A, %B %d, %Y")
    mode_note = (
        "place_order is simulated, no real money"
        if dry_run
        else "ORDERS WILL EXECUTE WITH REAL MONEY"
    )

    return f"""You are a Kalshi prediction market betting agent specializing in daily temperature markets.

TARGET DATE: {target_date}
CURRENT TIME: {now_str}
MODE: {mode} -- {mode_note}

You have been given complete weather forecast data and Kalshi market data for all target cities.
Analyze the data and place bets where you find edge. You have two tools: place_order and get_account_balance.

HOW KALSHI TEMPERATURE CONTRACTS WORK:
- Each contract is a yes/no question that costs 1-99 cents
- If YES wins, the contract pays $1.00. If NO wins, it pays $0
- Your PROFIT = $1.00 minus what you paid
- THE SWEET SPOT IS 20-70 CENTS. Contracts above 85c or below 15c are rejected.

SETTLEMENT RULES (from official Kalshi contract terms):
- Settlement source: NWS Daily Climate Report (OBSERVED max/min, not forecast)
- "Greater than" (>) is STRICT: ">38" requires 39+. If actual high is exactly 38, YES LOSES.
- "Less than" (<) is STRICT: "<38" requires 37 or lower.
- "Between" is INCLUSIVE on both ends: "38-39" means 38 OR 39 both make YES win.
- Full precision from NWS is used. No rounding.

WHY THIS MATTERS:
- If forecast says high=38F, a ">38" contract needs 39+ to win (maybe 40% chance)
- But a "38-39" range contract wins if high is 38 OR 39 (maybe 65% chance)
- Always check whether the threshold is strict > or inclusive >= before estimating probability

PROBABILITY MODEL:
Each contract includes MODEL_PROB and EV_YES/EV_NO, computed from a statistical model
of NWS forecast error (normal distribution with city/season-specific standard deviations
from published research). These are MORE ACCURATE than gut estimates.

DECISION RULE:
- USE MODEL_PROB for all probability assessments. Do NOT override it with your own estimates.
- EV_YES = expected value of buying YES at the crossing price (in cents)
- EV_NO = expected value of buying NO at the crossing price (in cents)
- ONLY bet when EV > +5 cents per contract (this is a 5%+ edge)
- Prefer bets with higher EV
- If no contracts show EV > +5, pass entirely -- "no bets today" is a valid and common outcome
- Contracts priced below 15c or above 85c are auto-rejected by the system
- Check BOTH EV_YES and EV_NO for each contract -- the edge is often on the NO side

ORDERBOOK EXECUTION:
TO BUY YES: side='yes', yes_price_cents = cost_yes shown in the data (crosses NO side)
TO BUY NO: side='no', yes_price_cents = (100 - cost_no) shown in the data (crosses YES side)
If the orderbook is EMPTY (EV shows N/A), SKIP that contract.

MAKER-TAKER AWARENESS:
For strong-edge bets (EV > +12c), crossing the spread is fine.
For moderate-edge bets (EV +5 to +12c), consider posting inside the spread.
For thin edges (EV < +5c), skip.

RULES:
- TRUST THE MODEL: use MODEL_PROB and EV values, not gut feeling
- Skip if no good opportunities exist -- many days will have 0-3 bets
- Check get_account_balance before placing bets
- Print a summary table at the end: | City | Contract | MODEL_PROB | Side | Cost | EV | Filled? |"""


# ---------------------------------------------------------------------------
# Tool dispatch (only trading tools now)
# ---------------------------------------------------------------------------

def dispatch_tool(name, inp, pk, api_key_id, base_url, dry_run, mode="", target_date=""):
    """Route a tool call to the correct function."""
    if name == "get_account_balance":
        return tool_get_account_balance(pk, api_key_id, base_url)
    elif name == "place_order":
        global _run_spend_cents
        ticker = inp["ticker"]
        if not _TICKER_RE.match(ticker):
            return json.dumps({"error": f"Invalid ticker format: {ticker!r}"})

        side = inp["side"]
        ypc = inp["yes_price_cents"]
        count = inp["contracts"]

        # --- Hard guardrail: Deduplication ---
        existing = get_existing_tickers(target_date)
        if ticker in {t for t, _ in existing}:
            return json.dumps({"error": f"DEDUP BLOCKED: Already have a position on {ticker} for {target_date}. Skipping duplicate."})

        # --- Hard guardrail: Contradictory bet blocking ---
        opposite = "no" if side == "yes" else "yes"
        if (ticker, opposite) in existing:
            return json.dumps({"error": f"CONFLICT BLOCKED: Already hold {opposite.upper()} on {ticker}. Cannot bet {side.upper()} on same contract."})

        # --- Hard guardrail: City limit ---
        city_code = ""
        for code in CITY_CONFIGS:
            if code in ticker:
                city_code = code
                break
        if city_code:
            city_counts = get_city_bet_count(target_date)
            run_city = _run_city_bets.get(city_code, 0)
            total_city = city_counts.get(city_code, 0) + run_city
            if total_city >= MAX_BETS_PER_CITY:
                return json.dumps({"error": f"CITY LIMIT: {city_code} already has {total_city} bet(s) for {target_date} (max {MAX_BETS_PER_CITY}). Skip this city."})

        # --- Hard guardrail: Negative-EV blocking ---
        est_prob = inp.get("est_probability")
        if est_prob is not None:
            cost_c = ypc if side == "yes" else 100 - ypc
            if side == "yes":
                ev = est_prob * (100 - cost_c) - (1 - est_prob) * cost_c
            else:
                ev = (1 - est_prob) * (100 - cost_c) - est_prob * cost_c
            if ev < 0:
                return json.dumps({"error": f"NEGATIVE EV BLOCKED: EV is {ev:.1f}c (negative). This bet loses money on average. Skipping."})

        # Per-run spending cap
        cost_this_order = (ypc if side == "yes" else 100 - ypc) * count
        if (_run_spend_cents + cost_this_order) > int(MAX_RUN_DOLLARS * 100):
            remaining = int(MAX_RUN_DOLLARS * 100) - _run_spend_cents
            return json.dumps({
                "error": f"RUN SPENDING CAP: This order costs {cost_this_order}c but only "
                         f"{remaining}c remains of the ${MAX_RUN_DOLLARS:.0f} per-run limit. "
                         f"Already committed ${_run_spend_cents/100:.2f} this run.",
                "suggestion": "Reduce contracts or skip this bet."
            })
        result = tool_place_order(pk, api_key_id, base_url, dry_run, ticker, side, ypc, count)
        # Track spend and log the trade
        try:
            parsed = json.loads(result)
            if "error" not in parsed:
                if "would_place" in parsed:
                    actual_cost = int(parsed["cost_dollars"] * 100)
                elif "response" in parsed:
                    actual_cost = int(parsed["response"].get("_cost_dollars", 0) * 100)
                else:
                    actual_cost = cost_this_order
                _run_spend_cents += actual_cost
                print(f"  [SPEND] ${actual_cost/100:.2f} this order | ${_run_spend_cents/100:.2f} / ${MAX_RUN_DOLLARS:.2f} run total")
                # Track per-city bet count for this run
                if city_code:
                    _run_city_bets[city_code] = _run_city_bets.get(city_code, 0) + 1
                city = city_code or ""
                if not city:
                    for code in CITY_CONFIGS:
                        if code in ticker:
                            city = code
                            break
                filled = False
                order_id = None
                if not dry_run and "response" in parsed:
                    resp = parsed["response"]
                    order_data = resp.get("order", resp)
                    filled = order_data.get("fill_count", 0) > 0
                    order_id = order_data.get("order_id", order_data.get("client_order_id"))
                est_prob = inp.get("est_probability")
                ev = None
                if est_prob is not None:
                    cost_c = ypc if side == "yes" else 100 - ypc
                    ev = est_prob * (100 - cost_c) - (1 - est_prob) * cost_c
                trade_title = _ticker_titles.get(ticker, "")
                fc = _city_forecasts.get(city, (None, None))
                log_trade(
                    mode=mode, target_date=target_date, city=city,
                    ticker=ticker, title=trade_title, side=side,
                    yes_price_cents=ypc, contracts=count,
                    forecast_high_f=fc[0], forecast_low_f=fc[1],
                    est_probability=est_prob, expected_value_cents=ev,
                    filled=filled, order_id=order_id, dry_run=dry_run,
                )
        except Exception:
            pass
        return result
    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


# ---------------------------------------------------------------------------
# Agent loop (typically 1-2 turns)
# ---------------------------------------------------------------------------

def run_agent(client, system_prompt, user_prompt, tools, pk, api_key_id, base_url, dry_run,
              mode="", target_date=""):
    """Run the tool-use loop (max 3 turns, typically 1-2)."""
    messages = [{"role": "user", "content": user_prompt}]
    token_log = []

    for turn in range(MAX_AGENT_TURNS):
        print(f"\n-- Turn {turn + 1} {'--' * 25}")

        for attempt in range(3):
            try:
                response = client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=4096,
                    system=system_prompt,
                    tools=tools,
                    messages=messages,
                )
                break
            except anthropic.RateLimitError:
                wait = 30 * (attempt + 1)
                print(f"\n  Rate limited. Waiting {wait}s...")
                time.sleep(wait)
        else:
            print("\n  Rate limit exceeded after retries. Stopping.")
            break

        blocks = response.content
        stop_reason = response.stop_reason
        token_log.append((response.usage.input_tokens, response.usage.output_tokens))
        messages.append({"role": "assistant", "content": blocks})

        # Print text blocks
        for block in blocks:
            if block.type == "text" and block.text.strip():
                print(f"\n{block.text}")

        if stop_reason == "end_turn":
            print("\n-- Agent finished --")
            break

        # Process tool calls
        if stop_reason == "tool_use":
            tool_results = []
            for block in blocks:
                if block.type != "tool_use":
                    continue
                name = block.name
                inp = block.input
                tool_id = block.id

                print(f"\n[TOOL] {name}({json.dumps(inp, separators=(',', ':'))})")
                result = dispatch_tool(name, inp, pk, api_key_id, base_url, dry_run,
                                       mode=mode, target_date=target_date)
                preview = result[:400] + ("..." if len(result) > 400 else "")
                print(f"  -> {preview}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})

    # Log token usage
    total_in = sum(u[0] for u in token_log)
    total_out = sum(u[1] for u in token_log)
    if response:
        usage = response.usage
        print(f"\n[TOKENS] Last turn: {usage.input_tokens} in / {usage.output_tokens} out")
    print(f"[TOKENS] Total: {total_in} in / {total_out} out")

    # Estimate API cost (Sonnet 4.5: $3/M in, $15/M out)
    cost_estimate = total_in / 1_000_000 * 3 + total_out / 1_000_000 * 15
    print(f"[COST] Estimated API spend: ${cost_estimate:.4f}")

    print(f"\n{'=' * 60}")
    return {"input_tokens": total_in, "output_tokens": total_out, "cost_estimate": cost_estimate}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Kalshi Weather Betting Agent")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--live", action="store_true", help="Place real bets on production")
    group.add_argument("--demo", action="store_true", help="Place bets on demo account")
    parser.add_argument("--date", type=str, default=None, help="Target date (YYYY-MM-DD)")
    parser.add_argument(
        "--cities", nargs="+", default=None,
        help="City codes to target (e.g. CHI NYC MIA). Default: all cities.",
    )
    parser.add_argument(
        "--history", action="store_true",
        help="Show trade history and P&L summary, then exit.",
    )
    args = parser.parse_args()

    if args.history:
        print_history()
        sys.exit(0)

    load_dotenv()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set. Add it to your .env file.")
        sys.exit(1)

    # Determine mode and load Kalshi credentials
    if args.live:
        mode, base_url, dry_run = "LIVE", KALSHI_PROD_BASE, False
        kalshi_key_id = os.environ.get("KALSHI_PROD_API_KEY_ID")
        kalshi_pk_path = os.environ.get("KALSHI_PROD_PRIVATE_KEY_PATH")
    elif args.demo:
        mode, base_url, dry_run = "DEMO", KALSHI_DEMO_BASE, False
        kalshi_key_id = os.environ.get("KALSHI_DEMO_API_KEY_ID")
        kalshi_pk_path = os.environ.get("KALSHI_DEMO_PRIVATE_KEY_PATH")
    else:
        mode, base_url, dry_run = "DRY RUN", KALSHI_PROD_BASE, True
        kalshi_key_id = os.environ.get("KALSHI_PROD_API_KEY_ID")
        kalshi_pk_path = os.environ.get("KALSHI_PROD_PRIVATE_KEY_PATH")

    if not kalshi_key_id or not kalshi_pk_path:
        print(f"Error: Kalshi credentials not set for {mode} mode. Check your .env file.")
        sys.exit(1)

    try:
        pk = load_private_key(kalshi_pk_path)
        print(f"Kalshi RSA key loaded ({mode} account).")
    except Exception as e:
        print(f"Error loading Kalshi private key: {e}")
        sys.exit(1)

    # Determine target date
    now = datetime.datetime.now(CST)
    if args.date:
        target_date = args.date
    else:
        tomorrow = (now + datetime.timedelta(days=1)).date()
        target_date = tomorrow.isoformat()

    # Determine cities
    cities = args.cities if args.cities else list(CITY_CONFIGS.keys())
    valid_cities = [c for c in cities if c in CITY_CONFIGS]
    if not valid_cities:
        print(f"Error: No valid city codes. Available: {list(CITY_CONFIGS.keys())}")
        sys.exit(1)

    # Print banner
    print(f"\n{'=' * 60}")
    print(f"  KALSHI WEATHER AGENT (v2 -- Worker + single call)")
    print(f"  Now:  {now.strftime('%I:%M %p CST, %A %b %d %Y')}")
    print(f"  Target: {target_date}")
    print(f"  Cities: {', '.join(valid_cities)}")
    print(f"  Mode: {mode}")
    print(f"{'=' * 60}")

    # Step 1: Fetch data from Cloudflare Worker
    print(f"\n[FETCH] Getting data from Worker for {target_date}...")
    try:
        bundle = fetch_bundle(target_date, valid_cities)
        print(f"[FETCH] Got data for {len(bundle.get('cities', {}))} cities")
        if bundle.get("errors"):
            for err in bundle["errors"]:
                print(f"[FETCH] Warning: {err['city']} - {err['error']}")
    except Exception as e:
        print(f"[FETCH] FAILED: {e}")
        notify_error("agent.py", f"Worker fetch failed: {e}")
        sys.exit(1)

    # Step 2: Compute model probabilities for each contract
    compute_contract_probabilities(bundle, target_date)

    # Step 3: Format data as readable text for Claude
    data_text = format_bundle_for_claude(bundle)
    print(f"[DATA] Formatted {len(data_text)} chars of market + weather data")

    # Step 4: Build prompt with all data included
    tools = TRADING_TOOL_DEFINITIONS
    system_prompt = build_system_prompt(now, target_date, mode, dry_run)
    user_prompt = (
        f"Here is all weather forecast and Kalshi market data for {target_date}.\n"
        f"Analyze every city, find value bets, and place orders.\n\n"
        f"{data_text}\n\n"
        f"Check your balance first, then place any bets where you find edge. "
        f"If no good opportunities exist, say so and explain why."
    )

    # Step 5: Run Claude (1-2 turns typically)
    client = anthropic.Anthropic(api_key=api_key)
    run_start = datetime.datetime.now(CST).isoformat()

    token_stats = None
    try:
        token_stats = run_agent(client, system_prompt, user_prompt, tools, pk, kalshi_key_id, base_url, dry_run,
                                mode=mode, target_date=target_date)
    except Exception as e:
        notify_error("agent.py", str(e))
        raise

    # Step 6: Send Discord notification for filled trades
    try:
        recent = get_trade_history(limit=50)
        run_trades = [t for t in recent
                      if t["timestamp"] >= run_start
                      and t["filled"]
                      and not t["dry_run"]]
        if run_trades:
            notify_bets_placed(run_trades, mode, target_date, token_stats=token_stats)
    except Exception as e:
        print(f"[NOTIFY] Failed to send Discord notification: {e}")


if __name__ == "__main__":
    main()
