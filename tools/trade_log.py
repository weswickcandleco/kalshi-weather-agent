import os
import json
import sqlite3
import datetime
from zoneinfo import ZoneInfo

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "trades.db")
CST = ZoneInfo("America/Chicago")


def _get_db():
    """Get a connection to the trade log database, creating tables if needed."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            mode TEXT NOT NULL,
            target_date TEXT NOT NULL,
            city TEXT NOT NULL,
            ticker TEXT NOT NULL,
            title TEXT,
            side TEXT NOT NULL,
            yes_price_cents INTEGER NOT NULL,
            cost_cents INTEGER NOT NULL,
            contracts INTEGER NOT NULL,
            potential_profit_cents INTEGER NOT NULL,
            forecast_high_f REAL,
            forecast_low_f REAL,
            est_probability REAL,
            expected_value_cents REAL,
            filled INTEGER DEFAULT 0,
            order_id TEXT,
            dry_run INTEGER DEFAULT 0,
            settlement_result TEXT DEFAULT 'pending',
            payout_cents INTEGER DEFAULT 0
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            mode TEXT NOT NULL,
            target_date TEXT NOT NULL,
            cities TEXT NOT NULL,
            trades_placed INTEGER DEFAULT 0,
            trades_skipped INTEGER DEFAULT 0,
            total_cost_cents INTEGER DEFAULT 0,
            balance_before_cents INTEGER,
            balance_after_cents INTEGER
        )
    """)
    db.commit()
    return db


def log_run(mode, target_date, cities, trades_placed, trades_skipped,
            total_cost_cents, balance_before_cents=None, balance_after_cents=None):
    """Log an agent run."""
    db = _get_db()
    now = datetime.datetime.now(CST).isoformat()
    db.execute(
        """INSERT INTO runs
           (timestamp, mode, target_date, cities, trades_placed, trades_skipped,
            total_cost_cents, balance_before_cents, balance_after_cents)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (now, mode, target_date, ",".join(cities), trades_placed, trades_skipped,
         total_cost_cents, balance_before_cents, balance_after_cents),
    )
    db.commit()
    db.close()


def log_trade(mode, target_date, city, ticker, title, side, yes_price_cents,
              contracts, forecast_high_f=None, forecast_low_f=None,
              est_probability=None, expected_value_cents=None,
              filled=False, order_id=None, dry_run=False):
    """Log a single trade to the database."""
    db = _get_db()
    now = datetime.datetime.now(CST).isoformat()

    if side == "yes":
        cost_cents = yes_price_cents * contracts
    else:
        cost_cents = (100 - yes_price_cents) * contracts
    potential_profit_cents = (100 * contracts) - cost_cents

    db.execute(
        """INSERT INTO trades
           (timestamp, mode, target_date, city, ticker, title, side,
            yes_price_cents, cost_cents, contracts, potential_profit_cents,
            forecast_high_f, forecast_low_f, est_probability,
            expected_value_cents, filled, order_id, dry_run)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (now, mode, target_date, city, ticker, title, side,
         yes_price_cents, cost_cents, contracts, potential_profit_cents,
         forecast_high_f, forecast_low_f, est_probability,
         expected_value_cents, int(filled), order_id, int(dry_run)),
    )
    db.commit()
    db.close()


