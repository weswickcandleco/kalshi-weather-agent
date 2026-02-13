#!/usr/bin/env python3
"""
Kalshi Weather Agent -- Automated Trader (No Claude API)
========================================================
Pure Python auto-trader that uses the probability model to find and execute
positive-EV bets. No Anthropic API calls -- $0/run.

Uses the same probability model, risk guardrails, and trade logging as agent.py,
but replaces Claude's decision-making with a simple rule: bet when EV > threshold.

Usage:
  python3 auto_trade.py              # dry run (default)
  python3 auto_trade.py --live       # real money
  python3 auto_trade.py --demo       # demo account
  python3 auto_trade.py --date 2026-02-15  # target a specific date
  python3 auto_trade.py --cities CHI NYC MIA  # specific cities only
"""

import os
import sys
import json
import time
import re
import argparse
import datetime

import requests
from dotenv import load_dotenv
from scipy.stats import norm

from config import (
    CST,
    KALSHI_PROD_BASE,
    KALSHI_DEMO_BASE,
    MAX_BET_DOLLARS,
    MAX_CONTRACTS_PER_ORDER,
    MAX_RUN_DOLLARS,
    CITY_CONFIGS,
    WORKER_URL,
)

from tools.kalshi_auth import load_private_key
from tools.kalshi_trading import tool_get_account_balance, tool_place_order
from tools.trade_log import log_trade, log_run, get_trade_history
from tools.notify import notify_bets_placed, notify_error

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_EV_CENTS = 5       # minimum EV to place a bet
MIN_PRICE_CENTS = 15   # mirrors kalshi_trading.py guardrail
MAX_PRICE_CENTS = 85   # mirrors kalshi_trading.py guardrail

CONTRACT_RE = re.compile(r"KX(HIGH|LOWT)([A-Z]+)-\d+[A-Z]+\d+-([BT])([\d\.]+)")

# NWS Day-1 forecast error standard deviations (Fahrenheit) by city and season
FORECAST_ERROR_SD = {
    "CHI":  {"winter": 4.0, "spring": 3.2, "summer": 2.2, "autumn": 3.0},
    "NYC":  {"winter": 3.5, "spring": 2.8, "summer": 2.0, "autumn": 2.8},
    "MIA":  {"winter": 2.0, "spring": 1.8, "summer": 1.5, "autumn": 1.8},
    "LAX":  {"winter": 2.0, "spring": 2.2, "summer": 1.5, "autumn": 2.5},
    "AUS":  {"winter": 3.0, "spring": 2.5, "summer": 2.0, "autumn": 2.5},
    "DEN":  {"winter": 4.5, "spring": 3.5, "summer": 2.5, "autumn": 3.5},
    "PHIL": {"winter": 3.5, "spring": 2.8, "summer": 2.0, "autumn": 2.8},
}
_DEFAULT_SD = {"winter": 3.5, "spring": 3.0, "summer": 2.0, "autumn": 3.0}


# ---------------------------------------------------------------------------
# Probability model (same math as agent.py)
# ---------------------------------------------------------------------------

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


def _contract_prob(ticker, forecast_high, forecast_low, season, yes_sub_title=""):
    """Compute P(YES wins) for a contract given NWS forecast temps."""
    m = CONTRACT_RE.search(ticker)
    if not m:
        return None, None
    temp_type = m.group(1)
    city = m.group(2)
    bet_type = m.group(3)
    value = float(m.group(4))

    forecast = forecast_high if temp_type == "HIGH" else forecast_low
    if forecast is None:
        return None, city

    sd_table = FORECAST_ERROR_SD.get(city, _DEFAULT_SD)
    sd = sd_table.get(season, 3.0)

    if bet_type == "B":
        low_bound = int(value)
        high_bound = low_bound + 1
        prob = norm.cdf(high_bound + 0.5, forecast, sd) - norm.cdf(low_bound - 0.5, forecast, sd)
    else:
        threshold = int(value)
        if "below" in yes_sub_title.lower():
            prob = norm.cdf(threshold - 0.5, forecast, sd)
        else:
            prob = 1.0 - norm.cdf(threshold + 0.5, forecast, sd)

    return max(0.0, min(1.0, prob)), city


