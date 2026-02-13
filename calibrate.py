#!/usr/bin/env python3
"""
Kalshi Weather Agent -- Model Calibration
==========================================
Analyzes settled trades to compare forecast vs observed temperatures,
compute actual forecast error standard deviations, and check probability
calibration.

Run periodically (or after accumulating ~30+ settled trades) to see if the
model's SD assumptions match reality.

Usage:
  python3 calibrate.py              # full report
  python3 calibrate.py --update     # update FORECAST_ERROR_SD in auto_trade.py and agent.py
"""

import argparse
import sqlite3
import re
import math
from collections import defaultdict

from tools.trade_log import DB_PATH

CONTRACT_RE = re.compile(r"KX(HIGH|LOWT)([A-Z]+)-\d+[A-Z]+\d+-([BT])([\d\.]+)")


def _get_season_from_date(date_str):
    month = int(date_str[5:7])
    if month in (12, 1, 2):
        return "winter"
    elif month in (3, 4, 5):
        return "spring"
    elif month in (6, 7, 8):
        return "summer"
    else:
        return "autumn"


def load_settled_trades():
    """Load all settled trades that have both forecast and observed temps."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT * FROM trades
        WHERE settlement_result IN ('win', 'loss')
        AND filled = 1
        AND dry_run = 0
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]


def analyze_forecast_errors(trades):
    """Compute forecast error stats grouped by city and season.

    Returns dict: {(city, season): {"errors": [...], "n": N, "mean_error": X, "sd": Y}}
    """
    # Group forecast errors by (city, season, temp_type)
    errors_by_group = defaultdict(list)

    for t in trades:
        forecast_high = t.get("forecast_high_f")
        forecast_low = t.get("forecast_low_f")
        observed_high = t.get("observed_high_f")
        observed_low = t.get("observed_low_f")

        if forecast_high is None or observed_high is None:
            continue

        m = CONTRACT_RE.search(t["ticker"])
        if not m:
            continue

        city = m.group(1 + 1)  # group(2) = city
        season = _get_season_from_date(t["target_date"])

        # High temp error
        if forecast_high is not None and observed_high is not None:
            errors_by_group[(city, season, "HIGH")].append(observed_high - forecast_high)

        # Low temp error
        if forecast_low is not None and observed_low is not None:
            errors_by_group[(city, season, "LOW")].append(observed_low - forecast_low)

    # Compute stats per (city, season) combining HIGH and LOW errors
    # (forecast error SD should be similar for both)
    combined = defaultdict(list)
    for (city, season, _), errs in errors_by_group.items():
        combined[(city, season)].extend(errs)

    results = {}
    for (city, season), errs in sorted(combined.items()):
        n = len(errs)
        mean_err = sum(errs) / n if n > 0 else 0
        variance = sum((e - mean_err) ** 2 for e in errs) / n if n > 1 else 0
        sd = math.sqrt(variance) if variance > 0 else 0
        results[(city, season)] = {
            "errors": errs,
            "n": n,
            "mean_error": round(mean_err, 2),
            "sd": round(sd, 2),
        }

    return results


def analyze_probability_calibration(trades):
    """Check if model probabilities match actual win rates.

    Groups trades into probability buckets and compares predicted vs actual.
    """
    buckets = defaultdict(lambda: {"count": 0, "wins": 0, "total_prob": 0})

    for t in trades:
        prob = t.get("est_probability")
        if prob is None:
            continue

        # Bucket by 10% ranges
        bucket = min(int(prob * 10), 9)  # 0-9 representing 0-10%, 10-20%, etc.
        bucket_label = f"{bucket*10}-{(bucket+1)*10}%"

        buckets[bucket_label]["count"] += 1
        buckets[bucket_label]["total_prob"] += prob
        if t["settlement_result"] == "win":
            buckets[bucket_label]["wins"] += 1

    results = {}
    for label in sorted(buckets.keys()):
        b = buckets[label]
        avg_prob = b["total_prob"] / b["count"] if b["count"] > 0 else 0
        actual_rate = b["wins"] / b["count"] if b["count"] > 0 else 0
        results[label] = {
            "count": b["count"],
            "wins": b["wins"],
            "avg_predicted": round(avg_prob, 3),
            "actual_win_rate": round(actual_rate, 3),
            "calibration_error": round(actual_rate - avg_prob, 3),
        }

    return results


def print_report(trades):
    """Print a full calibration report."""
    if not trades:
        print("No settled trades with forecast/observed data yet.")
        print("Data will accumulate as trades are placed and settled.")
        return

    print(f"\n{'=' * 70}")
    print(f"  CALIBRATION REPORT ({len(trades)} settled trades)")
    print(f"{'=' * 70}")

    # --- Forecast Error Analysis ---
    errors = analyze_forecast_errors(trades)
    if errors:
        print(f"\n  FORECAST ERROR BY CITY/SEASON")
        print(f"  {'City':<6} {'Season':<8} {'N':>4} {'Mean Err':>9} {'SD':>6} {'Current SD':>11}")
        print(f"  {'-'*6} {'-'*8} {'-'*4} {'-'*9} {'-'*6} {'-'*11}")

        # Import current SDs for comparison
        from auto_trade import FORECAST_ERROR_SD, _DEFAULT_SD

        for (city, season), stats in sorted(errors.items()):
            current_sd = FORECAST_ERROR_SD.get(city, _DEFAULT_SD).get(season, 3.0)
            marker = ""
            if stats["n"] >= 10:
                diff = abs(stats["sd"] - current_sd)
                if diff > 1.0:
                    marker = " <<< ADJUST"
                elif diff > 0.5:
                    marker = " < check"

            print(f"  {city:<6} {season:<8} {stats['n']:>4} "
                  f"{stats['mean_error']:>+8.1f}F {stats['sd']:>5.1f}F "
                  f"{current_sd:>10.1f}F{marker}")

        # Check for systematic bias
        all_errors = []
        for stats in errors.values():
            all_errors.extend(stats["errors"])
        if all_errors:
            overall_bias = sum(all_errors) / len(all_errors)
            print(f"\n  Overall forecast bias: {overall_bias:+.1f}F "
                  f"({'warm' if overall_bias > 0 else 'cool'} bias, N={len(all_errors)})")
            if abs(overall_bias) > 1.5:
                print(f"  WARNING: Significant bias detected. NWS may be systematically "
                      f"{'under' if overall_bias > 0 else 'over'}-forecasting.")

    # --- Probability Calibration ---
    cal = analyze_probability_calibration(trades)
    if cal:
        print(f"\n  PROBABILITY CALIBRATION")
        print(f"  {'Bucket':<10} {'N':>4} {'Wins':>5} {'Predicted':>10} {'Actual':>8} {'Error':>8}")
        print(f"  {'-'*10} {'-'*4} {'-'*5} {'-'*10} {'-'*8} {'-'*8}")

        total_count = 0
        total_wins = 0
        for label, stats in sorted(cal.items()):
            total_count += stats["count"]
            total_wins += stats["wins"]
            err_marker = ""
            if stats["count"] >= 5 and abs(stats["calibration_error"]) > 0.15:
                err_marker = " <<<" if stats["calibration_error"] > 0 else " >>>"
            print(f"  {label:<10} {stats['count']:>4} {stats['wins']:>5} "
                  f"{stats['avg_predicted']:>9.1%} {stats['actual_win_rate']:>7.1%} "
                  f"{stats['calibration_error']:>+7.1%}{err_marker}")

        if total_count > 0:
            overall_wr = total_wins / total_count
            print(f"\n  Overall: {total_wins}/{total_count} = {overall_wr:.1%} win rate")

    # --- Suggested SD Updates ---
    print(f"\n  SUGGESTED SD UPDATES")
    suggestions = {}
    for (city, season), stats in errors.items():
        if stats["n"] >= 15 and stats["sd"] > 0:
            from auto_trade import FORECAST_ERROR_SD, _DEFAULT_SD
            current = FORECAST_ERROR_SD.get(city, _DEFAULT_SD).get(season, 3.0)
            # Blend: 70% observed, 30% prior (regularization to avoid overfitting small samples)
            blended = round(0.7 * stats["sd"] + 0.3 * current, 1)
            if abs(blended - current) >= 0.3:
                suggestions[(city, season)] = {
                    "current": current,
                    "observed": stats["sd"],
                    "suggested": blended,
                    "n": stats["n"],
                }

    if suggestions:
        print(f"  (Based on trades with N >= 15, blended 70/30 observed/prior)")
        for (city, season), s in sorted(suggestions.items()):
            print(f"  {city} {season}: {s['current']} -> {s['suggested']} "
                  f"(observed={s['observed']}, N={s['n']})")
    else:
        print(f"  Not enough data yet (need 15+ trades per city/season).")
        by_group = defaultdict(int)
        for t in trades:
            m = CONTRACT_RE.search(t["ticker"])
            if m:
                city = m.group(2)
                season = _get_season_from_date(t["target_date"])
                by_group[(city, season)] += 1
        if by_group:
            print(f"  Current counts: {dict(by_group)}")

    print(f"\n{'=' * 70}\n")


def main():
    parser = argparse.ArgumentParser(description="Kalshi Model Calibration")
    parser.add_argument("--update", action="store_true",
                        help="Auto-update SD values in auto_trade.py and agent.py")
    args = parser.parse_args()

    trades = load_settled_trades()
    print_report(trades)

    if args.update:
        print("Auto-update not yet implemented (need sufficient data first).")
        print("Once we have 30+ trades per city/season, --update will rewrite the SD table.")


if __name__ == "__main__":
    main()
