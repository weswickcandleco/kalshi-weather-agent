#!/usr/bin/env python3
"""
Kalshi Weather Prediction Market Agent
=======================================
Autonomous agent that uses Claude to research NWS weather forecasts,
find Kalshi temperature markets across multiple cities, analyze edge,
and place bets.

Usage:
  python3 agent.py              # dry run (default -- no real orders)
  python3 agent.py --demo       # place orders on demo account
  python3 agent.py --live       # place orders with real money
  python3 agent.py --date 2026-02-15  # target a specific date
  python3 agent.py --cities CHI NYC MIA  # specific cities only
"""

import os
import sys
import json
import time
import argparse
import datetime

from dotenv import load_dotenv
import anthropic

import re

from config import (
    CST,
    KALSHI_PROD_BASE,
    KALSHI_DEMO_BASE,
    MAX_AGENT_TURNS,
    MAX_RUN_DOLLARS,
    CLAUDE_MODEL,
    CITY_CONFIGS,
)

# Tracks cumulative dollars committed in this run (reset each run)
_run_spend_cents = 0
# Valid Kalshi ticker: uppercase letters, digits, hyphens, dots (e.g. KXHIGHCHI-26FEB13-B38.5)
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-\.]{2,60}$")
from tools.kalshi_auth import load_private_key
from tools.nws import (
    tool_get_nws_forecast,
    tool_get_current_conditions,
    NWS_TOOL_DEFINITIONS,
)
from tools.kalshi_markets import (
    tool_search_kalshi_markets,
    tool_get_orderbook,
    MARKET_TOOL_DEFINITIONS,
)
from tools.kalshi_trading import (
    tool_get_account_balance,
    tool_place_order,
    TRADING_TOOL_DEFINITIONS,
)
from tools.trade_log import log_trade, log_run, print_history, get_trade_history
from tools.notify import notify_bets_placed, notify_error