# ---------------------------------------------------------------------------
# Data fetching (same as agent.py)
# ---------------------------------------------------------------------------

def _fetch_worker(target_date, city_codes):
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


# ---------------------------------------------------------------------------
# Bet selection engine
# ---------------------------------------------------------------------------

def find_bets(bundle, target_date):
    """Scan all contracts and return a list of positive-EV bets to place.

    Each bet dict has: ticker, side, yes_price_cents, contracts, cost_cents,
    ev_cents, model_prob, city, title
    """
    season = _get_season(target_date)
    bets = []

    for code, city in bundle.get("cities", {}).items():
        w = city.get("weather", {})
        forecast_high = w.get("predicted_high_f")
        forecast_low = w.get("predicted_low_f")

        for mtype in ("high", "low"):
            mdata = city.get("markets", {}).get(mtype, {})
            for c in mdata.get("contracts", []):
                ticker = c["ticker"]
                yes_sub = c.get("yes_sub_title", "")
                prob, _ = _contract_prob(ticker, forecast_high, forecast_low, season, yes_sub)
                if prob is None:
                    continue

                ob = c.get("orderbook") or {}
                yes_bids = ob.get("yes") or []
                no_bids = ob.get("no") or []

                # Evaluate YES side
                if no_bids:
                    best_no_bid = no_bids[-1][0] if isinstance(no_bids[-1], list) else no_bids[-1]
                    cost_yes = 100 - best_no_bid
                    ev_yes = prob * (100 - cost_yes) - (1 - prob) * cost_yes
                else:
                    cost_yes = None
                    ev_yes = None

                # Evaluate NO side
                if yes_bids:
                    best_yes_bid = yes_bids[-1][0] if isinstance(yes_bids[-1], list) else yes_bids[-1]
                    cost_no = 100 - best_yes_bid
                    ev_no = (1 - prob) * (100 - cost_no) - prob * cost_no
                else:
                    cost_no = None
                    ev_no = None

                # Pick the best side (if any meets threshold)
                candidates = []
                if ev_yes is not None and ev_yes >= MIN_EV_CENTS and MIN_PRICE_CENTS <= cost_yes <= MAX_PRICE_CENTS:
                    candidates.append(("yes", cost_yes, ev_yes))
                if ev_no is not None and ev_no >= MIN_EV_CENTS and MIN_PRICE_CENTS <= cost_no <= MAX_PRICE_CENTS:
                    candidates.append(("no", cost_no, ev_no))

                if not candidates:
                    continue

                # Pick the side with higher EV
                best = max(candidates, key=lambda x: x[2])
                side, cost, ev = best

                # Determine yes_price_cents for the order
                if side == "yes":
                    yes_price_cents = cost
                else:
                    yes_price_cents = 100 - cost

                # Determine contract count (max 5, respect MAX_BET_DOLLARS)
                contracts = min(MAX_CONTRACTS_PER_ORDER, int(MAX_BET_DOLLARS * 100 / cost))
                if contracts < 1:
                    contracts = 1

                bets.append({
                    "ticker": ticker,
                    "side": side,
                    "yes_price_cents": yes_price_cents,
                    "contracts": contracts,
                    "cost_cents": cost * contracts,
                    "ev_cents": round(ev, 1),
                    "model_prob": round(prob, 3),
                    "city": code,
                    "title": yes_sub,
                    "forecast_high_f": forecast_high,
                    "forecast_low_f": forecast_low,
                })

    # Sort by EV descending (best bets first)
    bets.sort(key=lambda b: b["ev_cents"], reverse=True)
    return bets


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def execute_bets(bets, pk, api_key_id, base_url, dry_run, mode, target_date):
    """Execute the selected bets, respecting the per-run spending cap.

    Returns list of result dicts for notification.
    """
    run_spend_cents = 0
    results = []

    for bet in bets:
        cost_this = bet["cost_cents"]
        if (run_spend_cents + cost_this) > int(MAX_RUN_DOLLARS * 100):
            remaining = int(MAX_RUN_DOLLARS * 100) - run_spend_cents
            # Try to reduce contracts to fit
            cost_per = cost_this // bet["contracts"]
            can_afford = remaining // cost_per if cost_per > 0 else 0
            if can_afford < 1:
                print(f"  SKIP (cap): {bet['ticker']} -- need {cost_this}c, only {remaining}c left")
                continue
            bet["contracts"] = can_afford
            bet["cost_cents"] = cost_per * can_afford
            cost_this = bet["cost_cents"]

        print(f"\n  [{bet['city']}] {bet['ticker']}")
        print(f"    Side: {bet['side'].upper()} | Cost: {cost_this}c ({bet['contracts']}x) | "
              f"EV: +{bet['ev_cents']:.0f}c | P={bet['model_prob']:.2f}")

        result_json = tool_place_order(
            pk, api_key_id, base_url, dry_run,
            bet["ticker"], bet["side"], bet["yes_price_cents"], bet["contracts"]
        )
        result = json.loads(result_json)

        if "error" in result:
            print(f"    REJECTED: {result['error']}")
            continue

        # Track spending
        if "would_place" in result:
            actual_cost = int(result["cost_dollars"] * 100)
        elif "response" in result:
            actual_cost = int(result["response"].get("_cost_dollars", 0) * 100)
        else:
            actual_cost = cost_this
        run_spend_cents += actual_cost

        # Determine fill status
        filled = False
        order_id = None
        if not dry_run and "response" in result:
            resp = result["response"]
            order_data = resp.get("order", resp)
            filled = order_data.get("fill_count", 0) > 0
            order_id = order_data.get("order_id", order_data.get("client_order_id"))

        status = "FILLED" if filled else ("DRY" if dry_run else "RESTING")
        print(f"    -> {status} | Spend: ${run_spend_cents/100:.2f} / ${MAX_RUN_DOLLARS:.2f}")

        # Log the trade
        log_trade(
            mode=mode, target_date=target_date, city=bet["city"],
            ticker=bet["ticker"], title=bet["title"], side=bet["side"],
            yes_price_cents=bet["yes_price_cents"], contracts=bet["contracts"],
            forecast_high_f=bet.get("forecast_high_f"),
            forecast_low_f=bet.get("forecast_low_f"),
            est_probability=bet["model_prob"],
            expected_value_cents=bet["ev_cents"],
            filled=filled, order_id=order_id, dry_run=dry_run,
        )

        results.append(bet)

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Kalshi Weather Auto-Trader (No API)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--live", action="store_true", help="Place real bets on production")
    group.add_argument("--demo", action="store_true", help="Place bets on demo account")
    parser.add_argument("--date", type=str, default=None, help="Target date (YYYY-MM-DD)")
    parser.add_argument("--cities", nargs="+", default=None, help="City codes (e.g. CHI NYC)")
    args = parser.parse_args()

    load_dotenv()

    # Determine mode
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
        print(f"Error: Kalshi credentials not set for {mode} mode.")
        sys.exit(1)

    try:
        pk = load_private_key(kalshi_pk_path)
    except Exception as e:
        print(f"Error loading Kalshi private key: {e}")
        sys.exit(1)

    # Target date
    now = datetime.datetime.now(CST)
    if args.date:
        target_date = args.date
    else:
        tomorrow = (now + datetime.timedelta(days=1)).date()
        target_date = tomorrow.isoformat()

    # Cities
    cities = args.cities if args.cities else list(CITY_CONFIGS.keys())
    valid_cities = [c for c in cities if c in CITY_CONFIGS]
    if not valid_cities:
        print(f"Error: No valid city codes. Available: {list(CITY_CONFIGS.keys())}")
        sys.exit(1)

    season = _get_season(target_date)

    # Banner
    print(f"\n{'=' * 60}")
    print(f"  KALSHI AUTO-TRADER (pure Python, $0 API cost)")
    print(f"  Now:    {now.strftime('%I:%M %p CT, %A %b %d %Y')}")
    print(f"  Target: {target_date}")
    print(f"  Cities: {', '.join(valid_cities)}")
    print(f"  Season: {season}")
    print(f"  Mode:   {mode}")
    print(f"  EV threshold: +{MIN_EV_CENTS}c | Price range: {MIN_PRICE_CENTS}-{MAX_PRICE_CENTS}c")
    print(f"{'=' * 60}")

    # Step 1: Fetch data
    print(f"\n[FETCH] Getting data for {target_date}...")
    try:
        bundle = fetch_bundle(target_date, valid_cities)
        print(f"[FETCH] Got data for {len(bundle.get('cities', {}))} cities")
        if bundle.get("errors"):
            for err in bundle["errors"]:
                print(f"[FETCH] Warning: {err['city']} - {err['error']}")
    except Exception as e:
        print(f"[FETCH] FAILED: {e}")
        notify_error("auto_trade.py", f"Worker fetch failed: {e}")
        sys.exit(1)

    # Step 2: Find positive-EV bets
    print(f"\n[MODEL] Scanning contracts...")
    bets = find_bets(bundle, target_date)
    print(f"[MODEL] Found {len(bets)} positive-EV bet(s)")

    if not bets:
        print("\nNo bets today -- no contracts met the EV threshold.")
        # Log the run even if no bets
        log_run(mode, target_date, valid_cities, 0, 0, 0)
        return

    # Print summary table
    print(f"\n{'Ticker':<30} {'Side':<4} {'Cost':>5} {'EV':>5} {'Prob':>5}")
    print(f"{'-'*30} {'-'*4} {'-'*5} {'-'*5} {'-'*5}")
    for b in bets:
        cost_per = b["cost_cents"] // b["contracts"]
        print(f"{b['ticker']:<30} {b['side'].upper():<4} {cost_per:>4}c {b['ev_cents']:>+4.0f}c {b['model_prob']:>5.2f}")

    # Step 3: Check balance (if not dry run)
    if not dry_run:
        bal_json = tool_get_account_balance(pk, kalshi_key_id, base_url)
        bal = json.loads(bal_json)
        if "error" not in bal:
            print(f"\n[BALANCE] ${bal['balance_dollars']:.2f}")
        else:
            print(f"\n[BALANCE] Error: {bal['error']}")

    # Step 4: Execute bets
    print(f"\n[EXECUTE] Placing {len(bets)} bet(s)...")
    run_start = datetime.datetime.now(CST).isoformat()
    results = execute_bets(bets, pk, kalshi_key_id, base_url, dry_run, mode, target_date)

    # Step 5: Log the run
    total_cost = sum(r["cost_cents"] for r in results)
    log_run(mode, target_date, valid_cities, len(results), len(bets) - len(results), total_cost)

    # Step 6: Notify Discord
    if results and not dry_run:
        try:
            recent = get_trade_history(limit=50)
            run_trades = [t for t in recent
                          if t["timestamp"] >= run_start
                          and t["filled"]
                          and not t["dry_run"]]
            if run_trades:
                notify_bets_placed(run_trades, mode, target_date,
                                   token_stats={"input_tokens": 0, "output_tokens": 0, "cost_estimate": 0.0})
        except Exception as e:
            print(f"[NOTIFY] Failed: {e}")

    # Summary
    print(f"\n{'=' * 60}")
    total_ev = sum(r["ev_cents"] for r in results)
    print(f"  Placed: {len(results)} bet(s) | Cost: ${total_cost/100:.2f} | Total EV: +{total_ev:.0f}c")
    print(f"  API cost: $0.00")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
