#!/usr/bin/env python3
"""
Asian Session Alert Trader
━━━━━━━━━━━━━━━━━━━━━━━━━
Strategy:
  1. Mark Asian session H/L (01:00–05:00 WAT)
  2. Alert when that high or low is breached/taken
  3. Alert when a 5m or 15m candle CLOSES beyond the breach candle
     (confirmation must fall within the Pre-London window)

DST toggle:
  ON  (UK BST, Mar–Oct): Confirmation window = 05:00–06:59 WAT
  OFF (UK GMT, Nov–Mar): Confirmation window = 06:00–07:59 WAT
"""

import asyncio
import json
import threading
import time
import requests
import websockets
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template_string, request as flask_request, jsonify

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

DERIV_APP_ID = "1089"
DERIV_WS_URL  = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
WAT           = timezone(timedelta(hours=1))   # Nigeria = UTC+1, always

SYMBOL_MAP = {
    "AUD/USD": "frxAUDUSD",
    "GBP/USD": "frxGBPUSD",
    "EUR/USD": "frxEURUSD",
    "EUR/GBP": "frxEURGBP",
    "USD/JPY": "frxUSDJPY",
    "GBP/JPY": "frxGBPJPY",
    "NZD/USD": "frxNZDUSD",
    "USD/CAD": "frxUSDCAD",
    "GBP/AUD": "frxGBPAUD",
    "GBP/NZD": "frxGBPNZD",
    "EUR/JPY": "frxEURJPY",
    "USD/CHF": "frxUSDCHF",
    "EUR/CAD": "frxEURCAD",
    "AUD/JPY": "frxAUDJPY",
}

# ─── GLOBAL STATE ─────────────────────────────────────────────────────────────

app_state = {
    "running":          False,
    "dst_on":           True,      # DST ON by default (currently BST)
    "selected_assets":  [],
    "bot_token":        "",
    "chat_id":          "",
    "logs":             [],
    "session_data":     {},
    "phase":            "Idle",
}

stop_event  = threading.Event()
state_lock  = threading.Lock()
monitor_thread = None

# ─── LOGGING ──────────────────────────────────────────────────────────────────

def log(msg, log_type="info"):
    wat_now = datetime.now(WAT)
    entry   = {"time": wat_now.strftime("%H:%M:%S"), "msg": msg, "type": log_type}
    with state_lock:
        app_state["logs"].append(entry)
        if len(app_state["logs"]) > 150:
            app_state["logs"] = app_state["logs"][-150:]
    print(f"[{entry['time']}] [{log_type.upper()}] {msg}")

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def send_telegram(bot_token, chat_id, message):
    try:
        url  = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        if resp.ok:
            log(f"Telegram sent OK: {message[:70]}…", "alert")
        else:
            log(f"Telegram HTTP error {resp.status_code}: {resp.text[:100]}", "error")
    except Exception as e:
        log(f"Telegram exception: {e}", "error")

# ─── DERIV API ────────────────────────────────────────────────────────────────

async def get_candles_async(symbol, granularity, count=100):
    """Fetch OHLC candles from Deriv WebSocket API."""
    try:
        async with websockets.connect(DERIV_WS_URL, ping_interval=None, close_timeout=10) as ws:
            req = {
                "ticks_history": symbol,
                "end":           "latest",
                "count":         count,
                "style":         "candles",
                "granularity":   granularity,
                "adjust_start_time": 1,
            }
            await ws.send(json.dumps(req))
            raw  = await asyncio.wait_for(ws.recv(), timeout=20)
            resp = json.loads(raw)
            if "error" in resp:
                log(f"Deriv error [{symbol}]: {resp['error'].get('message', resp['error'])}", "error")
                return []
            return resp.get("candles", [])
    except asyncio.TimeoutError:
        log(f"Deriv timeout for {symbol}", "error")
        return []
    except Exception as e:
        log(f"Deriv WS error [{symbol}]: {e}", "error")
        return []