def get_trade_history(limit=50, mode=None):
    """Get recent trade history."""
    db = _get_db()
    query = "SELECT * FROM trades ORDER BY timestamp DESC"
    params = []
    if mode:
        query = "SELECT * FROM trades WHERE mode = ? ORDER BY timestamp DESC"
        params = [mode]
    query += f" LIMIT {limit}"
    rows = db.execute(query, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_run_history(limit=20):
    """Get recent run history."""
    db = _get_db()
    rows = db.execute(
        "SELECT * FROM runs ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_pnl_summary(mode=None):
    """Get profit/loss summary."""
    db = _get_db()
    where = "WHERE dry_run = 0"
    params = []
    if mode:
        where += " AND mode = ?"
        params.append(mode)

    stats = db.execute(f"""
        SELECT
            COUNT(*) as total_trades,
            SUM(CASE WHEN filled = 1 THEN 1 ELSE 0 END) as filled_trades,
            SUM(CASE WHEN filled = 1 THEN cost_cents ELSE 0 END) as total_cost,
            SUM(CASE WHEN settlement_result = 'win' THEN 100 * contracts ELSE 0 END) as total_payout,
            SUM(CASE WHEN settlement_result = 'win' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN settlement_result = 'loss' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN settlement_result = 'pending' AND filled = 1 THEN 1 ELSE 0 END) as pending
        FROM trades {where}
    """, params).fetchone()
    db.close()

    result = dict(stats)
    total_cost = result["total_cost"] or 0
    total_payout = result["total_payout"] or 0
    result["net_pnl_cents"] = total_payout - total_cost
    result["net_pnl_dollars"] = result["net_pnl_cents"] / 100
    result["total_cost_dollars"] = total_cost / 100
    result["total_payout_dollars"] = total_payout / 100
    win_rate_denom = (result["wins"] or 0) + (result["losses"] or 0)
    result["win_rate"] = (result["wins"] or 0) / win_rate_denom if win_rate_denom > 0 else None
    return result


def update_settlement(ticker, target_date, result, payout_cents=0):
    """Update settlement result for trades on a given ticker and date."""
    db = _get_db()
    db.execute(
        """UPDATE trades SET settlement_result = ?, payout_cents = ?
           WHERE ticker = ? AND target_date = ? AND settlement_result = 'pending'""",
        (result, payout_cents, ticker, target_date),
    )
    db.commit()
    db.close()


def print_history(limit=30, mode=None):
    """Print formatted trade history to stdout."""
    trades = get_trade_history(limit=limit, mode=mode)
    if not trades:
        print("No trades logged yet.")
        return

    print(f"\n{'=' * 90}")
    print(f"  TRADE HISTORY (last {len(trades)} trades)")
    print(f"{'=' * 90}")
    print(f"{'Date':<12} {'Mode':<8} {'Ticker':<28} {'Side':<5} {'Cost':>6} {'Profit':>7} {'Result':>8}")
    print(f"{'-' * 12} {'-' * 8} {'-' * 28} {'-' * 5} {'-' * 6} {'-' * 7} {'-' * 8}")

    for t in reversed(trades):
        date = t["target_date"]
        mode_str = t["mode"]
        if t["dry_run"]:
            mode_str = "DRY"
        ticker = t["ticker"]
        if len(ticker) > 27:
            ticker = ticker[:24] + "..."
        side = t["side"].upper()
        cost = f"${t['cost_cents'] / 100:.2f}"
        profit = f"${t['potential_profit_cents'] / 100:.2f}"
        result = t["settlement_result"].upper()
        if not t["filled"]:
            result = "NOFILL"
        print(f"{date:<12} {mode_str:<8} {ticker:<28} {side:<5} {cost:>6} {profit:>7} {result:>8}")

    # P&L summary
    pnl = get_pnl_summary(mode=mode)
    print(f"\n{'─' * 50}")
    print(f"  Filled: {pnl['filled_trades']}  |  "
          f"Wins: {pnl['wins'] or 0}  |  "
          f"Losses: {pnl['losses'] or 0}  |  "
          f"Pending: {pnl['pending'] or 0}")
    if pnl["win_rate"] is not None:
        print(f"  Win Rate: {pnl['win_rate']:.0%}")
    print(f"  Total Cost: ${pnl['total_cost_dollars']:.2f}  |  "
          f"Total Payout: ${pnl['total_payout_dollars']:.2f}  |  "
          f"Net P&L: ${pnl['net_pnl_dollars']:.2f}")
    print(f"{'=' * 90}\n")
