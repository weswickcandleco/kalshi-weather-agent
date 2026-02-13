"""
Discord webhook notifications for the Kalshi Weather Agent.

Uses a simple webhook POST (no bot library needed).
Set DISCORD_WEBHOOK_URL in your .env file.
"""

import os
import datetime
import requests
from zoneinfo import ZoneInfo

CST = ZoneInfo("America/Chicago")

WEBHOOK_URL = None  # Loaded lazily from env


def _get_webhook_url():
    """Load webhook URL from environment (lazy, so .env can be loaded first)."""
    global WEBHOOK_URL
    if WEBHOOK_URL is None:
        WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
    return WEBHOOK_URL


def send_discord(content):
    """Send a plain text message to the Discord webhook."""
    url = _get_webhook_url()
    if not url:
        print("[NOTIFY] No DISCORD_WEBHOOK_URL set, skipping notification.")
        return False
    try:
        r = requests.post(url, json={"content": content}, timeout=10)
        if r.status_code in (200, 204):
            return True
        print(f"[NOTIFY] Discord returned HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[NOTIFY] Discord webhook failed: {e}")
        return False


def notify_bets_placed(trades, mode, target_date, token_stats=None):
    """Notify Discord about bets placed during an agent run.

    Args:
        trades: list of trade dicts from get_trade_history()
        mode: "LIVE", "DEMO", or "DRY RUN"
        target_date: ISO date string
        token_stats: optional dict with input_tokens, output_tokens, cost_estimate
    """
    if not trades:
        return

    now = datetime.datetime.now(CST)
    total_cost = sum(t["cost_cents"] for t in trades) / 100
    count = len(trades)

    lines = [
        f"**KALSHI AGENT -- Bets Placed**",
        f"Target: {target_date} | Cost: ${total_cost:.2f} ({count} bet{'s' if count != 1 else ''})",
        "",
    ]

    for t in trades:
        ticker = t["ticker"]
        side = t["side"].upper()
        cost = t["cost_cents"] / 100
        profit = t["potential_profit_cents"] / 100
        city = t.get("city", "")
        lines.append(f"{city} `{ticker}` -- {side} @ {t['yes_price_cents']}c -- cost ${cost:.2f} / win ${profit:.2f}")

    lines.append("")
    if token_stats:
        tok_in = token_stats.get("input_tokens", 0)
        tok_out = token_stats.get("output_tokens", 0)
        api_cost = token_stats.get("cost_estimate", 0)
        lines.append(f"API: {tok_in:,} in / {tok_out:,} out | ~${api_cost:.4f}")
    lines.append(f"Mode: {mode} | {now.strftime('%I:%M %p CT')}")

    send_discord("\n".join(lines))


def notify_settlements(results, target_date):
    """Notify Discord about settlement results.

    Args:
        results: list of dicts with keys: ticker, city, side, cost_cents, contracts,
                 observed_temp, result ('win'/'loss'), payout_cents, net_cents
        target_date: ISO date string
    """
    if not results:
        return

    wins = sum(1 for r in results if r["result"] == "win")
    losses = sum(1 for r in results if r["result"] == "loss")
    net = sum(r["net_cents"] for r in results) / 100

    sign = "+" if net >= 0 else ""
    lines = [
        f"**KALSHI AGENT -- Settlement Results**",
        f"Date: {target_date} | {wins}W-{losses}L | Net: {sign}${net:.2f}",
        "",
    ]

    for r in results:
        status = "WIN" if r["result"] == "win" else "LOSS"
        net_trade = r["net_cents"] / 100
        sign_t = "+" if net_trade >= 0 else ""
        obs = f"Observed: {r['observed_temp']}F" if r.get("observed_temp") is not None else ""
        lines.append(f"{'**WIN**' if r['result'] == 'win' else 'LOSS'}: `{r['ticker']}` | {obs} | {sign_t}${net_trade:.2f}")

    # Add all-time summary
    try:
        from .trade_log import get_pnl_summary
        pnl = get_pnl_summary()
        total_w = pnl["wins"] or 0
        total_l = pnl["losses"] or 0
        total_net = pnl["net_pnl_dollars"]
        total_sign = "+" if total_net >= 0 else ""
        lines.append("")
        lines.append(f"All-time: {total_w}W-{total_l}L | {total_sign}${total_net:.2f}")
    except Exception:
        pass

    send_discord("\n".join(lines))


def notify_error(script, message):
    """Notify Discord about an error.

    Args:
        script: name of the script that errored (e.g. 'settle.py', 'agent.py')
        message: error description
    """
    now = datetime.datetime.now(CST)
    lines = [
        f"**KALSHI AGENT -- Error**",
        f"Script: {script} | {now.strftime('%I:%M %p CT')}",
        f"```{message[:1500]}```",
    ]
    send_discord("\n".join(lines))


def notify_pnl_summary():
    """Post an overall P&L summary to Discord."""
    try:
        from .trade_log import get_pnl_summary
        pnl = get_pnl_summary()

        filled = pnl["filled_trades"] or 0
        wins = pnl["wins"] or 0
        losses = pnl["losses"] or 0
        pending = pnl["pending"] or 0
        net = pnl["net_pnl_dollars"]
        sign = "+" if net >= 0 else ""
        wr = f"{pnl['win_rate']:.0%}" if pnl["win_rate"] is not None else "N/A"

        lines = [
            f"**KALSHI AGENT -- P&L Summary**",
            f"Filled: {filled} | Wins: {wins} | Losses: {losses} | Pending: {pending}",
            f"Win Rate: {wr}",
            f"Total Cost: ${pnl['total_cost_dollars']:.2f} | Payout: ${pnl['total_payout_dollars']:.2f} | Net: {sign}${net:.2f}",
        ]
        send_discord("\n".join(lines))
    except Exception as e:
        notify_error("notify.py", f"P&L summary failed: {e}")