# ─── SESSION WINDOW HELPERS ───────────────────────────────────────────────────

def get_confirmation_window(dst_on):
    """
    Returns (start_hour, end_hour_exclusive) in WAT.
    DST ON  (BST): 05:00–06:59 → (5, 7)
    DST OFF (GMT): 06:00–07:59 → (6, 8)
    """
    return (5, 7) if dst_on else (6, 8)

def in_asian_session(now_wat):
    return 1 <= now_wat.hour < 5

def past_asian_session(now_wat):
    return now_wat.hour >= 5

def in_confirmation_window(now_wat, dst_on):
    start, end = get_confirmation_window(dst_on)
    return start <= now_wat.hour < end

# ─── CORE CALCULATIONS ────────────────────────────────────────────────────────

async def fetch_asian_hl(deriv_symbol):
    """
    Compute Asian session H/L from today's 15m candles.
    Asian session = 01:00–05:00 WAT = 00:00–04:00 UTC
    """
    candles = await get_candles_async(deriv_symbol, 900, count=50)
    if not candles:
        return None, None

    today_utc        = datetime.now(timezone.utc).date()
    session_start_ts = int(datetime(today_utc.year, today_utc.month, today_utc.day,
                                    0, 0, 0, tzinfo=timezone.utc).timestamp())
    session_end_ts   = int(datetime(today_utc.year, today_utc.month, today_utc.day,
                                    4, 0, 0, tzinfo=timezone.utc).timestamp())

    highs = [float(c["high"]) for c in candles if session_start_ts <= c["epoch"] < session_end_ts]
    lows  = [float(c["low"])  for c in candles if session_start_ts <= c["epoch"] < session_end_ts]

    if not highs:
        return None, None

    return round(max(highs), 5), round(min(lows), 5)

async def get_latest_completed_candle(deriv_symbol, granularity):
    """Last fully-closed candle (second-to-last returned by API)."""
    candles = await get_candles_async(deriv_symbol, granularity, count=5)
    if len(candles) < 2:
        return None
    return candles[-2]   # -1 is still forming

async def get_recent_completed_candles(deriv_symbol, granularity, n=15):
    """Last n fully-closed candles."""
    candles = await get_candles_async(deriv_symbol, granularity, count=n + 3)
    if len(candles) < 2:
        return []
    return candles[:-1]  # drop forming candle

# ─── SESSION DATA ─────────────────────────────────────────────────────────────

def init_session_data(assets):
    return {
        asset: {
            "asian_high":         None,
            "asian_low":          None,
            "breach_type":        None,    # "HIGH" | "LOW"
            "breach_candle":      None,    # full candle dict from Deriv
            "breach_alert_sent":  False,
            "confirmed":          False,
            "confirmed_tf":       None,    # "5M" | "15M"
            "confirm_alert_sent": False,
        }
        for asset in assets
    }

# ─── MAIN MONITORING CYCLE ────────────────────────────────────────────────────

