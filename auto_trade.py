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
from tools.trade_log import log_trade, log_run, get_trade_history, get_existing_tickers, get_city_bet_count, export_dashboard_data
from tools.notify import notify_bets_placed, notify_bet_logic, notify_error

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_EV_CENTS = 5       # minimum EV to place a bet
MIN_PRICE_CENTS = 15   # mirrors kalshi_trading.py guardrail
MAX_PRICE_CENTS = 85   # mirrors kalshi_trading.py guardrail
MAX_BETS_PER_CITY = 2  # max bets per city per day

CONTRACT_RE = re.compile(r"KX(HIGH|LOWT)([A-Z]+)-\d+[A-Z]+\d+-([BT])([\d\.]+)")

# NWS Day-1 forecast error standard deviations (Fahrenheit) by city and season
FORECAST_ERROR_SD = {
    "CHI":  {"winter": 7.0, "spring": 5.5, "summer": 4.0, "autumn": 5.5},
    "NYC":  {"winter": 6.0, "spring": 5.0, "summer": 3.5, "autumn": 5.0},
    "MIA":  {"winter": 4.0, "spring": 3.5, "summer": 3.0, "autumn": 3.5},
    "LAX":  {"winter": 4.5, "spring": 4.0, "summer": 3.5, "autumn": 5.0},
    "AUS":  {"winter": 6.0, "spring": 5.0, "summer": 4.0, "autumn": 5.0},
    "DEN":  {"winter": 8.0, "spring": 6.5, "summer": 4.5, "autumn": 6.0},
    "PHIL": {"winter": 6.0, "spring": 5.0, "summer": 3.5, "autumn": 5.0},
}
_DEFAULT_SD = {"winter": 6.0, "spring": 5.0, "summer": 3.5, "autumn": 5.0}


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
    """Compute P(YES wins) for a contract given NWS forecast temps (hardcoded SD fallback)."""
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


def _contract_prob_ensemble(ticker, ensemble_data, yes_sub_title=""):
    """Compute P(YES wins) using ensemble member data (empirical distribution).

    Uses the actual spread of 100+ weather model runs instead of a hardcoded
    standard deviation. Falls back to None if ensemble data is insufficient.
    """
    m = CONTRACT_RE.search(ticker)
    if not m:
        return None, None
    temp_type = m.group(1)
    city = m.group(2)
    bet_type = m.group(3)
    value = float(m.group(4))

    if temp_type == "HIGH":
        members = ensemble_data.get("high_members", [])
    else:
        members = ensemble_data.get("low_members", [])

    if not members or len(members) < 10:
        return None, city

    n = len(members)

    if bet_type == "B":
        # Bracket contract: temp lands in [value, value+1] (2-degree bucket)
        low_bound = int(value)
        high_bound = low_bound + 1
        count = sum(1 for t in members if low_bound - 0.5 <= t <= high_bound + 0.5)
        prob = count / n
    else:
        # Threshold contract
        threshold = int(value)
        if "below" in yes_sub_title.lower():
            count = sum(1 for t in members if t < threshold - 0.5)
        else:
            count = sum(1 for t in members if t >= threshold + 0.5)
        prob = count / n

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

