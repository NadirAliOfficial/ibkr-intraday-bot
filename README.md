# IBKR Intraday Bot

Automated intraday trading bot for Interactive Brokers. Monitors US-listed stocks at market open, places conditional stop-buy orders based on the first 1-minute candle, manages an initial stop loss, and trails the stop below completed 5-minute candle lows.

---

## How It Works

1. Before market open, you add 3–15 tickers to `tickers.txt`
2. The bot connects to IBKR and watches for the first completed 1-minute candle (9:30–9:31 ET)
3. For each ticker, it places a **stop-market buy order** at the candle's high
4. It automatically sets a **stop-loss** at the candle's low
5. Position size is calculated from your account value and chosen risk percentage
6. If the buy order is not triggered within your set window (default 15 min), it is cancelled
7. Once a position is open, the bot **trails the stop** upward below each completed 5-minute candle low
8. The stop only moves up — never down

---

## Prerequisites

- Python 3.11 or higher
- Interactive Brokers account (paper or live, including ISK accounts)
- IBKR Trader Workstation (TWS) **or** IB Gateway installed and running
- API access enabled in TWS/Gateway settings

---

## Step-by-Step Setup

### 1. Install Python

Download from [python.org](https://python.org) and install. Make sure to check **"Add Python to PATH"** during installation.

Verify it works by opening a terminal (Command Prompt on Windows, Terminal on Mac) and running:

```
python --version
```

---

### 2. Download or Clone This Project

If you received a ZIP file, extract it to a folder such as `C:\ibkr-bot` (Windows) or `~/ibkr-bot` (Mac/Linux).

---

### 3. Install Dependencies

Open a terminal, navigate to the project folder, and run:

```
pip install -r requirements.txt
```

This installs `ib_insync`, the Python library for the IBKR API.

---

### 4. Enable API in TWS or IB Gateway

**In TWS:**
1. Open TWS and log in
2. Go to **Edit → Global Configuration → API → Settings**
3. Check **Enable ActiveX and Socket Clients**
4. Set **Socket port** (default `7497` for paper, `7496` for live)
5. Check **Allow connections from localhost only**
6. Click **Apply** and **OK**

**In IB Gateway:**
1. Open IB Gateway and log in
2. Go to **Configure → Settings → API**
3. Enable the API and note the port (`4002` for paper, `4001` for live)

---

### 5. Configure `config.json`

Open `config.json` in any text editor and adjust the values:

```json
{
    "host": "127.0.0.1",
    "port": 7497,
    "client_id": 1,
    "risk_percent": 0.5,
    "cancel_after_minutes": 15,
    "account": ""
}
```

| Field | Description |
|---|---|
| `host` | Always `127.0.0.1` (localhost) |
| `port` | `7497` = TWS paper, `7496` = TWS live, `4002` = Gateway paper, `4001` = Gateway live |
| `client_id` | Any number (1 is fine unless another program also uses 1) |
| `risk_percent` | Percentage of account to risk per trade (e.g. `0.5` = 0.5%) |
| `cancel_after_minutes` | Minutes after market open to cancel unfilled buy orders |
| `account` | Leave empty for single accounts. Add your account ID if you have multiple accounts |

---

### 6. Add Your Tickers

Open `tickers.txt` and replace the example tickers with the ones you want to trade. One ticker per line, 3–15 tickers maximum.

```
AAPL
NVDA
META
```

Do this **before** starting the bot each morning.

---

### 7. Run the Bot

**Paper trading (recommended first):**

Make sure TWS or IB Gateway is open and logged into your **paper trading** account. Then run:

```
python bot.py
```

You will see live logs in the terminal and in `bot.log`.

**Live trading:**

Change the `port` in `config.json` to `7496` (TWS live) or `4001` (Gateway live), then log into your live account in TWS/Gateway and run the same command.

---

## Switching Between Paper and Live

The only change needed is the `port` in `config.json`:

| Mode | TWS Port | IB Gateway Port |
|---|---|---|
| Paper trading | `7497` | `4002` |
| Live trading | `7496` | `4001` |

No code changes required. The bot works with any account type including ISK accounts.

---

## Daily Workflow

1. Update `tickers.txt` with today's tickers (do this the night before or before 9:30 AM ET)
2. Open TWS or IB Gateway and log in
3. Run `python bot.py`
4. The bot waits for market open, places orders, manages stops automatically
5. Stop the bot at any time with `Ctrl+C`
6. The bot also shuts itself down automatically at 4:00 PM ET

---

## Position Sizing Formula

```
risk_amount   = account_value × (risk_percent / 100)
risk_per_share = entry_price - stop_price
shares        = floor(risk_amount / risk_per_share)
```

**Example:** Account = $50,000, risk = 0.5%, entry = $150.00, stop = $148.50  
`risk_amount = $250` | `risk_per_share = $1.50` | `shares = 166`

---

## Log File

All activity is saved to `bot.log` in the project folder. Check this file to review what the bot did during the session — entries, stops, cancellations, trailing stop updates.

---

## Troubleshooting

**"Connection failed"**  
- Make sure TWS or IB Gateway is open and API is enabled
- Check that the port in `config.json` matches the one in TWS/Gateway
- Try a different `client_id` (e.g. `2`) if another application is already connected

**"Cannot read account value"**  
- You must be fully logged in, not on the 2FA screen
- For ISK accounts, the value may be in SEK — the bot will still use it for sizing

**"Failed to qualify contract"**  
- The ticker may be delisted, misspelled, or not available on SMART routing
- Double-check the ticker symbol on the IBKR website

**Bot places no orders**  
- Make sure you start the bot before 9:31 AM ET
- The bot only reads the 9:30 candle — if it starts after that candle completes and the cancellation window has passed, it skips that ticker for the day

---

## File Overview

```
ibkr-intraday-bot/
├── bot.py            Main bot logic
├── config.json       Settings (port, risk %, cancel window)
├── tickers.txt       Tickers to trade (update each morning)
├── requirements.txt  Python dependencies
├── bot.log           Generated at runtime — activity log
└── README.md         This guide
```

---

## Strategy Summary

| Step | Detail |
|---|---|
| Candle used for entry | First completed 1-minute candle (9:30–9:31 ET) |
| Entry trigger | Stop-market buy at the candle's high |
| Initial stop loss | Candle's low |
| Order cancellation | After `cancel_after_minutes` if not triggered |
| Trailing stop | Trails below completed 5-minute candle lows |
| Stop direction | Only moves up, never down |
| Position sizing | Account value × risk % ÷ (entry − stop) |
<!-- updated: 2026-05-29 -->

