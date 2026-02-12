# Kalshi Weather Prediction Market Agent

Autonomous AI agent that bets on Chicago Midway Airport (KMDW) daily temperature markets on Kalshi. Uses Claude to research NWS weather forecasts, find matching markets, analyze pricing edge, and place bets -- all autonomously.

## How It Works

```
Start
  |-> Fetch NWS hourly forecast for target date (KMDW)
  |-> Check current observed conditions
  |-> Identify predicted HIGH and LOW temps + timing
  |-> Verify temps haven't already occurred (timing safety)
  |-> Search Kalshi for matching temperature markets
  |-> Analyze orderbooks for pricing edge
  |-> Check account balance
  |-> Place bets where edge exists (or simulate in dry-run)
  |-> Print full reasoning + summary
End
```

The agent uses Claude (Sonnet) in a tool-use loop. Claude autonomously decides which tools to call, reasons about the data, and makes betting decisions. Risk limits are enforced in code -- Claude cannot override the $5/bet cap.

## Prerequisites

- Python 3.10+
- An Anthropic API key (from [console.anthropic.com](https://console.anthropic.com))
- A Kalshi account with API keys (see setup below)

## Kalshi Account Setup

1. **Create an account** at [kalshi.com](https://kalshi.com) (production) or [demo.kalshi.com](https://demo.kalshi.com) (testing)
2. **Complete identity verification** (KYC) -- required for trading on production
3. **Generate API keys:**
   - Log in to Kalshi
   - Go to **Profile Settings** -> **API Keys**
   - Click **Create New API Key**
   - **Save the Key ID** (a UUID like `a952bcbe-ec3b-4b5b-b8f9-11dae589608c`)
   - **Download the private key** PEM file immediately -- it cannot be retrieved again
4. **Save the private key** as `kalshi-private-key.pem` in this project directory
5. **Fund your account** with a small amount ($10-25 to start) if using production

**Recommendation:** Start with a demo account at [demo.kalshi.com](https://demo.kalshi.com) to test everything with virtual funds before using real money.

## Project Setup

```bash
cd ~/Work/active-projects/kalshi-weather-agent

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set up credentials
cp .env.example .env
# Edit .env with your actual API keys and private key path
```

### .env Configuration

```
ANTHROPIC_API_KEY=sk-ant-your-key-here
KALSHI_API_KEY_ID=your-kalshi-key-id
KALSHI_PRIVATE_KEY_PATH=./kalshi-private-key.pem
```

## Usage

```bash
# Dry run (default) -- full analysis, simulated orders, no real money
python3 agent.py

# Demo account -- places real orders on demo.kalshi.co
python3 agent.py --demo

# Live -- places real orders with real money (use with caution)
python3 agent.py --live

# Target a specific date
python3 agent.py --date 2026-02-15

# Combine flags
python3 agent.py --demo --date 2026-02-15
```

## Risk Management

These limits are enforced in code and cannot be overridden by the AI agent:

| Parameter | Value |
|-----------|-------|
| Max bet per position | $5.00 |
| Max contracts per order | 5 |
| Min edge required | 5 cents |
| Price range | 1-99 cents |
| Dry-run default | Yes (no real orders unless --demo or --live) |

## Reading the Output

The agent prints its reasoning at each step:

- **Text blocks**: Claude's analysis and reasoning
- **[TOOL] calls**: Which tool was called and with what parameters
- **-> results**: Preview of the tool result data
- **Turn numbers**: How many reasoning steps the agent has taken

## Project Structure

```
kalshi-weather-agent/
  agent.py              # Entry point -- CLI, system prompt, agentic loop
  config.py             # Constants, risk params, API URLs
  requirements.txt      # Python dependencies
  .env.example          # Credential template
  .env                  # Your actual credentials (gitignored)
  .gitignore
  tools/
    __init__.py
    kalshi_auth.py       # RSA-PSS signing for Kalshi API
    nws.py               # NWS weather forecast tools
    kalshi_markets.py    # Market search and orderbook tools
    kalshi_trading.py    # Balance and order placement (with risk limits)
```

## Tools Available to the Agent

| Tool | Purpose |
|------|---------|
| `get_nws_forecast` | Hourly temp forecast from NWS API (KMDW gridpoint) |
| `get_current_conditions` | Live observed temp at KMDW station |
| `search_kalshi_markets` | Search open Kalshi markets by keywords |
| `get_orderbook` | Live bid/ask data for a market ticker |
| `get_account_balance` | Check available trading funds |
| `place_order` | Place a limit order (risk-capped internally) |

## Important Notes

- **NWS is the settlement source.** Kalshi settles weather markets using official NWS data. The agent uses the same data source for its predictions.
- **Start with dry-run mode.** Always run without `--demo` or `--live` first to see what the agent would do.
- **Weather markets may not always exist.** Kalshi doesn't always have KMDW temperature markets open. The agent will tell you if it can't find any.
- **This is not financial advice.** Use at your own risk. Most retail traders lose money.