def build_system_prompt(now, target_date, mode, dry_run, cities):
    now_str = now.strftime("%I:%M %p CST on %A, %B %d, %Y")
    mode_note = (
        "place_order is simulated, no real money"
        if dry_run
        else "ORDERS WILL EXECUTE WITH REAL MONEY"
    )
    city_list = ", ".join(cities)
    city_details = "\n".join(
        f"  - {code}: {CITY_CONFIGS[code]['name']} ({CITY_CONFIGS[code]['station']}) "
        f"-- tickers: {CITY_CONFIGS[code]['high_ticker']}, {CITY_CONFIGS[code]['low_ticker']}"
        for code in cities if code in CITY_CONFIGS
    )

    return f"""You are an autonomous Kalshi prediction market betting agent specializing in
daily temperature markets across multiple US cities.

TARGET DATE: {target_date}
CURRENT TIME: {now_str}
MODE: {mode} -- {mode_note}

TARGET CITIES:
{city_details}

HOW KALSHI TEMPERATURE CONTRACTS WORK:
- Each contract is a yes/no question that costs 1-99 cents
- If YES wins, the contract pays $1.00. If NO wins, it pays $0
- Your PROFIT = $1.00 minus what you paid
- THE SWEET SPOT IS 20-70 CENTS. Contracts above 85c or below 15c are rejected.

SETTLEMENT RULES (from official Kalshi contract terms -- KNOW THESE):
- Settlement source: NWS Daily Climate Report (OBSERVED max/min, not forecast)
- "Greater than" (>) is STRICT: ">38" requires 39+. If actual high is exactly 38, YES LOSES.
- "Less than" (<) is STRICT: "<38" requires 37 or lower. If actual high is exactly 38, YES LOSES.
- "Between" is INCLUSIVE on both ends: "38-39" means 38 OR 39 both make YES win.
- Full precision from NWS is used. No rounding.
- Last trading: 11:59 PM ET on the target date.
- Settlement: next morning after NWS publishes the Daily Climate Report.

WHY THIS MATTERS FOR YOUR BETS:
- If forecast says high=38F, a ">38" contract needs 39+ to win (maybe 40% chance)
- But a "38-39" range contract wins if high is 38 OR 39 (maybe 65% chance)
- Always check whether the threshold is strict > or inclusive >= before estimating probability

YOUR WORKFLOW:

1. WEATHER RESEARCH
   For each target city, call get_nws_forecast(target_date='{target_date}', city='CODE').
   Also call get_current_conditions to see what temp has been observed so far today.

2. MARKET SEARCH
   For each target city, search by its specific tickers:
   - search_kalshi_markets(["KXHIGHCHI"]) for Chicago high temp
   - search_kalshi_markets(["KXLOWTCHI"]) for Chicago low temp
   (Replace CHI with the city code: NYC, MIA, LAX, AUS, DEN, PHIL)
   Filter results to your target date.

3. FIND VALUE CONTRACTS
   The BEST bets are contracts where:
   - The NWS forecast is NEAR the contract threshold (within 3-5 degrees)
   - The contract price is in the 20-70 cent range
   - You have an information edge (forecast says one thing, market prices another)

   BAD bets to AVOID:
   - Contracts far from the forecast (e.g. "high >45" when forecast is 38)
   - Any contract where you'd pay 85c+ to win 15c or less

   GOOD bets to LOOK FOR:
   - Range contracts (B38.5 = "will high be 38-39?") where the forecast lands
     IN the range. These are often priced 30-60c with good edge.
   - Threshold contracts near the forecast where the market is mispriced.
     Remember: ">38" is STRICT, so if forecast is 38F, ">38" only wins if it
     goes ABOVE 38. Factor this into your probability estimate.

4. ORDERBOOK ANALYSIS
   For each promising contract, call get_orderbook.
   The orderbook returns:
   - "yes": [[price, qty], ...] = people wanting to BUY YES (YES bids)
   - "no": [[price, qty], ...] = people wanting to BUY NO (NO bids)
   The ASK on one side = 100 minus the BID on the other side.

5. EXPECTED VALUE CHECK (MANDATORY before every bet)
   For EACH potential bet, calculate:
   a) Your estimated probability of winning (p)
   b) Your cost per contract (c)
   c) Your profit if you win = (100 - c) cents
   d) Expected value = p * (100 - c) - (1 - p) * c
   e) ONLY bet if expected value > +10 cents per contract
   f) SKIP if cost > 85c or cost < 15c (these are automatically rejected anyway)

   Example of a GOOD bet:
   - "Will high be 36-37?" NWS says 36F. Probability ~65%. Price: 40c.
   - EV = 0.65 * 60 - 0.35 * 40 = 39 - 14 = +25c per contract. BET!

   Example of a BAD bet:
   - "Will high be >45?" NWS says 38F. Probability ~3%. NO costs 99c.
   - EV = 0.97 * 1 - 0.03 * 99 = 0.97 - 2.97 = -2c per contract. SKIP!

6. ORDER EXECUTION
   TO GET AN IMMEDIATE FILL, YOU MUST CROSS THE SPREAD:

   TO BUY YES:
   - Find the best NO bid price in the orderbook
   - Your cost = 100 - no_bid_price
   - Place: side='yes', yes_price_cents = (100 - no_bid_price)
   - Example: NO bid at 40 -> YES costs 60. Place side='yes', yes_price_cents=60

   TO BUY NO:
   - Find the best YES bid price in the orderbook
   - Your cost = 100 - yes_bid_price
   - Place: side='no', yes_price_cents = yes_bid_price
   - Example: YES bid at 30 -> NO costs 70. Place side='no', yes_price_cents=30

   If the orderbook is EMPTY (no bids on either side), SKIP that contract.
   Do NOT post passive limit orders into an empty book.

7. PRE-TRADE CHECK
   Call get_account_balance to confirm sufficient funds.

8. PLACE ORDERS
   Place each bet. Check the response -- did it fill? What was the actual cost?
   The response includes _risk_reward showing your risk vs potential profit.

9. SUMMARY
   Print: | City | Contract | Your Prob | Cost | Profit if Win | EV | Filled? |

RULES:
- FOCUS ON VALUE: only bet contracts priced 20-70c with clear edge
- NEVER chase extreme prices (below 15c or above 85c) -- the system will reject them
- Always calculate EV BEFORE placing a bet
- Skip if no good opportunities exist -- passing is better than a bad bet
- Remember settlement uses STRICT > and < operators. Factor this into probability.
- NWS Daily Climate Report is the official settlement source"""


def dispatch_tool(name, inp, pk, api_key_id, base_url, dry_run, mode="", target_date=""):
    """Route a tool call to the correct function and return the result string."""
    if name == "get_nws_forecast":
        return tool_get_nws_forecast(inp["target_date"], inp.get("city", "CHI"))
    elif name == "get_current_conditions":
        return tool_get_current_conditions(inp.get("city", "CHI"))
    elif name == "search_kalshi_markets":
        return tool_search_kalshi_markets(pk, api_key_id, base_url, inp["keywords"])
    elif name == "get_orderbook":
        ticker = inp["ticker"]
        if not _TICKER_RE.match(ticker):
            return json.dumps({"error": f"Invalid ticker format: {ticker!r}"})
        return tool_get_orderbook(pk, api_key_id, base_url, ticker)
    elif name == "get_account_balance":
        return tool_get_account_balance(pk, api_key_id, base_url)
    elif name == "place_order":
        global _run_spend_cents
        ticker = inp["ticker"]
        # Ticker validation
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
        result = tool_place_order(
            pk,
            api_key_id,
            base_url,
            dry_run,
            ticker,
            side,
            ypc,
            count,
        )
        # Track spend and log the trade
        try:
            parsed = json.loads(result)
            if "error" not in parsed:
                # Update run spend tracker (use actual cost after any contract adjustment)
                if "would_place" in parsed:
                    actual_cost = int(parsed["cost_dollars"] * 100)
                elif "response" in parsed:
                    actual_cost = int(parsed["response"].get("_cost_dollars", 0) * 100)
                else:
                    actual_cost = cost_this_order
                _run_spend_cents += actual_cost
                print(f"  [SPEND] ${actual_cost/100:.2f} this order | ${_run_spend_cents/100:.2f} / ${MAX_RUN_DOLLARS:.2f} run total")
                # Extract city from ticker (e.g. KXHIGHCHI-26FEB12-B38.5 -> CHI)
                ticker = inp["ticker"]
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
                    mode=mode,
                    target_date=target_date,
                    city=city,
                    ticker=ticker,
                    title="",
                    side=inp["side"],
                    yes_price_cents=inp["yes_price_cents"],
                    contracts=inp["contracts"],
                    filled=filled,
                    order_id=order_id,
                    dry_run=dry_run,
                )
        except Exception:
            pass  # Don't let logging errors break trading
        return result
    else:
        return json.dumps({"error": f"Unknown tool: {name}"})


