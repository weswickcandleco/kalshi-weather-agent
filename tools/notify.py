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


def notify_bet_logic(trades, target_date):
    """Send detailed bet logic breakdown with probability calculations.

    Args:
        trades: list of trade dicts with ensemble data
        target_date: ISO date string
    """
    if not trades:
        return

    url = _get_webhook_url()
    if not url:
        return

    fields = []
    for i, t in enumerate(trades, 1):
        city = t.get("city", "?")
        ticker = t["ticker"]
        side = t["side"].upper()
        cost = t["cost_cents"] / 100
        contracts = t["contracts"]
        yes_price = t["yes_price_cents"]
        ev = t.get("expected_value_cents", 0)

        # Get ensemble stats
        ens_count = t.get("ensemble_member_count", 0)
        model_prob = t.get("est_probability", 0)

        # Determine high/low and mean/sd
        is_low = "LOWT" in ticker
        if is_low:
            ens_mean = t.get("ensemble_mean_low")
            ens_sd = t.get("ensemble_sd_low")
            temp_type = "Low"
        else:
            ens_mean = t.get("ensemble_mean_high")
            ens_sd = t.get("ensemble_sd_high")
            temp_type = "High"

        # Format ensemble stats
        ens_str = f"{ens_count} forecasts" if ens_count else "N/A"
        mean_str = f"{ens_mean:.1f}°F" if ens_mean else "?"
        sd_str = f"{ens_sd:.1f}°F" if ens_sd else "?"

        # Extract threshold from ticker
        import re
        match = re.search(r'[BT]([\d.]+)', ticker)
        threshold = match.group(1) if match else "?"

        # Determine what we're betting
        bracket_type = "B" if "B" in ticker.split("-")[-1][0] else "T"
        if side == "YES":
            if bracket_type == "T":
                bet_desc = f"{temp_type} ≥{threshold}°F (YES)"
            else:
                bet_desc = f"{temp_type} <{threshold}°F (YES)"
        else:
            if bracket_type == "T":
                bet_desc = f"{temp_type} <{threshold}°F (NO)"
            else:
                bet_desc = f"{temp_type} ≥{threshold}°F (NO)"

        # Build field value
        prob_pct = int(model_prob * 100) if model_prob else 0
        market_prob = yes_price if side == "YES" else (100 - yes_price)

        value_lines = [
            "```",
            f"Ensemble: {ens_str}",
            f"Mean: {mean_str} | SD: {sd_str}",
            "",
            f"P(win) = {prob_pct}%",
            f"Market price: {yes_price}¢ (implies {market_prob}%)",
            "",
            f"Edge: {prob_pct}% - {market_prob}% = +{ev:.0f}¢",
        ]

        # Add insight
        if ev >= 30:
            value_lines.append("→ Strong mispricing")
        elif ev >= 15:
            value_lines.append("→ Moderate edge")
        else:
            value_lines.append("→ Small edge, above threshold")

        value_lines.append("```")

        fields.append({
            "name": f"{i}️⃣ {city} - {bet_desc} | ${cost:.2f} @ {yes_price}¢ | +{ev:.0f}¢ EV",
            "value": "\n".join(value_lines)
        })

    # Add model details footer
    fields.append({
        "name": "📊 Model Details",
        "value": "Uses **normal distribution** with ensemble mean/SD to calculate P(condition true)\n\nBets when: `P(model) - P(market) > 5¢`"
    })

    embed = {
        "title": f"🎯 Bet Logic Breakdown - {target_date}",
        "description": f"Why these {len(trades)} bet{'s' if len(trades) != 1 else ''} were placed",
        "color": 3066993,  # green
        "fields": fields,
        "footer": {
            "text": "Cloudflare cron → GitHub Actions → Kalshi API"
        }
    }

    try:
        r = requests.post(url, json={"embeds": [embed]}, timeout=10)
        if r.status_code not in (200, 204):
            print(f"[NOTIFY] Bet logic Discord returned HTTP {r.status_code}")
    except Exception as e:
        print(f"[NOTIFY] Bet logic webhook failed: {e}")


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