def find_bets(bundle, target_date, market_type="all"):
    """Scan all contracts and return a list of positive-EV bets to place.

    Applies all guardrails:
    - Deduplication: skip tickers we already have positions on
    - City limits: max MAX_BETS_PER_CITY bets per city per day
    - Contradictory bet blocking: don't bet opposite sides on same contract
    - Negative-EV blocking: only bet when EV > MIN_EV_CENTS

    Each bet dict has: ticker, side, yes_price_cents, contracts, cost_cents,
    ev_cents, model_prob, city, title
    """
    season = _get_season(target_date)

    # --- Guardrail: Deduplication ---
    existing_positions = get_existing_tickers(target_date)
    existing_tickers = {ticker for ticker, _ in existing_positions}
    if existing_tickers:
        print(f"  [DEDUP] Already have {len(existing_tickers)} position(s) for {target_date}")

    # --- Guardrail: City limits ---
    city_counts = get_city_bet_count(target_date)

    bets = []

    for code, city in bundle.get("cities", {}).items():
        # Check city limit before scanning
        existing_city_bets = city_counts.get(code, 0)
        if existing_city_bets >= MAX_BETS_PER_CITY:
            print(f"  [LIMIT] {code}: already has {existing_city_bets} bet(s), skipping (max {MAX_BETS_PER_CITY})")
            continue

        w = city.get("weather", {})
        forecast_high = w.get("predicted_high_f")
        forecast_low = w.get("predicted_low_f")

        # Check for ensemble data and compute stats
        ensemble = w.get("ensemble") or {}
        has_ensemble = ensemble.get("member_count", 0) >= 10 and not ensemble.get("error")
        ens_stats = {}
        if has_ensemble:
            high_m = ensemble.get("high_members", [])
            low_m = ensemble.get("low_members", [])
            ens_stats["count"] = ensemble.get("member_count", 0)
            if high_m:
                mean_h = sum(high_m) / len(high_m)
                sd_h = (sum((t - mean_h) ** 2 for t in high_m) / len(high_m)) ** 0.5
                ens_stats["mean_high"] = round(mean_h, 1)
                ens_stats["sd_high"] = round(sd_h, 1)
            if low_m:
                mean_l = sum(low_m) / len(low_m)
                sd_l = (sum((t - mean_l) ** 2 for t in low_m) / len(low_m)) ** 0.5
                ens_stats["mean_low"] = round(mean_l, 1)
                ens_stats["sd_low"] = round(sd_l, 1)
            print(f"  [ENSEMBLE] {code}: {ens_stats['count']} members | "
                  f"high={ens_stats.get('mean_high', '?')}F +/-{ens_stats.get('sd_high', '?')} | "
                  f"low={ens_stats.get('mean_low', '?')}F +/-{ens_stats.get('sd_low', '?')}")
        else:
            ens_err = ensemble.get("error", "no data")
            print(f"  [ENSEMBLE] {code}: unavailable ({ens_err}), using SD model")

        current_temp = w.get("current_temp_f")
        city_new_bets = 0
        city_bets_buffer = []

        market_types = ("high", "low") if market_type == "all" else (market_type,)
        for mtype in market_types:
            mdata = city.get("markets", {}).get(mtype, {})
            for c in mdata.get("contracts", []):
                ticker = c["ticker"]
                yes_sub = c.get("yes_sub_title", "")

                # --- Guardrail: Deduplication ---
                if ticker in existing_tickers:
                    print(f"  [DEDUP] Skipping {ticker} -- already have a position")
                    continue

                # Try ensemble-based probability first, fall back to hardcoded SD
                prob = None
                prob_source = "sd_model"
                if has_ensemble:
                    prob, _ = _contract_prob_ensemble(ticker, ensemble, yes_sub)
                    if prob is not None:
                        prob_source = "ensemble"
                if prob is None:
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

                # --- Guardrail: Block negative-EV bets ---
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

                # --- Guardrail: Contradictory bet blocking ---
                # Don't bet opposite side of a ticker we already hold
                opposite = "no" if side == "yes" else "yes"
                if (ticker, opposite) in existing_positions:
                    print(f"  [CONFLICT] Skipping {side.upper()} on {ticker} -- already hold {opposite.upper()}")
                    continue

                # Determine yes_price_cents for the order
                if side == "yes":
                    yes_price_cents = cost
                else:
                    yes_price_cents = 100 - cost

                # Determine contract count (max 5, respect MAX_BET_DOLLARS)
                contracts = min(MAX_CONTRACTS_PER_ORDER, int(MAX_BET_DOLLARS * 100 / cost))
                if contracts < 1:
                    contracts = 1

                city_bets_buffer.append({
                    "ticker": ticker,
                    "side": side,
                    "yes_price_cents": yes_price_cents,
                    "contracts": contracts,
                    "cost_cents": cost * contracts,
                    "ev_cents": round(ev, 1),
                    "model_prob": round(prob, 3),
                    "prob_source": prob_source,
                    "city": code,
                    "title": yes_sub,
                    "forecast_high_f": forecast_high,
                    "forecast_low_f": forecast_low,
                    "ensemble_member_count": ens_stats.get("count"),
                    "ensemble_mean_high": ens_stats.get("mean_high"),
                    "ensemble_mean_low": ens_stats.get("mean_low"),
                    "ensemble_sd_high": ens_stats.get("sd_high"),
                    "ensemble_sd_low": ens_stats.get("sd_low"),
                    "current_temp_f": current_temp,
                })

        # --- Guardrail: City limit (for new bets this run) ---
        city_bets_buffer.sort(key=lambda b: b["ev_cents"], reverse=True)
        slots_left = MAX_BETS_PER_CITY - existing_city_bets
        for bet in city_bets_buffer[:slots_left]:
            bets.append(bet)
        if len(city_bets_buffer) > slots_left:
            skipped = len(city_bets_buffer) - slots_left
            print(f"  [LIMIT] {code}: capped at {slots_left} new bet(s), skipped {skipped} lower-EV")

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
            prob_source=bet.get("prob_source"),
            ensemble_member_count=bet.get("ensemble_member_count"),
            ensemble_mean_high=bet.get("ensemble_mean_high"),
            ensemble_mean_low=bet.get("ensemble_mean_low"),
            ensemble_sd_high=bet.get("ensemble_sd_high"),
            ensemble_sd_low=bet.get("ensemble_sd_low"),
            current_temp_f=bet.get("current_temp_f"),
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
    parser.add_argument("--market-type", choices=["all", "high", "low"], default="all",
                        help="Only bet on high or low contracts (default: all)")
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
    print(f"  Market: {args.market_type.upper()}")
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

    # Step 1b: Cross-check forecasts (sanity check)
    print(f"\n[CHECK] Cross-checking forecasts...")
    for code, city in bundle.get("cities", {}).items():
        w = city.get("weather", {})
        pred_high = w.get("predicted_high_f")
        pred_low = w.get("predicted_low_f")
        current = w.get("current_temp_f")
        hourly = w.get("hourly", [])

        if pred_high is None or pred_low is None:
            print(f"  [CHECK] {code}: NO FORECAST DATA -- skipping this city")
            # Remove city from bundle so find_bets won't touch it
            bundle["cities"][code]["weather"]["predicted_high_f"] = None
            bundle["cities"][code]["weather"]["predicted_low_f"] = None
            continue

        # Check if current observation already exceeds forecast
        if current is not None:
            if current > pred_high + 3:
                print(f"  [CHECK] {code}: Current temp {current}F already exceeds forecast high {pred_high}F by {current - pred_high:.0f}F -- forecast may be stale")
            if current < pred_low - 3:
                print(f"  [CHECK] {code}: Current temp {current}F already below forecast low {pred_low}F by {pred_low - current:.0f}F -- forecast may be stale")

        # Check hourly spread vs predicted range
        if hourly:
            hourly_temps = [h.get("temp_f") for h in hourly if h.get("temp_f") is not None]
            if hourly_temps:
                hourly_high = max(hourly_temps)
                hourly_low = min(hourly_temps)
                if abs(hourly_high - pred_high) > 3:
                    print(f"  [CHECK] {code}: Hourly max {hourly_high}F differs from predicted high {pred_high}F by {abs(hourly_high - pred_high):.0f}F")
                if abs(hourly_low - pred_low) > 3:
                    print(f"  [CHECK] {code}: Hourly min {hourly_low}F differs from predicted low {pred_low}F by {abs(hourly_low - pred_low):.0f}F")

    # Step 2: Find positive-EV bets
    print(f"\n[MODEL] Scanning contracts...")
    market_type = args.market_type
    bets = find_bets(bundle, target_date, market_type=market_type)
    print(f"[MODEL] Found {len(bets)} positive-EV bet(s)")

    if not bets:
        print("\nNo bets today -- no contracts met the EV threshold.")
        # Log the run even if no bets
        log_run(mode, target_date, valid_cities, 0, 0, 0)
        _export_dashboard_json()
        return

    # Print summary table
    print(f"\n{'Ticker':<30} {'Side':<4} {'Cost':>5} {'EV':>5} {'Prob':>5} {'Source':<8}")
    print(f"{'-'*30} {'-'*4} {'-'*5} {'-'*5} {'-'*5} {'-'*8}")
    for b in bets:
        cost_per = b["cost_cents"] // b["contracts"]
        src = b.get("prob_source", "sd")[:8]
        print(f"{b['ticker']:<30} {b['side'].upper():<4} {cost_per:>4}c {b['ev_cents']:>+4.0f}c {b['model_prob']:>5.2f} {src:<8}")

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
                notify_bet_logic(run_trades, target_date)
        except Exception as e:
            print(f"[NOTIFY] Failed: {e}")

    # Summary
    print(f"\n{'=' * 60}")
    total_ev = sum(r["ev_cents"] for r in results)
    print(f"  Placed: {len(results)} bet(s) | Cost: ${total_cost/100:.2f} | Total EV: +{total_ev:.0f}c")
    print(f"  API cost: $0.00")
    print(f"{'=' * 60}\n")

    # Step 7: Export dashboard data
    _export_dashboard_json()


def _export_dashboard_json():
    """Export all trade/run data to dashboard/data.json for the live dashboard."""
    try:
        dashboard_dir = os.path.join(os.path.dirname(__file__), "dashboard")
        os.makedirs(dashboard_dir, exist_ok=True)
        data = export_dashboard_data()
        out_path = os.path.join(dashboard_dir, "data.json")
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"[EXPORT] Dashboard data written to dashboard/data.json")
    except Exception as e:
        print(f"[EXPORT] Failed: {e}")


if __name__ == "__main__":
    main()