async def run_cycle(assets, dst_on, bot_token, chat_id):
    now_wat  = datetime.now(WAT)
    hour_wat = now_wat.hour
    conf_start, conf_end = get_confirmation_window(dst_on)

    # ── Phase label ──────────────────────────────────────────────────────────
    if hour_wat < 1 or hour_wat >= 22:
        phase = "⏳ Waiting for Asian Session (starts 01:00 WAT)"
    elif 1 <= hour_wat < 5:
        phase = "🌏 Asian Session active — building H/L (01:00–05:00 WAT)"
    elif 5 <= hour_wat < conf_start:
        phase = f"🔍 Monitoring for breach (confirmation opens {conf_start:02d}:00 WAT)"
    elif conf_start <= hour_wat < conf_end:
        phase = f"⚡ PRE-LONDON WINDOW — watching for confirmation ({conf_start:02d}:00–{conf_end-1:02d}:59 WAT)"
    else:
        phase = "🏁 Session complete for today"

    with state_lock:
        app_state["phase"] = phase

    # ── Midnight reset ───────────────────────────────────────────────────────
    if hour_wat == 0 and now_wat.minute < 2:
        with state_lock:
            app_state["session_data"] = init_session_data(assets)
        log("🔄 Midnight reset — session data cleared for new day", "info")
        return

    with state_lock:
        session_data = app_state["session_data"]

    for asset in assets:
        sym = SYMBOL_MAP.get(asset)
        if not sym:
            log(f"Unknown symbol for {asset}", "error")
            continue

        d = session_data.get(asset)
        if d is None:
            continue

        # ════════════════════════════════════════════════════════════════════
        # STEP 1 — Build / refresh Asian session H/L
        # ════════════════════════════════════════════════════════════════════
        if in_asian_session(now_wat) or (past_asian_session(now_wat) and d["asian_high"] is None):
            asian_high, asian_low = await fetch_asian_hl(sym)
            if asian_high is not None:
                with state_lock:
                    app_state["session_data"][asset]["asian_high"] = asian_high
                    app_state["session_data"][asset]["asian_low"]  = asian_low
                log(f"{asset} | Asian H/L updated → H:{asian_high}  L:{asian_low}", "info")
            else:
                log(f"{asset} | No Asian session candles yet", "info")

        # ════════════════════════════════════════════════════════════════════
        # STEP 2 — Watch for breach (after 05:00 WAT, before breach found)
        # ════════════════════════════════════════════════════════════════════
        if (past_asian_session(now_wat)
                and d["asian_high"] is not None
                and d["breach_type"] is None):

            c15 = await get_latest_completed_candle(sym, 900)
            if c15:
                c_high = float(c15["high"])
                c_low  = float(c15["low"])
                a_high = d["asian_high"]
                a_low  = d["asian_low"]

                if c_high > a_high:
                    breach = "HIGH"
                elif c_low < a_low:
                    breach = "LOW"
                else:
                    breach = None

                if breach:
                    with state_lock:
                        app_state["session_data"][asset]["breach_type"]   = breach
                        app_state["session_data"][asset]["breach_candle"] = c15

                    direction_hint = "SELL" if breach == "HIGH" else "BUY"
                    emoji = "🔴" if breach == "HIGH" else "🟢"
                    level = a_high if breach == "HIGH" else a_low
                    taken = c_high if breach == "HIGH" else c_low

                    log(f"{asset} | {emoji} Asian {breach} BREACHED | Level:{level}  Candle:{'H' if breach=='HIGH' else 'L'}={taken}", "alert")

                    if not d["breach_alert_sent"]:
                        msg = (
                            f"{emoji} <b>SESSION {'HIGH' if breach=='HIGH' else 'LOW'} BREACHED</b>\n"
                            f"Asset: <b>{asset}</b>\n"
                            f"Asian {'High' if breach=='HIGH' else 'Low'}: {level}\n"
                            f"Breach candle {'high' if breach=='HIGH' else 'low'}: {taken}\n"
                            f"Time (WAT): {now_wat.strftime('%H:%M')}\n\n"
                            f"⚠️ Watching for <b>{direction_hint}</b> confirmation…"
                        )
                        send_telegram(bot_token, chat_id, msg)
                        with state_lock:
                            app_state["session_data"][asset]["breach_alert_sent"] = True

        # ════════════════════════════════════════════════════════════════════
        # STEP 3 — Confirmation close (only within Pre-London window)
        #
        # SELL setup (Asian HIGH breached by a bullish candle):
        #   → Need a bearish candle to CLOSE BELOW THE LOW of that breach candle
        #   → Displacement back in the opposite (bearish) direction = FVG signal
        #
        # BUY setup (Asian LOW breached by a bearish candle):
        #   → Need a bullish candle to CLOSE ABOVE THE HIGH of that breach candle
        #   → Displacement back in the opposite (bullish) direction = FVG signal
        #
        # Check fires on whichever TF (5M or 15M) confirms first.
        # ════════════════════════════════════════════════════════════════════
        if (in_confirmation_window(now_wat, dst_on)
                and d["breach_type"] is not None
                and d["breach_candle"] is not None
                and not d["confirm_alert_sent"]):

            breach_type = d["breach_type"]
            bc          = d["breach_candle"]
            bc_high     = float(bc["high"])
            bc_low      = float(bc["low"])
            bc_epoch    = bc["epoch"]
            confirmed_tf = None

            # ── 15-min check ────────────────────────────────────────────────
            # SELL: bearish close below the LOW of the bullish breach candle
            # BUY:  bullish close above the HIGH of the bearish breach candle
            c15 = await get_latest_completed_candle(sym, 900)
            if c15 and c15["epoch"] > bc_epoch:
                c15_close = float(c15["close"])
                if breach_type == "HIGH" and c15_close < bc_low:
                    confirmed_tf = "15M"
                    log(f"{asset} | ✅ 15M SELL confirmed | Bearish close {c15_close} < Breach candle low {bc_low}", "confirm")
                elif breach_type == "LOW" and c15_close > bc_high:
                    confirmed_tf = "15M"
                    log(f"{asset} | ✅ 15M BUY confirmed | Bullish close {c15_close} > Breach candle high {bc_high}", "confirm")

            # ── 5-min check (if 15M not yet confirmed) ───────────────────
            # Same logic on the lower timeframe
            if not confirmed_tf:
                candles_5m = await get_recent_completed_candles(sym, 300, n=20)
                for c5 in reversed(candles_5m):
                    if c5["epoch"] <= bc_epoch:
                        continue   # only candles formed AFTER the breach candle
                    c5_close = float(c5["close"])
                    if breach_type == "HIGH" and c5_close < bc_low:
                        confirmed_tf = "5M"
                        log(f"{asset} | ✅ 5M SELL confirmed | Bearish close {c5_close} < Breach candle low {bc_low}", "confirm")
                        break
                    elif breach_type == "LOW" and c5_close > bc_high:
                        confirmed_tf = "5M"
                        log(f"{asset} | ✅ 5M BUY confirmed | Bullish close {c5_close} > Breach candle high {bc_high}", "confirm")
                        break

            # ── Fire confirmation alert ───────────────────────────────────
            if confirmed_tf:
                direction = "SELL" if breach_type == "HIGH" else "BUY"
                emoji     = "🔴" if direction == "SELL" else "🟢"
                swept_level = d["asian_high"] if breach_type == "HIGH" else d["asian_low"]

                msg = (
                    f"{emoji} <b>TRADE CONFIRMATION — {direction}</b>\n"
                    f"Asset: <b>{asset}</b>\n"
                    f"Signal timeframe: <b>{confirmed_tf}</b>\n"
                    f"Asian {'High' if breach_type=='HIGH' else 'Low'} swept: {swept_level}\n"
                    f"Breach candle: H={bc_high}  L={bc_low}\n"
                    f"Time (WAT): {now_wat.strftime('%H:%M')}\n\n"
                    f"🎯 <b>Look for {direction} entry</b>"
                )
                send_telegram(bot_token, chat_id, msg)
                with state_lock:
                    app_state["session_data"][asset]["confirmed"]          = True
                    app_state["session_data"][asset]["confirmed_tf"]       = confirmed_tf
                    app_state["session_data"][asset]["confirm_alert_sent"] = True

