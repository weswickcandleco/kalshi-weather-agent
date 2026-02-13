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
from tools.trade_log import log_trade, log_run, print_history, get_trade_history
from tools.notify import notify_bets_placed, notify_error

# Tracks cumulative dollars committed in this run (reset each run)
_run_spend_cents = 0
# Valid Kalshi ticker: uppercase letters, digits, hyphens, dots
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-\.]{2,60}$")


# ---------------------------------------------------------------------------
# Data fetching from Cloudflare Worker
# ---------------------------------------------------------------------------

def fetch_bundle(target_date, cities):
    """Fetch pre-bundled NWS + Kalshi data from the Cloudflare Worker."""
    params = {"date": target_date}
    if cities:
        params["cities"] = ",".join(cities)
    url = f"{WORKER_URL}/bundle"
    for attempt in range(2):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 0:
                print(f"  Worker fetch failed ({e}), retrying...")
                time.sleep(2)
            else:
                raise


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
                ob = c.get("orderbook", {})
                yes_bids = ob.get("yes", []) if ob else []
                no_bids = ob.get("no", []) if ob else []
                ob_str = f"book: YES {yes_bids[:3]} NO {no_bids[:3]}" if ob else "no orderbook"
                lines.append(
                    f"  {c['ticker']} \"{c.get('yes_sub_title', c.get('title',''))}\" | "
                    f"yes_bid={c.get('yes_bid')} yes_ask={c.get('yes_ask')} "
                    f"last={c.get('last_price')} vol={c.get('volume')} | {ob_str}"
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

FINDING VALUE:
The BEST bets are contracts where:
- The NWS forecast is NEAR the contract threshold (within 3-5 degrees)
- The contract price is in the 20-70 cent range
- You have an information edge (forecast says one thing, market prices another)

BAD bets to AVOID:
- Contracts far from the forecast (e.g. "high >45" when forecast is 38)
- Any contract where you'd pay 85c+ to win 15c or less

ORDERBOOK EXECUTION:
TO GET AN IMMEDIATE FILL, YOU MUST CROSS THE SPREAD:

TO BUY YES:
- Find the best NO bid price in the orderbook
- Your cost = 100 - no_bid_price
- Place: side='yes', yes_price_cents = (100 - no_bid_price)

TO BUY NO:
- Find the best YES bid price in the orderbook
- Your cost = 100 - yes_bid_price
- Place: side='no', yes_price_cents = yes_bid_price

If the orderbook is EMPTY (no bids on either side), SKIP that contract.

MAKER-TAKER AWARENESS:
Research on weather markets shows a ~2.5 percentage point gap between makers (who
post limit orders) and takers (who cross the spread). For strong-edge bets (15+
cents EV), crossing the spread is fine. For moderate-edge bets (8-12 cents), consider
posting inside the spread to save a few cents. For thin edges (<8 cents), skip.

EXPECTED VALUE CHECK (MANDATORY before every bet):
a) Your estimated probability of winning (p)
b) Your cost per contract (c)
c) Expected value = p * (100 - c) - (1 - p) * c
d) ONLY bet if expected value > +10 cents per contract

RULES:
- FOCUS ON VALUE: only bet contracts priced 20-70c with clear edge
- Always calculate EV BEFORE placing a bet
- Skip if no good opportunities exist -- passing is better than a bad bet
- Remember settlement uses STRICT > and < operators
- NWS Daily Climate Report is the official settlement source
- Check get_account_balance before placing bets
- Print a summary table at the end: | City | Contract | Your Prob | Cost | Profit if Win | EV | Filled? |"""


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
        # Per-run spending cap
        side = inp["side"]
        ypc = inp["yes_price_cents"]
        count = inp["contracts"]
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
                city = ""
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
                log_trade(
                    mode=mode, target_date=target_date, city=city,
                    ticker=ticker, title="", side=side,
                    yes_price_cents=ypc, contracts=count,
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
    if response:
        usage = response.usage
        print(f"\n[TOKENS] Last turn: {usage.input_tokens} in / {usage.output_tokens} out")

    print(f"\n{'=' * 60}")


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

    # Step 2: Format data as readable text for Claude
    data_text = format_bundle_for_claude(bundle)
    print(f"[DATA] Formatted {len(data_text)} chars of market + weather data")

    # Step 3: Build prompt with all data included
    tools = TRADING_TOOL_DEFINITIONS
    system_prompt = build_system_prompt(now, target_date, mode, dry_run)
    user_prompt = (
        f"Here is all weather forecast and Kalshi market data for {target_date}.\n"
        f"Analyze every city, find value bets, and place orders.\n\n"
        f"{data_text}\n\n"
        f"Check your balance first, then place any bets where you find edge. "
        f"If no good opportunities exist, say so and explain why."
    )

    # Step 4: Run Claude (1-2 turns typically)
    client = anthropic.Anthropic(api_key=api_key)
    run_start = datetime.datetime.now(CST).isoformat()

    try:
        run_agent(client, system_prompt, user_prompt, tools, pk, kalshi_key_id, base_url, dry_run,
                  mode=mode, target_date=target_date)
    except Exception as e:
        notify_error("agent.py", str(e))
        raise

    # Step 5: Send Discord notification for filled trades
    try:
        recent = get_trade_history(limit=50)
        run_trades = [t for t in recent
                      if t["timestamp"] >= run_start
                      and t["filled"]
                      and not t["dry_run"]]
        if run_trades:
            notify_bets_placed(run_trades, mode, target_date)
    except Exception as e:
        print(f"[NOTIFY] Failed to send Discord notification: {e}")


if __name__ == "__main__":
    main()