def run_agent(client, system_prompt, user_prompt, tools, pk, api_key_id, base_url, dry_run,
              mode="", target_date=""):
    """Run the agentic tool-use loop."""
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

        # If the model is done, exit
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

                # Show a preview of the result
                preview = result[:400] + ("..." if len(result) > 400 else "")
                print(f"  -> {preview}")

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

    print(f"\n{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="Kalshi Weather Betting Agent")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--live", action="store_true", help="Place real bets on production")
    group.add_argument("--demo", action="store_true", help="Place bets on demo account")
    parser.add_argument("--date", type=str, default=None, help="Target date (ISO format, e.g. 2026-02-15)")
    parser.add_argument(
        "--cities", nargs="+", default=None,
        help="City codes to target (e.g. CHI NYC MIA). Default: all cities.",
    )
    parser.add_argument(
        "--history", action="store_true",
        help="Show trade history and P&L summary, then exit.",
    )
    args = parser.parse_args()

    # Handle --history early (no credentials needed)
    if args.history:
        print_history()
        sys.exit(0)

    # Load environment
    load_dotenv()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set. Add it to your .env file.")
        sys.exit(1)

    # Determine mode and load corresponding Kalshi credentials
    if args.live:
        mode, base_url, dry_run = "LIVE", KALSHI_PROD_BASE, False
        kalshi_key_id = os.environ.get("KALSHI_PROD_API_KEY_ID")
        kalshi_pk_path = os.environ.get("KALSHI_PROD_PRIVATE_KEY_PATH")
    elif args.demo:
        mode, base_url, dry_run = "DEMO", KALSHI_DEMO_BASE, False
        kalshi_key_id = os.environ.get("KALSHI_DEMO_API_KEY_ID")
        kalshi_pk_path = os.environ.get("KALSHI_DEMO_PRIVATE_KEY_PATH")
    else:
        # Dry run uses production API (read-only) to see real markets
        mode, base_url, dry_run = "DRY RUN", KALSHI_PROD_BASE, True
        kalshi_key_id = os.environ.get("KALSHI_PROD_API_KEY_ID")
        kalshi_pk_path = os.environ.get("KALSHI_PROD_PRIVATE_KEY_PATH")

    if not kalshi_key_id or not kalshi_pk_path:
        print(f"Error: Kalshi credentials not set for {mode} mode. Check your .env file.")
        sys.exit(1)

    # Load Kalshi private key
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
    # Validate city codes
    valid_cities = [c for c in cities if c in CITY_CONFIGS]
    if not valid_cities:
        print(f"Error: No valid city codes. Available: {list(CITY_CONFIGS.keys())}")
        sys.exit(1)

    # Print banner
    print(f"\n{'=' * 60}")
    print(f"  KALSHI WEATHER AGENT")
    print(f"  Now:  {now.strftime('%I:%M %p CST, %A %b %d %Y')}")
    print(f"  Target: {target_date}")
    print(f"  Cities: {', '.join(valid_cities)}")
    print(f"  Mode: {mode}")
    print(f"{'=' * 60}")

    # Build tools list
    tools = NWS_TOOL_DEFINITIONS + MARKET_TOOL_DEFINITIONS + TRADING_TOOL_DEFINITIONS

    # Build prompts
    system_prompt = build_system_prompt(now, target_date, mode, dry_run, valid_cities)
    user_prompt = (
        f"Research weather forecasts and place bets on {target_date} temperature markets "
        f"for these cities: {', '.join(valid_cities)}. "
        f"Current time: {now.strftime('%I:%M %p CST')}. "
        f"IMPORTANT: When you find good bets, TAKE the existing liquidity at the ask/bid price. "
        f"Do NOT post passive limit orders at extreme prices. "
        f"Be methodical -- check timing carefully before betting. Go."
    )

    # Create Anthropic client and run
    client = anthropic.Anthropic(api_key=api_key)
    run_start = datetime.datetime.now(CST).isoformat()

    try:
        run_agent(client, system_prompt, user_prompt, tools, pk, kalshi_key_id, base_url, dry_run,
                  mode=mode, target_date=target_date)
    except Exception as e:
        notify_error("agent.py", str(e))
        raise

    # Send Discord notification for any trades placed during this run
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