# ─── BACKGROUND THREAD ────────────────────────────────────────────────────────

def monitoring_loop(assets, dst_on, bot_token, chat_id):
    log(f"🚀 Monitoring started | Assets: {', '.join(assets)} | DST: {'ON (BST)' if dst_on else 'OFF (GMT)'}", "info")
    conf_start, conf_end = get_confirmation_window(dst_on)
    log(f"⏰ Confirmation window: {conf_start:02d}:00–{conf_end-1:02d}:59 WAT", "info")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        while not stop_event.is_set():
            try:
                loop.run_until_complete(run_cycle(assets, dst_on, bot_token, chat_id))
            except Exception as e:
                log(f"Cycle error: {e}", "error")
            stop_event.wait(timeout=60)   # check every 60 seconds
    finally:
        loop.close()
        log("⏹ Monitoring stopped", "info")

# ─── FLASK APP ────────────────────────────────────────────────────────────────

app = Flask(__name__)

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Asian Session Alert Trader</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg:       #0d1117;
    --surface:  #161b22;
    --border:   #30363d;
    --text:     #e6edf3;
    --muted:    #8b949e;
    --blue:     #58a6ff;
    --green:    #3fb950;
    --red:      #f85149;
    --yellow:   #e3b341;
  }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }

  /* ── Header ── */
  .header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; gap: 12px; }
  .header-icon { font-size: 1.4rem; }
  .header h1 { font-size: 1.15rem; color: var(--blue); }
  .header p  { font-size: 0.78rem; color: var(--muted); margin-top: 2px; }

  /* ── Layout ── */
  .container { max-width: 960px; margin: 0 auto; padding: 20px 16px; display: grid; gap: 14px; }

  /* ── Card ── */
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; }
  .card-title { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-bottom: 14px; font-weight: 600; }

  /* ── Config grid ── */
  .config-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  @media(max-width:600px){ .config-grid { grid-template-columns: 1fr; } }

  label { display: block; font-size: 0.8rem; color: var(--muted); margin-bottom: 5px; }
  input[type="text"], input[type="password"] {
    width: 100%; padding: 8px 11px; background: var(--bg); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-size: 0.875rem; outline: none; transition: border 0.15s;
  }
  input:focus { border-color: var(--blue); }

  select[multiple] {
    width: 100%; height: 190px; background: var(--bg); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-size: 0.875rem; padding: 4px;
    outline: none; transition: border 0.15s;
  }
  select[multiple]:focus { border-color: var(--blue); }
  select[multiple] option { padding: 5px 8px; border-radius: 4px; }
  select[multiple] option:checked { background: #1f6feb55; color: var(--blue); }

  /* ── DST toggle ── */
  .dst-row { display: flex; align-items: flex-start; gap: 10px; margin-top: 12px; }
  .toggle-wrap { position: relative; flex-shrink: 0; width: 42px; height: 22px; margin-top: 1px; }
  .toggle-wrap input { opacity: 0; width: 0; height: 0; }
  .slider { position: absolute; inset: 0; background: var(--border); border-radius: 22px; cursor: pointer; transition: 0.2s; }
  .slider::before { content:''; position: absolute; width: 16px; height: 16px; left: 3px; top: 3px; background: white; border-radius: 50%; transition: 0.2s; }
  input:checked + .slider { background: var(--blue); }
  input:checked + .slider::before { transform: translateX(20px); }
  .dst-text { font-size: 0.83rem; }
  .dst-text strong { color: var(--text); }
  .dst-text small  { display: block; color: var(--muted); font-size: 0.75rem; margin-top: 2px; }

  /* ── Window pill ── */
  .window-pill { margin-top: 10px; padding: 8px 12px; border-radius: 6px; font-size: 0.8rem; border: 1px solid; }
  .pill-bst { background: #1f6feb18; border-color: #1f6feb44; color: #79c0ff; }
  .pill-gmt { background: #e3b34118; border-color: #e3b34144; color: #e3b341; }

  /* ── Buttons ── */
  .btn-row { display: flex; align-items: center; gap: 10px; margin-top: 16px; flex-wrap: wrap; }
  .btn { padding: 9px 22px; border-radius: 6px; border: none; font-size: 0.875rem; font-weight: 600; cursor: pointer; transition: all 0.15s; }
  .btn-start { background: #238636; color: #fff; }
  .btn-start:hover:not(:disabled) { background: #2ea043; }
  .btn-stop  { background: #b62324; color: #fff; }
  .btn-stop:hover:not(:disabled)  { background: #da3633; }
  .btn:disabled { background: var(--border); color: var(--muted); cursor: not-allowed; }
  #statusText { font-size: 0.82rem; color: var(--muted); }

  /* ── Phase bar ── */
  .phase-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); flex-shrink: 0; }
  .dot.active { background: var(--green); box-shadow: 0 0 6px var(--green); animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.45} }
  #phaseLabel { font-size: 0.85rem; color: var(--muted); }
  #currentTime { margin-left: auto; font-family: monospace; font-size: 0.8rem; color: var(--muted); }

  /* ── Asset cards ── */
  .asset-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 10px; }
  .asset-card { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 13px; }
  .asset-name { font-weight: 700; color: var(--blue); margin-bottom: 10px; font-size: 0.95rem; }
  .data-row { display: flex; justify-content: space-between; font-size: 0.78rem; margin-bottom: 5px; }
  .data-label { color: var(--muted); }
  .data-value { font-family: monospace; color: var(--text); }
  .val-high { color: var(--red); }
  .val-low  { color: var(--green); }
  .val-confirm { color: var(--yellow); font-weight: 600; }
  .none-msg { font-size: 0.82rem; color: var(--muted); }

  /* ── Log ── */
  .log-box { background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
             padding: 10px 12px; height: 230px; overflow-y: auto; font-family: 'Cascadia Code', monospace; font-size: 0.76rem; line-height: 1.6; }
  .l-info    { color: var(--muted); }
  .l-alert   { color: var(--yellow); }
  .l-confirm { color: var(--green); }
  .l-error   { color: var(--red); }

  /* ── Scrollbar ── */
  ::-webkit-scrollbar { width: 6px; } ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>

<div class="header">
  <div class="header-icon">🕐</div>
  <div>
    <h1>Asian Session Alert Trader</h1>
    <p>Marks Asian H/L · Alerts on breach · Confirms Pre-London displacement</p>
  </div>
</div>

<div class="container">

  <!-- ── CONFIG ─────────────────────────────────────── -->
  <div class="card">
    <div class="card-title">⚙️ Configuration</div>
    <div class="config-grid">

      <!-- Left: Asset selector -->
      <div>
        <label>Assets to Monitor <span style="color:var(--muted);font-size:0.72rem">(Ctrl+Click or Cmd+Click for multiple)</span></label>
        <select id="assetSelect" multiple>
          <option value="AUD/USD">AUD/USD</option>
          <option value="GBP/USD">GBP/USD</option>
          <option value="EUR/USD">EUR/USD</option>
          <option value="EUR/GBP">EUR/GBP</option>
          <option value="USD/JPY">USD/JPY</option>
          <option value="GBP/JPY">GBP/JPY</option>
          <option value="NZD/USD">NZD/USD</option>
          <option value="USD/CAD">USD/CAD</option>
          <option value="GBP/AUD">GBP/AUD</option>
          <option value="GBP/NZD">GBP/NZD</option>
          <option value="EUR/JPY">EUR/JPY</option>
          <option value="USD/CHF">USD/CHF</option>
          <option value="EUR/CAD">EUR/CAD</option>
          <option value="AUD/JPY">AUD/JPY</option>
        </select>
      </div>

      <!-- Right: Telegram + DST -->
      <div>
        <div style="margin-bottom:10px">
          <label>Telegram Bot Token</label>
          <input type="password" id="botToken" placeholder="1234567890:ABCdef…">
        </div>
        <div style="margin-bottom:10px">
          <label>Telegram Chat ID</label>
          <input type="text" id="chatId" placeholder="-100123456789">
        </div>

        <!-- DST toggle -->
        <div class="dst-row">
          <label class="toggle-wrap">
            <input type="checkbox" id="dstToggle" checked onchange="updateWindow()">
            <span class="slider"></span>
          </label>
          <div class="dst-text">
            <strong id="dstLabel">UK Daylight Saving (BST) is ON</strong>
            <small id="dstSub">Currently active — clocks go back last Sunday of October</small>
          </div>
        </div>

        <div class="window-pill pill-bst" id="windowPill">
          🕔 Confirmation window: <strong>05:00 – 06:59 WAT</strong>
          &nbsp;·&nbsp; London opens 07:00 WAT
        </div>
      </div>
    </div>

    <div class="btn-row">
      <button class="btn btn-start" id="startBtn" onclick="startMonitor()">▶ Start Monitoring</button>
      <button class="btn btn-stop"  id="stopBtn"  onclick="stopMonitor()" disabled>⏹ Stop</button>
      <span id="statusText">Not running</span>
    </div>
  </div>

  <!-- ── SESSION STATUS ─────────────────────────────── -->
  <div class="card">
    <div class="card-title">📊 Session Status</div>
    <div class="phase-bar">
      <div class="dot" id="statusDot"></div>
      <span id="phaseLabel">Idle</span>
      <span id="currentTime"></span>
    </div>
    <div class="asset-grid" id="assetGrid">
      <p class="none-msg">No assets monitored yet. Start the system to see live data.</p>
    </div>
  </div>

  <!-- ── ACTIVITY LOG ───────────────────────────────── -->
  <div class="card">
    <div class="card-title">📋 Activity Log</div>
    <div class="log-box" id="logBox">
      <div class="l-info">Waiting to start…</div>
    </div>
  </div>

</div><!-- /container -->

<script>
// ── DST display update ───────────────────────────────────────────────────────
function updateWindow() {
  const on   = document.getElementById('dstToggle').checked;
  const pill = document.getElementById('windowPill');
  const lbl  = document.getElementById('dstLabel');
  const sub  = document.getElementById('dstSub');

  if (on) {
    lbl.textContent  = 'UK Daylight Saving (BST) is ON';
    sub.textContent  = 'Currently active — clocks go back last Sunday of October';
    pill.className   = 'window-pill pill-bst';
    pill.innerHTML   = '🕔 Confirmation window: <strong>05:00 – 06:59 WAT</strong> &nbsp;·&nbsp; London opens 07:00 WAT';
  } else {
    lbl.textContent  = 'UK Daylight Saving (GMT) is OFF';
    sub.textContent  = 'Winter hours — clocks go forward last Sunday of March';
    pill.className   = 'window-pill pill-gmt';
    pill.innerHTML   = '🕕 Confirmation window: <strong>06:00 – 07:59 WAT</strong> &nbsp;·&nbsp; London opens 08:00 WAT';
  }
}

// ── Start / Stop ─────────────────────────────────────────────────────────────
async function startMonitor() {
  const assets = Array.from(document.getElementById('assetSelect').selectedOptions).map(o => o.value);
  if (!assets.length) { alert('Please select at least one asset.'); return; }

  const botToken = document.getElementById('botToken').value.trim();
  const chatId   = document.getElementById('chatId').value.trim();
  if (!botToken || !chatId) { alert('Enter your Telegram Bot Token and Chat ID first.'); return; }

  const dst_on = document.getElementById('dstToggle').checked;

  const res  = await fetch('/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ assets, bot_token: botToken, chat_id: chatId, dst_on })
  });
  const data = await res.json();

  if (data.success) {
    document.getElementById('startBtn').disabled = true;
    document.getElementById('stopBtn').disabled  = false;
    document.getElementById('statusDot').classList.add('active');
    document.getElementById('statusText').textContent = 'Monitoring active…';
  } else {
    alert('Error: ' + (data.error || 'Unknown'));
  }
}

async function stopMonitor() {
  await fetch('/stop', { method: 'POST' });
  document.getElementById('startBtn').disabled = false;
  document.getElementById('stopBtn').disabled  = true;
  document.getElementById('statusDot').classList.remove('active');
  document.getElementById('statusText').textContent = 'Stopped';
}

// ── Render asset cards ───────────────────────────────────────────────────────
function renderAssets(session_data) {
  const grid   = document.getElementById('assetGrid');
  const assets = Object.keys(session_data);
  if (!assets.length) {
    grid.innerHTML = '<p class="none-msg">Waiting for first data cycle…</p>';
    return;
  }

  grid.innerHTML = assets.map(asset => {
    const d          = session_data[asset];
    const breachCls  = d.breach_type === 'HIGH' ? 'val-high' : d.breach_type === 'LOW' ? 'val-low' : '';
    const breachText = d.breach_type || '—';
    const confirmTxt = d.confirmed ? `✅ ${d.confirmed_tf}` : '—';
    const confirmCls = d.confirmed ? 'val-confirm' : '';

    return `
      <div class="asset-card">
        <div class="asset-name">${asset}</div>
        <div class="data-row">
          <span class="data-label">Asian High</span>
          <span class="data-value">${d.asian_high ?? '—'}</span>
        </div>
        <div class="data-row">
          <span class="data-label">Asian Low</span>
          <span class="data-value">${d.asian_low ?? '—'}</span>
        </div>
        <div class="data-row">
          <span class="data-label">Breach</span>
          <span class="data-value ${breachCls}">${breachText}</span>
        </div>
        <div class="data-row">
          <span class="data-label">Confirmation</span>
          <span class="data-value ${confirmCls}">${confirmTxt}</span>
        </div>
      </div>`;
  }).join('');
}

// ── Poll status ──────────────────────────────────────────────────────────────
async function poll() {
  try {
    const res  = await fetch('/status');
    const data = await res.json();

    document.getElementById('currentTime').textContent = data.current_time_wat;
    document.getElementById('phaseLabel').textContent  = data.phase || '—';

    renderAssets(data.session_data || {});

    const box = document.getElementById('logBox');
    box.innerHTML = (data.logs || []).map(l =>
      `<div class="l-${l.type}">[${l.time}] ${l.msg}</div>`
    ).join('');
    box.scrollTop = box.scrollHeight;
  } catch(e) { /* server may be restarting */ }

  setTimeout(poll, 8000);
}

poll();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/start", methods=["POST"])
def start():
    global monitor_thread
    if app_state["running"]:
        return jsonify({"success": False, "error": "Already running"})

    data      = flask_request.get_json()
    assets    = data.get("assets", [])
    bot_token = data.get("bot_token", "").strip()
    chat_id   = data.get("chat_id", "").strip()
    dst_on    = data.get("dst_on", True)

    if not assets:
        return jsonify({"success": False, "error": "No assets selected"})
    if not bot_token or not chat_id:
        return jsonify({"success": False, "error": "Missing Telegram credentials"})

    with state_lock:
        app_state.update({
            "running":         True,
            "dst_on":          dst_on,
            "selected_assets": assets,
            "bot_token":       bot_token,
            "chat_id":         chat_id,
            "logs":            [],
            "session_data":    init_session_data(assets),
        })

    stop_event.clear()
    monitor_thread = threading.Thread(
        target=monitoring_loop,
        args=(assets, dst_on, bot_token, chat_id),
        daemon=True
    )
    monitor_thread.start()
    return jsonify({"success": True})


@app.route("/stop", methods=["POST"])
def stop():
    stop_event.set()
    with state_lock:
        app_state["running"] = False
    return jsonify({"success": True})


@app.route("/status")
def status():
    with state_lock:
        now_wat = datetime.now(WAT)
        return jsonify({
            "running":         app_state["running"],
            "phase":           app_state["phase"],
            "current_time_wat": now_wat.strftime("%H:%M:%S WAT"),
            "session_data":    app_state["session_data"],
            "logs":            app_state["logs"][-60:],
        })


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5001))
    print("=" * 55)
    print("  Asian Session Alert Trader")
    print(f"  Running on port {port}")
    print("=" * 55)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
