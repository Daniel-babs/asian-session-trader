# 🕐 Asian Session Alert Trader

Monitors the Asian session high/low on any Forex pair, sends a Telegram alert
when that level is breached, then sends a second **trade confirmation** alert
when a 5-minute or 15-minute candle closes back beyond the breach candle
(within the Pre-London kill zone).

---

## How It Works

| Step | What happens | Alert sent? |
|------|-------------|-------------|
| 01:00–05:00 WAT | System builds the Asian session High & Low from 15-min candles | No |
| After 05:00 WAT | Monitors for price to take/breach that High or Low on 15M | ✅ **"Session High/Low Breached"** |
| Pre-London window | Watches for a 5M or 15M candle to **close beyond** the breach candle | ✅ **"Trade Confirmation — BUY/SELL"** |

### DST (Daylight Saving Time)
| DST State | London open (WAT) | Confirmation window (WAT) |
|-----------|-------------------|--------------------------|
| **ON** (UK BST, Mar–Oct) | 07:00 | **05:00 – 06:59** |
| **OFF** (UK GMT, Nov–Feb) | 08:00 | **06:00 – 07:59** |

Toggle the checkbox in the UI each time the UK clocks change.

---

## Setup

### 1. Install Python dependencies
```bash
pip install -r requirements.txt
```

### 2. Get a Telegram Bot Token
1. Open Telegram → search for **@BotFather**
2. Send `/newbot` and follow prompts
3. Copy the token (format: `1234567890:ABCdef...`)

### 3. Get your Chat ID
1. Start a conversation with your bot
2. Visit: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Send a message to the bot, then refresh that URL
4. Find `"chat":{"id": -100XXXXXXX}` — that's your Chat ID

### 4. Run the script
```bash
python main.py
```
Open your browser at **http://localhost:5001**

---

## Daily Workflow

1. Open the dashboard before or during the Asian session
2. Select the 1–2 pairs you want to trade today from the dropdown
3. Check/uncheck the **DST toggle** based on current UK clock status
4. Hit **▶ Start Monitoring**
5. Go about your morning — alerts arrive on Telegram automatically

---

## Alert Message Examples

**Breach alert:**
```
🔴 SESSION HIGH BREACHED
Asset: GBP/USD
Asian High: 1.27450
Breach candle high: 1.27512
Time (WAT): 05:34
⚠️ Watching for SELL confirmation…
```

**Confirmation alert:**
```
🔴 TRADE CONFIRMATION — SELL
Asset: GBP/USD
Signal timeframe: 5M
Asian High swept: 1.27450
Breach candle: H=1.27512  L=1.27391
Time (WAT): 05:48
🎯 Look for SELL entry
```

---

## Notes
- The system polls every **60 seconds** — fine for 5M/15M timeframes
- Asian session data resets automatically at **00:00 WAT** each day
- Select up to as many pairs as you want; typically 1–2 per session works best
- Data source: **Deriv WebSocket API** (no API key needed for price data)
