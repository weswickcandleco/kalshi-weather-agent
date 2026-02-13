import json
import uuid
from .kalshi_auth import kalshi_get, kalshi_post
from config import MAX_BET_DOLLARS, MAX_CONTRACTS_PER_ORDER

# Price guardrails -- the agent cannot override these
MIN_PRICE_CENTS = 15  # never buy YES below 15c (longshot garbage)
MAX_PRICE_CENTS = 85  # never buy YES above 85c (paying 85c+ to win 15c = bad risk/reward)


def tool_get_account_balance(pk, api_key_id, base_url):
    """Get current Kalshi account balance."""
    try:
        r = kalshi_get(pk, api_key_id, base_url, "/trade-api/v2/portfolio/balance")
        if r.status_code == 200:
            data = r.json()
            return json.dumps({
                "balance_cents": data["balance"],
                "balance_dollars": data["balance"] / 100,
                "portfolio_value_cents": data.get("portfolio_value", 0),
                "portfolio_value_dollars": data.get("portfolio_value", 0) / 100,
            })
        return json.dumps({"error": f"HTTP {r.status_code}", "body": r.text[:500]})
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_place_order(pk, api_key_id, base_url, dry_run, ticker, side, yes_price_cents, contracts):
    """Place a Kalshi limit order with built-in risk enforcement."""
    # --- Risk enforcement (not overridable by the agent) ---
    contracts = min(contracts, MAX_CONTRACTS_PER_ORDER)
    if contracts < 1:
        contracts = 1
    if not (1 <= yes_price_cents <= 99):
        return json.dumps({"error": f"Price {yes_price_cents}c out of range (must be 1-99)"})

    # Calculate the ACTUAL cost to the buyer
    if side == "yes":
        cost_per_contract = yes_price_cents
    else:  # side == "no"
        cost_per_contract = 100 - yes_price_cents

    # PRICE GUARDRAILS: reject bad risk/reward bets
    if cost_per_contract > MAX_PRICE_CENTS:
        profit_if_win = 100 - cost_per_contract
        return json.dumps({
            "error": f"REJECTED: Cost {cost_per_contract}c per contract is too high "
                     f"(max {MAX_PRICE_CENTS}c). You'd risk {cost_per_contract}c to win "
                     f"only {profit_if_win}c. Find a better-priced contract.",
            "suggestion": "Look for contracts priced 20-70c where the risk/reward ratio is reasonable."
        })
    if cost_per_contract < MIN_PRICE_CENTS:
        return json.dumps({
            "error": f"REJECTED: Cost {cost_per_contract}c per contract is too low "
                     f"(min {MIN_PRICE_CENTS}c). Longshot bets under {MIN_PRICE_CENTS}c rarely hit. "
                     f"Find a contract closer to the forecast threshold.",
            "suggestion": "Look for contracts where the NWS forecast is near the threshold, priced 20-70c."
        })

    # Max dollar enforcement
    cost_dollars = cost_per_contract * contracts / 100
    if cost_dollars > MAX_BET_DOLLARS:
        contracts = int(MAX_BET_DOLLARS * 100 / cost_per_contract)
        if contracts < 1:
            return json.dumps({"error": f"Even 1 contract costs ${cost_per_contract/100:.2f} which exceeds ${MAX_BET_DOLLARS} limit"})
        cost_dollars = cost_per_contract * contracts / 100

    profit_if_win = (100 - cost_per_contract) * contracts / 100

    order = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "action": "buy",
        "side": side,
        "count": contracts,
        "type": "limit",
        "yes_price": yes_price_cents,
    }

    if dry_run:
        return json.dumps(
            {
                "dry_run": True,
                "would_place": order,
                "cost_dollars": cost_dollars,
                "profit_if_win_dollars": profit_if_win,
                "risk_reward": f"risk ${cost_dollars:.2f} to win ${profit_if_win:.2f}",
            }
        )

    try:
        r = kalshi_post(pk, api_key_id, base_url, "/trade-api/v2/portfolio/orders", order)
        resp = r.json()
        resp["_cost_dollars"] = cost_dollars
        resp["_profit_if_win"] = profit_if_win
        resp["_risk_reward"] = f"risk ${cost_dollars:.2f} to win ${profit_if_win:.2f}"
        return json.dumps({"http_status": r.status_code, "response": resp})
    except Exception as e:
        return json.dumps({"error": str(e)})


# Tool definitions for the Claude API
TRADING_TOOL_DEFINITIONS = [
    {
        "name": "get_account_balance",
        "description": "Check the current Kalshi account balance and portfolio value.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "place_order",
        "description": (
            "Place a YES or NO limit order on a Kalshi market. Each contract pays $1 if "
            "correct, $0 if wrong. Your cost is the price you pay. "
            "ENFORCED LIMITS: contract cost must be 15-85 cents (no longshots, no "
            "overpaying). Max $5 per bet, max 5 contracts. "
            "IMPORTANT: The response includes risk/reward info so you can verify the bet makes sense."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Market ticker"},
                "side": {
                    "type": "string",
                    "enum": ["yes", "no"],
                    "description": "'yes' bets the outcome happens, 'no' bets it doesn't",
                },
                "yes_price_cents": {
                    "type": "integer",
                    "description": "The yes_price for the order (1-99). If side='yes', this is your cost. If side='no', your cost is 100 minus this.",
                },
                "contracts": {
                    "type": "integer",
                    "description": "Number of contracts (max 5)",
                },
                "est_probability": {
                    "type": "number",
                    "description": "Your estimated probability of winning (0.0 to 1.0). Used for calibration tracking.",
                },
            },
            "required": ["ticker", "side", "yes_price_cents", "contracts", "est_probability"],
        },
    },
]
