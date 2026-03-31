#!/usr/bin/env python3
"""
Session Alert Trader — Asian · London · New York AM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Per-asset session selection. Same strategy across all 3 sessions:
  1. Build the session High/Low during the session window
  2. Alert when that H/L is breached after the session closes
  3. Alert when a 5M or 15M candle closes in the opposite direction
     (confirmation must fall within the 2-hr window after session)

Session windows (all WAT = UTC+1, fixed):
  ┌─────────────┬──────────────────┬─────────────────────┐
  │ Session     │ Build window     │ Confirmation window │
  ├─────────────┼──────────────────┼─────────────────────┤
  │ Asian       │ 01:00 – 04:59    │ 05:00–06:59 (BST)   │
  │             │                  │ 06:00–07:59 (GMT)   │
  │ London      │ 07:00 – 09:59    │ 10:00 – 11:59       │
  │ New York AM │ 14:30 – 15:59    │ 16:00 – 17:59       │
  └─────────────┴──────────────────┴─────────────────────┘
"""

import asyncio
import json
import os
import threading
import requests
import websockets
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template_string, request as flask_request, jsonify

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

DERIV_APP_ID  = "1089"
DERIV_WS_URL  = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"
WAT           = timezone(timedelta(hours=1))

SYMBOL_MAP = {
    "AUD/USD": "frxAUDUSD",
    "AUD/NZD": "frxAUDNZD",
    "AUD/JPY": "frxAUDJPY",
    "AUD/CHF": "frxAUDCHF",
    "AUD/CAD": "frxAUDCAD",
    "EUR/USD": "frxEURUSD",
    "EUR/AUD": "frxEURAUD",
    "EUR/CAD": "frxEURCAD",
    "EUR/CHF": "frxEURCHF",
    "EUR/GBP": "frxEURGBP",
    "EUR/JPY": "frxEURJPY",
    "EUR/NZD": "frxEURNZD",
    "GBP/USD": "frxGBPUSD",
    "GBP/AUD": "frxGBPAUD",
    "GBP/CAD": "frxGBPCAD",
    "GBP/CHF": "frxGBPCHF",
    "GBP/JPY": "frxGBPJPY",
    "GBP/NZD": "frxGBPNZD",
    "NZD/USD": "frxNZDUSD",
    "NZD/CAD": "frxNZDCAD",
    "NZD/CHF": "frxNZDCHF",
    "NZD/JPY": "frxNZDJPY",
    "USD/CAD": "frxUSDCAD",
    "USD/JPY": "frxUSDJPY",
    "US SP 500": "OTC_SPX",
    "US Tech 100": "OTC_NDX",
}

# Session definitions — times in minutes from midnight WAT
# Asian confirm window is dynamic (DST-dependent), injected at runtime
SESSION_WINDOWS = {
    "Asian": {
        "build_start":   1 * 60,        # 01:00
        "build_end":     5 * 60,        # 05:00
    },
    "London": {
        "build_start":   7 * 60,        # 07:00
        "build_end":     10 * 60,       # 10:00
        "confirm_start": 10 * 60,       # 10:00
        "confirm_end":   12 * 60,       # 12:00
    },
    "New York AM": {
        "build_start":   14 * 60 + 30,  # 14:30
        "build_end":     16 * 60,       # 16:00
        "confirm_start": 16 * 60,       # 16:00
        "confirm_end":   18 * 60,       # 18:00
    },
}

# ─── GLOBAL STATE ─────────────────────────────────────────────────────────────

app_state = {
    "running":         False,
    "dst_on":          True,
    "asset_sessions":  {},   # { "GBP/USD": "London", "AUD/USD": "Asian" }
    "bot_token":       "",
    "chat_id":         "",
    "logs":            [],
    "session_data":    {},
    "phase":           "Idle",
}

stop_event     = threading.Event()
state_lock     = threading.Lock()
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
            log(f"Telegram sent: {message[:60]}...", "alert")
        else:
            log(f"Telegram error {resp.status_code}: {resp.text[:80]}", "error")
    except Exception as e:
        log(f"Telegram exception: {e}", "error")

# ─── DERIV API ────────────────────────────────────────────────────────────────

async def get_candles_async(symbol, granularity, count=100):
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
                log(f"Deriv error [{symbol}]: {resp['error'].get('message','')}", "error")
                return []
            return resp.get("candles", [])
    except asyncio.TimeoutError:
        log(f"Deriv timeout [{symbol}]", "error")
        return []
    except Exception as e:
        log(f"Deriv WS error [{symbol}]: {e}", "error")
        return []

# ─── SESSION WINDOW HELPERS ───────────────────────────────────────────────────

def minutes_now(now_wat):
    return now_wat.hour * 60 + now_wat.minute

def get_confirm_window(session_name, dst_on):
    """Return (confirm_start_min, confirm_end_min) for a session."""
    if session_name == "Asian":
        return (5 * 60, 7 * 60) if dst_on else (6 * 60, 8 * 60)
    win = SESSION_WINDOWS[session_name]
    return win["confirm_start"], win["confirm_end"]

def in_build_window(now_min, session_name):
    win = SESSION_WINDOWS[session_name]
    return win["build_start"] <= now_min < win["build_end"]

def past_build_window(now_min, session_name):
    win = SESSION_WINDOWS[session_name]
    return now_min >= win["build_end"]

def in_confirm_window(now_min, session_name, dst_on):
    cs, ce = get_confirm_window(session_name, dst_on)
    return cs <= now_min < ce

# ─── FETCH SESSION H/L ────────────────────────────────────────────────────────

async def fetch_session_hl(deriv_symbol, session_name):
    """Compute session H/L from today's 15m candles within the build window."""
    candles = await get_candles_async(deriv_symbol, 900, count=60)
    if not candles:
        return None, None

    win       = SESSION_WINDOWS[session_name]
    today_wat = datetime.now(WAT).date()

    # Midnight WAT in epoch seconds
    midnight_wat_epoch = int(
        datetime(today_wat.year, today_wat.month, today_wat.day,
                 0, 0, 0, tzinfo=WAT).timestamp()
    )

    build_start_epoch = midnight_wat_epoch + win["build_start"] * 60
    build_end_epoch   = midnight_wat_epoch + win["build_end"]   * 60

    highs = [float(c["high"]) for c in candles if build_start_epoch <= c["epoch"] < build_end_epoch]
    lows  = [float(c["low"])  for c in candles if build_start_epoch <= c["epoch"] < build_end_epoch]

    if not highs:
        return None, None

    return round(max(highs), 5), round(min(lows), 5)

async def get_latest_completed_candle(deriv_symbol, granularity):
    candles = await get_candles_async(deriv_symbol, granularity, count=5)
    if len(candles) < 2:
        return None
    return candles[-2]

async def get_recent_completed_candles(deriv_symbol, granularity, n=20):
    candles = await get_candles_async(deriv_symbol, granularity, count=n + 3)
    if len(candles) < 2:
        return []
    return candles[:-1]

# ─── SESSION DATA ─────────────────────────────────────────────────────────────

def empty_asset_state(session_name):
    return {
        "session":            session_name,
        "session_high":       None,
        "session_low":        None,
        "breach_type":        None,
        "breach_candle":      None,
        "breach_alert_sent":  False,
        "confirmed":          False,
        "confirmed_tf":       None,
        "confirm_alert_sent": False,
    }

def init_session_data(asset_sessions):
    return {asset: empty_asset_state(sess) for asset, sess in asset_sessions.items()}

# ─── PHASE LABEL ─────────────────────────────────────────────────────────────

def compute_phase(now_min, dst_on, asset_sessions):
    active = []
    for asset, sess in asset_sessions.items():
        win    = SESSION_WINDOWS[sess]
        cs, ce = get_confirm_window(sess, dst_on)
        if in_build_window(now_min, sess):
            active.append(f"🌐 {sess} building ({asset})")
        elif win["build_end"] <= now_min < cs:
            active.append(f"🔍 Watching for {sess} breach ({asset})")
        elif cs <= now_min < ce:
            active.append(f"⚡ {sess} confirm window open ({asset})")
    return " · ".join(active) if active else "Waiting for next session window"

# ─── MAIN MONITORING CYCLE ────────────────────────────────────────────────────

async def run_cycle(asset_sessions, dst_on, bot_token, chat_id):
    now_wat = datetime.now(WAT)
    now_min = minutes_now(now_wat)

    with state_lock:
        app_state["phase"] = compute_phase(now_min, dst_on, asset_sessions)
        session_data = app_state["session_data"]

    # Midnight reset
    if now_wat.hour == 0 and now_wat.minute < 2:
        with state_lock:
            app_state["session_data"] = init_session_data(asset_sessions)
        log("Midnight reset — all session data cleared", "info")
        return

    for asset, session_name in asset_sessions.items():
        sym = SYMBOL_MAP.get(asset)
        if not sym:
            log(f"Unknown symbol for {asset}", "error")
            continue

        d = session_data.get(asset)
        if d is None:
            continue

        win    = SESSION_WINDOWS[session_name]
        cs, ce = get_confirm_window(session_name, dst_on)

        # ── STEP 1: Build session H/L ────────────────────────────────────────
        if in_build_window(now_min, session_name) or \
           (past_build_window(now_min, session_name) and d["session_high"] is None):

            s_high, s_low = await fetch_session_hl(sym, session_name)
            if s_high is not None:
                with state_lock:
                    app_state["session_data"][asset]["session_high"] = s_high
                    app_state["session_data"][asset]["session_low"]  = s_low
                log(f"{asset} [{session_name}] H/L updated: H={s_high}  L={s_low}", "info")
            else:
                log(f"{asset} [{session_name}] No candles in build window yet", "info")

        # ── STEP 2: Watch for breach ─────────────────────────────────────────
        if (past_build_window(now_min, session_name)
                and d["session_high"] is not None
                and d["breach_type"] is None):

            c15 = await get_latest_completed_candle(sym, 900)
            if c15:
                c_high = float(c15["high"])
                c_low  = float(c15["low"])
                s_high = d["session_high"]
                s_low  = d["session_low"]

                if c_high > s_high:
                    breach = "HIGH"
                elif c_low < s_low:
                    breach = "LOW"
                else:
                    breach = None

                if breach:
                    with state_lock:
                        app_state["session_data"][asset]["breach_type"]   = breach
                        app_state["session_data"][asset]["breach_candle"] = c15

                    direction_hint = "SELL" if breach == "HIGH" else "BUY"
                    emoji = "🔴" if breach == "HIGH" else "🟢"
                    level = s_high if breach == "HIGH" else s_low
                    taken = c_high if breach == "HIGH" else c_low

                    log(f"{asset} [{session_name}] {emoji} {breach} BREACHED | Level:{level} Candle:{'H' if breach=='HIGH' else 'L'}={taken}", "alert")

                    if not d["breach_alert_sent"]:
                        msg = (
                            f"{emoji} <b>SESSION {'HIGH' if breach=='HIGH' else 'LOW'} BREACHED</b>\n"
                            f"Asset: <b>{asset}</b>  |  Session: <b>{session_name}</b>\n"
                            f"{'High' if breach=='HIGH' else 'Low'}: {level}  Breach {'high' if breach=='HIGH' else 'low'}: {taken}\n"
                            f"Time (WAT): {now_wat.strftime('%H:%M')}\n\n"
                            f"Watching for <b>{direction_hint}</b> confirmation..."
                        )
                        send_telegram(bot_token, chat_id, msg)
                        with state_lock:
                            app_state["session_data"][asset]["breach_alert_sent"] = True

        # ── STEP 3: Confirmation close (within the 2-hr post-session window) ─
        #
        # SELL: session HIGH breached by bullish candle
        #   → Need bearish candle to CLOSE BELOW THE LOW of that breach candle
        #
        # BUY: session LOW breached by bearish candle
        #   → Need bullish candle to CLOSE ABOVE THE HIGH of that breach candle
        # ─────────────────────────────────────────────────────────────────────
        if (in_confirm_window(now_min, session_name, dst_on)
                and d["breach_type"] is not None
                and d["breach_candle"] is not None
                and not d["confirm_alert_sent"]):

            breach_type  = d["breach_type"]
            bc           = d["breach_candle"]
            bc_high      = float(bc["high"])
            bc_low       = float(bc["low"])
            bc_epoch     = bc["epoch"]
            confirmed_tf = None

            # 15M check
            c15 = await get_latest_completed_candle(sym, 900)
            if c15 and c15["epoch"] > bc_epoch:
                c15_close = float(c15["close"])
                if breach_type == "HIGH" and c15_close < bc_low:
                    confirmed_tf = "15M"
                    log(f"{asset} | 15M SELL confirmed | Bearish close {c15_close} < Breach low {bc_low}", "confirm")
                elif breach_type == "LOW" and c15_close > bc_high:
                    confirmed_tf = "15M"
                    log(f"{asset} | 15M BUY confirmed | Bullish close {c15_close} > Breach high {bc_high}", "confirm")

            # 5M check (if 15M not yet confirmed)
            if not confirmed_tf:
                candles_5m = await get_recent_completed_candles(sym, 300, n=20)
                for c5 in reversed(candles_5m):
                    if c5["epoch"] <= bc_epoch:
                        continue
                    c5_close = float(c5["close"])
                    if breach_type == "HIGH" and c5_close < bc_low:
                        confirmed_tf = "5M"
                        log(f"{asset} | 5M SELL confirmed | Bearish close {c5_close} < Breach low {bc_low}", "confirm")
                        break
                    elif breach_type == "LOW" and c5_close > bc_high:
                        confirmed_tf = "5M"
                        log(f"{asset} | 5M BUY confirmed | Bullish close {c5_close} > Breach high {bc_high}", "confirm")
                        break

            if confirmed_tf:
                direction = "SELL" if breach_type == "HIGH" else "BUY"
                emoji     = "🔴" if direction == "SELL" else "🟢"
                swept_lvl = d["session_high"] if breach_type == "HIGH" else d["session_low"]

                msg = (
                    f"{emoji} <b>TRADE CONFIRMATION - {direction}</b>\n"
                    f"Asset: <b>{asset}</b>  |  Session: <b>{session_name}</b>\n"
                    f"Signal TF: <b>{confirmed_tf}</b>\n"
                    f"{session_name} {'High' if breach_type=='HIGH' else 'Low'} swept: {swept_lvl}\n"
                    f"Breach candle: H={bc_high}  L={bc_low}\n"
                    f"Time (WAT): {now_wat.strftime('%H:%M')}\n\n"
                    f"Look for <b>{direction}</b> entry"
                )
                send_telegram(bot_token, chat_id, msg)
                with state_lock:
                    app_state["session_data"][asset]["confirmed"]          = True
                    app_state["session_data"][asset]["confirmed_tf"]       = confirmed_tf
                    app_state["session_data"][asset]["confirm_alert_sent"] = True

# ─── BACKGROUND THREAD ────────────────────────────────────────────────────────

def all_windows_expired(asset_sessions, dst_on):
    """Returns True when ALL sessions confirmation windows have closed for today."""
    now_min = datetime.now(WAT).hour * 60 + datetime.now(WAT).minute
    for session_name in set(asset_sessions.values()):
        _, ce = get_confirm_window(session_name, dst_on)
        if now_min < ce:
            return False
    return True


def monitoring_loop(asset_sessions, dst_on, bot_token, chat_id):
    summary = ", ".join(f"{a} ({s})" for a, s in asset_sessions.items())
    log(f"Monitoring started | {summary} | DST: {'ON (BST)' if dst_on else 'OFF (GMT)'}", "info")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        while not stop_event.is_set():
            try:
                loop.run_until_complete(run_cycle(asset_sessions, dst_on, bot_token, chat_id))
            except Exception as e:
                log(f"Cycle error: {e}", "error")

            # Auto-stop when all confirmation windows have closed for the day
            if all_windows_expired(asset_sessions, dst_on):
                log("All session windows closed — auto-stopping for today.", "info")
                with state_lock:
                    app_state["running"] = False
                    app_state["phase"]   = "Session complete for today. Restart tomorrow."
                stop_event.set()
                break

            stop_event.wait(timeout=5)  # 5s poll — Stop button is near-instant
    finally:
        loop.close()
        log("Monitoring stopped", "info")

# ─── FLASK + HTML ─────────────────────────────────────────────────────────────

app = Flask(__name__)

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Session Alert Trader</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg:      #0d1117;
    --surface: #161b22;
    --border:  #30363d;
    --text:    #e6edf3;
    --muted:   #8b949e;
    --blue:    #58a6ff;
    --green:   #3fb950;
    --red:     #f85149;
    --yellow:  #e3b341;
  }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }

  .header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 1.1rem; color: var(--blue); }
  .header p  { font-size: 0.78rem; color: var(--muted); margin-top: 2px; }

  .container { max-width: 980px; margin: 0 auto; padding: 20px 16px; display: grid; gap: 14px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; }
  .card-title { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-bottom: 14px; font-weight: 600; }

  label { display: block; font-size: 0.8rem; color: var(--muted); margin-bottom: 5px; }
  input[type="text"], input[type="password"] {
    width: 100%; padding: 8px 11px; background: var(--bg); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-size: 0.875rem; outline: none; transition: border 0.15s;
  }
  input:focus { border-color: var(--blue); }

  /* Asset rows */
  .asset-rows { display: flex; flex-direction: column; gap: 10px; margin-bottom: 12px; }
  .asset-row  {
    display: grid; grid-template-columns: 160px 1fr auto;
    gap: 10px; align-items: center;
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px;
  }
  @media(max-width:600px){ .asset-row { grid-template-columns: 1fr; } }
  .asset-row select {
    width: 100%; padding: 7px 10px; background: var(--bg); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-size: 0.85rem; outline: none;
  }
  .asset-row select:focus { border-color: var(--blue); }

  /* Session pills */
  .sess-pills { display: flex; gap: 6px; flex-wrap: wrap; }
  .sess-pill  {
    padding: 6px 14px; border-radius: 20px; font-size: 0.75rem; font-weight: 600;
    cursor: pointer; border: 1px solid var(--border); background: var(--bg);
    color: var(--muted); transition: all 0.15s; user-select: none;
  }
  .sess-pill:hover { border-color: var(--muted); color: var(--text); }
  .sess-pill.active-asian  { background: #1f3a5f; border-color: #58a6ff; color: #79c0ff; }
  .sess-pill.active-london { background: #1f3a1f; border-color: #3fb950; color: #56d364; }
  .sess-pill.active-ny     { background: #3a2a0a; border-color: #e3b341; color: #f0c060; }

  .btn-remove { background: none; border: none; color: var(--muted); font-size: 1.1rem; cursor: pointer; padding: 2px 6px; border-radius: 4px; line-height: 1; }
  .btn-remove:hover { color: var(--red); }
  .btn-add { background: none; border: 1px dashed var(--border); color: var(--muted);
             padding: 7px 18px; border-radius: 6px; font-size: 0.82rem; cursor: pointer; transition: 0.15s; }
  .btn-add:hover { border-color: var(--blue); color: var(--blue); }

  /* Two col layout */
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  @media(max-width:640px){ .two-col { grid-template-columns: 1fr; } }

  /* DST */
  .dst-row { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 10px; }
  .toggle-wrap { position: relative; flex-shrink: 0; width: 42px; height: 22px; margin-top: 2px; }
  .toggle-wrap input { opacity:0; width:0; height:0; }
  .slider { position:absolute; inset:0; background:var(--border); border-radius:22px; cursor:pointer; transition:.2s; }
  .slider::before { content:''; position:absolute; width:16px; height:16px; left:3px; top:3px; background:white; border-radius:50%; transition:.2s; }
  input:checked + .slider { background:var(--blue); }
  input:checked + .slider::before { transform:translateX(20px); }
  .dst-text strong { font-size:0.83rem; color: var(--text); }
  .dst-text small  { display:block; color:var(--muted); font-size:0.73rem; margin-top:2px; }

  /* Session reference table */
  .session-ref { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; font-size: 0.78rem; }
  .session-ref table { width: 100%; border-collapse: collapse; }
  .session-ref td { padding: 5px 6px; }
  .session-ref tr:not(:last-child) td { border-bottom: 1px solid #21262d; }
  .sdot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; flex-shrink:0; }
  .d-blue { background:#58a6ff; } .d-green { background:#3fb950; } .d-yellow { background:#e3b341; }

  /* Buttons */
  .btn-row { display:flex; align-items:center; gap:10px; margin-top:16px; flex-wrap:wrap; }
  .btn { padding:9px 22px; border-radius:6px; border:none; font-size:0.875rem; font-weight:600; cursor:pointer; transition:all .15s; }
  .btn-start { background:#238636; color:#fff; }
  .btn-start:hover:not(:disabled) { background:#2ea043; }
  .btn-stop  { background:#b62324; color:#fff; }
  .btn-stop:hover:not(:disabled)  { background:#da3633; }
  .btn:disabled { background:var(--border); color:var(--muted); cursor:not-allowed; }
  #statusText { font-size:0.82rem; color:var(--muted); }

  /* Phase bar */
  .phase-bar { display:flex; align-items:center; gap:10px; margin-bottom:14px; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--muted); flex-shrink:0; }
  .dot.active { background:var(--green); box-shadow:0 0 6px var(--green); animation:pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.45} }
  #phaseLabel { font-size:0.84rem; color:var(--muted); }
  #currentTime { margin-left:auto; font-family:monospace; font-size:0.8rem; color:var(--muted); }

  /* Asset status cards */
  .asset-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:10px; }
  .asset-card { background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:13px; }
  .asset-name { font-weight:700; color:var(--blue); margin-bottom:4px; font-size:0.95rem; }
  .asset-sess-tag { font-size:0.68rem; font-weight:600; padding:2px 8px; border-radius:10px; display:inline-block; margin-bottom:10px; }
  .tag-asian  { background:#1f3a5f44; color:#79c0ff; border:1px solid #1f6feb44; }
  .tag-london { background:#1f3a1f44; color:#56d364;  border:1px solid #3fb95044; }
  .tag-ny     { background:#3a2a0a44; color:#f0c060;  border:1px solid #e3b34144; }
  .data-row { display:flex; justify-content:space-between; font-size:0.78rem; margin-bottom:5px; }
  .data-label { color:var(--muted); }
  .data-value { font-family:monospace; color:var(--text); }
  .val-high { color:var(--red); } .val-low { color:var(--green); } .val-confirm { color:var(--yellow); font-weight:600; }
  .none-msg { font-size:0.82rem; color:var(--muted); }

  /* Log */
  .log-box { background:var(--bg); border:1px solid var(--border); border-radius:6px;
             padding:10px 12px; height:230px; overflow-y:auto; font-family:'Cascadia Code',monospace; font-size:0.76rem; line-height:1.6; }
  .l-info    { color:var(--muted); }
  .l-alert   { color:var(--yellow); }
  .l-confirm { color:var(--green); }
  .l-error   { color:var(--red); }

  ::-webkit-scrollbar { width:6px; } ::-webkit-scrollbar-track { background:transparent; }
  ::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }

  /* Popup */
  .popup-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.6); z-index:1000; align-items:center; justify-content:center; }
  .popup-overlay.show { display:flex; animation:fadeIn .2s; }
  @keyframes fadeIn { from{opacity:0} to{opacity:1} }
  .popup-box { border-radius:16px; padding:40px 48px; text-align:center; max-width:420px; width:90%;
               box-shadow:0 8px 48px rgba(0,0,0,.6); animation:popIn .25s cubic-bezier(.175,.885,.32,1.275); position:relative; }
  @keyframes popIn { from{transform:scale(.8);opacity:0} to{transform:scale(1);opacity:1} }
  .popup-box.sell { background:#3d0a0a; border:2px solid #f85149; }
  .popup-box.buy  { background:#0a2e14; border:2px solid #3fb950; }
  .popup-emoji  { font-size:3.5rem; margin-bottom:12px; line-height:1; }
  .popup-action { font-size:2rem; font-weight:800; margin-bottom:8px; }
  .popup-box.sell .popup-action { color:#ff7b72; }
  .popup-box.buy  .popup-action { color:#56d364; }
  .popup-asset  { font-size:1.05rem; color:rgba(255,255,255,.8); margin-bottom:4px; }
  .popup-detail { font-size:0.8rem; color:rgba(255,255,255,.45); margin-bottom:24px; }
  .popup-dismiss { padding:10px 28px; border-radius:8px; border:none; font-size:.9rem; font-weight:700; cursor:pointer; }
  .popup-box.sell .popup-dismiss { background:#f85149; color:#fff; }
  .popup-box.buy  .popup-dismiss { background:#3fb950; color:#000; }
  .popup-close { position:absolute; top:12px; right:16px; background:none; border:none; color:rgba(255,255,255,.4); font-size:1.2rem; cursor:pointer; }

  /* Toasts */
  .toast-container { position:fixed; top:20px; right:20px; z-index:999; display:flex; flex-direction:column; gap:10px; }
  .toast { padding:12px 18px; border-radius:10px; font-size:0.85rem; font-weight:600;
           box-shadow:0 4px 20px rgba(0,0,0,.5); max-width:300px; border:1px solid; }
  .toast.show { animation:slideIn .3s; }
  .toast.sell-toast { background:#2d0c0c; border-color:#f85149; color:#ff9492; }
  .toast.buy-toast  { background:#0c2416; border-color:#3fb950; color:#7ee787; }
  @keyframes slideIn { from{transform:translateX(120%);opacity:0} to{transform:translateX(0);opacity:1} }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>📈 Session Alert Trader</h1>
    <p>Asian · London · New York AM &nbsp;—&nbsp; Deriv API · Telegram alerts · Browser pop-ups</p>
  </div>
</div>

<div class="container">

  <!-- CONFIG -->
  <div class="card">
    <div class="card-title">Configuration</div>
    <div class="two-col">

      <!-- Left: asset rows -->
      <div>
        <label style="margin-bottom:8px">Assets &amp; Sessions</label>
        <div class="asset-rows" id="assetRows"></div>
        <button class="btn-add" onclick="addAssetRow()">＋ Add asset</button>
      </div>

      <!-- Right: Telegram + DST + reference -->
      <div style="display:flex;flex-direction:column;gap:10px;">
        <div>
          <label>Telegram Bot Token</label>
          <input type="password" id="botToken" placeholder="1234567890:ABCdef…">
        </div>
        <div>
          <label>Telegram Chat ID</label>
          <input type="text" id="chatId" placeholder="-100123456789">
        </div>

        <div class="dst-row">
          <label class="toggle-wrap">
            <input type="checkbox" id="dstToggle" checked onchange="updateDst()">
            <span class="slider"></span>
          </label>
          <div class="dst-text">
            <strong id="dstLabel">UK DST (BST) is ON</strong>
            <small id="dstSub">Asian confirm window: 05:00–06:59 WAT</small>
          </div>
        </div>

        <!-- Session reference -->
        <div class="session-ref">
          <table>
            <tr>
              <td><span class="sdot d-blue"></span><strong>Asian</strong></td>
              <td style="color:var(--muted);font-size:0.75rem">Build 01:00–04:59</td>
              <td id="asianConfirmRef" style="color:#79c0ff;font-size:0.75rem">Confirm 05:00–06:59</td>
            </tr>
            <tr>
              <td><span class="sdot d-green"></span><strong>London</strong></td>
              <td style="color:var(--muted);font-size:0.75rem">Build 07:00–09:59</td>
              <td style="color:#56d364;font-size:0.75rem">Confirm 10:00–11:59</td>
            </tr>
            <tr>
              <td><span class="sdot d-yellow"></span><strong>NY AM</strong></td>
              <td style="color:var(--muted);font-size:0.75rem">Build 14:30–15:59</td>
              <td style="color:#f0c060;font-size:0.75rem">Confirm 16:00–17:59</td>
            </tr>
          </table>
        </div>
      </div>
    </div>

    <div class="btn-row">
      <button class="btn btn-start" id="startBtn" onclick="startMonitor()">▶ Start Monitoring</button>
      <button class="btn btn-stop"  id="stopBtn"  onclick="stopMonitor()" disabled>⏹ Stop</button>
      <span id="statusText">Not running</span>
    </div>
  </div>

  <!-- SESSION STATUS -->
  <div class="card">
    <div class="card-title">Session Status</div>
    <div class="phase-bar">
      <div class="dot" id="statusDot"></div>
      <span id="phaseLabel">Idle</span>
      <span id="currentTime"></span>
    </div>
    <div class="asset-grid" id="assetGrid">
      <p class="none-msg">No assets monitored yet.</p>
    </div>
  </div>

  <!-- ACTIVITY LOG -->
  <div class="card">
    <div class="card-title">Activity Log</div>
    <div class="log-box" id="logBox"><div class="l-info">Waiting to start...</div></div>
  </div>

</div>

<!-- Confirmation popup -->
<div class="popup-overlay" id="confirmPopup">
  <div class="popup-box" id="popupBox">
    <button class="popup-close" onclick="dismissPopup()">&#x2715;</button>
    <div class="popup-emoji"  id="popupEmoji"></div>
    <div class="popup-action" id="popupAction"></div>
    <div class="popup-asset"  id="popupAsset"></div>
    <div class="popup-detail" id="popupDetail"></div>
    <button class="popup-dismiss" onclick="dismissPopup()">Got it &mdash; dismiss</button>
  </div>
</div>
<div class="toast-container" id="toastContainer"></div>

<script>
const ASSETS   = ["AUD/USD","AUD/NZD","AUD/JPY","AUD/CHF","AUD/CAD","EUR/USD","EUR/AUD","EUR/CAD","EUR/CHF","EUR/GBP","EUR/JPY","EUR/NZD","GBP/USD","GBP/AUD","GBP/CAD","GBP/CHF","GBP/JPY","GBP/NZD","NZD/USD","NZD/CAD","NZD/CHF","NZD/JPY","USD/CAD","USD/JPY","US SP 500","US Tech 100"];
const SESSIONS = ["Asian","London","New York AM"];
const SESS_COLOR = { "Asian":"active-asian", "London":"active-london", "New York AM":"active-ny" };
const SESS_TAG   = { "Asian":"tag-asian",    "London":"tag-london",    "New York AM":"tag-ny" };

let rowCount = 0;

function addAssetRow(defAsset="GBP/USD", defSess="Asian") {
  const id  = ++rowCount;
  const row = document.createElement("div");
  row.className = "asset-row";
  row.id        = "row-" + id;
  const opts = ASSETS.map(a => `<option value="${a}"${a===defAsset?" selected":""}>${a}</option>`).join("");
  row.innerHTML = `
    <select id="asset-${id}">${opts}</select>
    <div class="sess-pills" id="pills-${id}">
      ${SESSIONS.map(s=>`<span class="sess-pill${s===defSess?" "+SESS_COLOR[s]:""}" data-session="${s}" onclick="selectSession(this,${id})">${s}</span>`).join("")}
    </div>
    <button class="btn-remove" onclick="removeRow(${id})">&#x2715;</button>`;
  document.getElementById("assetRows").appendChild(row);
}

function selectSession(el, id) {
  document.querySelectorAll(`#pills-${id} .sess-pill`).forEach(p => p.className = "sess-pill");
  el.classList.add(SESS_COLOR[el.dataset.session]);
}

function removeRow(id) {
  const el = document.getElementById("row-"+id);
  if (el) el.remove();
}

function getActiveSession(id) {
  const a = document.querySelector(`#pills-${id} .sess-pill[class*="active"]`);
  return a ? a.dataset.session : "Asian";
}

function collectAssetSessions() {
  const result = {};
  document.querySelectorAll(".asset-row").forEach(row => {
    const id    = row.id.split("-")[1];
    const asset = document.getElementById("asset-"+id).value;
    const sess  = getActiveSession(id);
    result[asset] = sess;
  });
  return result;
}

function updateDst() {
  const on = document.getElementById("dstToggle").checked;
  document.getElementById("dstLabel").textContent       = on ? "UK DST (BST) is ON"  : "UK DST (GMT) is OFF";
  document.getElementById("dstSub").textContent         = on ? "Asian confirm window: 05:00\u201306:59 WAT" : "Asian confirm window: 06:00\u201307:59 WAT";
  document.getElementById("asianConfirmRef").textContent = on ? "Confirm 05:00\u201306:59" : "Confirm 06:00\u201307:59";
}

// Popup
function showConfirmPopup(asset, direction, tf, sessionName, breachType) {
  const isSell = direction === "SELL";
  document.getElementById("popupBox").className     = "popup-box " + (isSell ? "sell" : "buy");
  document.getElementById("popupEmoji").textContent  = isSell ? "🔴" : "🟢";
  document.getElementById("popupAction").textContent = isSell ? "\u2B07 SELL NOW" : "\u2B06 BUY NOW";
  document.getElementById("popupAsset").textContent  = asset;
  document.getElementById("popupDetail").textContent =
    sessionName + " " + (breachType === "HIGH" ? "High" : "Low") + " swept \u00B7 Confirmed on " + tf;
  document.getElementById("confirmPopup").classList.add("show");
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator(), g = ctx.createGain();
    osc.connect(g); g.connect(ctx.destination);
    osc.frequency.value = isSell ? 440 : 660; osc.type = "sine";
    g.gain.setValueAtTime(0.4, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.8);
    osc.start(); osc.stop(ctx.currentTime + 0.8);
  } catch(e) {}
}

function dismissPopup() {
  document.getElementById("confirmPopup").classList.remove("show");
}

function showBreachToast(asset, breachType, sessionName) {
  const isSell = breachType === "HIGH";
  const t = document.createElement("div");
  t.className = "toast show " + (isSell ? "sell-toast" : "buy-toast");
  t.innerHTML = (isSell?"🔴":"🟢") + " <strong>" + asset + "</strong> \u2014 " + sessionName + " " + (isSell?"High":"Low") + " breached<br>" +
    "<span style='font-weight:400;opacity:.8'>Watching for " + (isSell?"SELL":"BUY") + " confirmation...</span>";
  document.getElementById("toastContainer").appendChild(t);
  setTimeout(()=>{ t.style.transition="opacity .5s"; t.style.opacity="0"; setTimeout(()=>t.remove(),500); }, 7000);
}

async function startMonitor() {
  const asset_sessions = collectAssetSessions();
  if (!Object.keys(asset_sessions).length) { alert("Add at least one asset."); return; }
  const botToken = document.getElementById("botToken").value.trim();
  const chatId   = document.getElementById("chatId").value.trim();
  if (!botToken || !chatId) { alert("Enter Telegram credentials first."); return; }
  const dst_on = document.getElementById("dstToggle").checked;
  const res  = await fetch("/start", { method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify({asset_sessions, bot_token:botToken, chat_id:chatId, dst_on}) });
  const data = await res.json();
  if (data.success) {
    seenBreaches={}; seenConfirmations={};
    document.getElementById("startBtn").disabled = true;
    document.getElementById("stopBtn").disabled  = false;
    document.getElementById("statusDot").classList.add("active");
    document.getElementById("statusText").textContent = "Monitoring active...";
  } else { alert("Error: " + (data.error||"Unknown")); }
}

async function stopMonitor() {
  await fetch("/stop", {method:"POST"});
  document.getElementById("startBtn").disabled = false;
  document.getElementById("stopBtn").disabled  = true;
  document.getElementById("statusDot").classList.remove("active");
  document.getElementById("statusText").textContent = "Stopped";
  seenBreaches={}; seenConfirmations={};
}

function renderAssets(sd) {
  const grid = document.getElementById("assetGrid");
  const keys = Object.keys(sd||{});
  if (!keys.length) { grid.innerHTML="<p class='none-msg'>Waiting for first data cycle...</p>"; return; }
  grid.innerHTML = keys.map(asset => {
    const d=sd[asset], sess=d.session||"Asian";
    const bc = d.breach_type==="HIGH"?"val-high":d.breach_type==="LOW"?"val-low":"";
    const ct = d.confirmed ? "&#x2705; "+d.confirmed_tf : "&mdash;";
    const cc = d.confirmed ? "val-confirm" : "";
    return `<div class="asset-card">
      <div class="asset-name">${asset}</div>
      <span class="asset-sess-tag ${SESS_TAG[sess]||"tag-asian"}">${sess}</span>
      <div class="data-row"><span class="data-label">Session High</span><span class="data-value">${d.session_high??'&mdash;'}</span></div>
      <div class="data-row"><span class="data-label">Session Low</span><span class="data-value">${d.session_low??'&mdash;'}</span></div>
      <div class="data-row"><span class="data-label">Breach</span><span class="data-value ${bc}">${d.breach_type||'&mdash;'}</span></div>
      <div class="data-row"><span class="data-label">Confirmation</span><span class="data-value ${cc}">${ct}</span></div>
    </div>`;
  }).join("");
}

let seenBreaches={}, seenConfirmations={};

async function poll() {
  try {
    const res=await fetch("/status"), data=await res.json();
    document.getElementById("currentTime").textContent = data.current_time_wat;
    document.getElementById("phaseLabel").textContent  = data.phase||"Idle";
    renderAssets(data.session_data||{});

    // Sync button state with server — handles auto-stop
    const isRunning = data.running;
    document.getElementById("startBtn").disabled = isRunning;
    document.getElementById("stopBtn").disabled  = !isRunning;
    if (!isRunning) {
      document.getElementById("statusDot").classList.remove("active");
      if (document.getElementById("statusText").textContent === "Monitoring active...") {
        document.getElementById("statusText").textContent = "Stopped";
      }
    }

    Object.entries(data.session_data||{}).forEach(([asset,d])=>{
      if (d.breach_type && d.breach_alert_sent && !seenBreaches[asset]) {
        seenBreaches[asset]=true;
        showBreachToast(asset, d.breach_type, d.session);
      }
      if (d.confirmed && d.confirm_alert_sent && !seenConfirmations[asset]) {
        seenConfirmations[asset]=true;
        showConfirmPopup(asset, d.breach_type==="HIGH"?"SELL":"BUY", d.confirmed_tf, d.session, d.breach_type);
      }
    });
    const box=document.getElementById("logBox");
    box.innerHTML=(data.logs||[]).map(l=>`<div class="l-${l.type}">[${l.time}] ${l.msg}</div>`).join("");
    box.scrollTop=box.scrollHeight;
  } catch(e){}
  setTimeout(poll, 5000);
}

// Init with 2 default rows
addAssetRow("GBP/USD","Asian");
addAssetRow("AUD/USD","London");
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
    # If already running, force-stop cleanly before starting new session
    if app_state["running"] or (monitor_thread and monitor_thread.is_alive()):
        stop_event.set()
        if monitor_thread and monitor_thread.is_alive():
            monitor_thread.join(timeout=8)  # wait up to 8s for thread to die
        with state_lock:
            app_state["running"] = False
        stop_event.clear()
        log("Previous session force-stopped to start new one.", "info")

    data           = flask_request.get_json()
    asset_sessions = data.get("asset_sessions", {})
    bot_token      = data.get("bot_token", "").strip()
    chat_id        = data.get("chat_id", "").strip()
    dst_on         = data.get("dst_on", True)

    if not asset_sessions:
        return jsonify({"success": False, "error": "No assets configured"})
    if not bot_token or not chat_id:
        return jsonify({"success": False, "error": "Missing Telegram credentials"})

    with state_lock:
        app_state.update({
            "running":        True,
            "dst_on":         dst_on,
            "asset_sessions": asset_sessions,
            "bot_token":      bot_token,
            "chat_id":        chat_id,
            "logs":           [],
            "session_data":   init_session_data(asset_sessions),
        })

    stop_event.clear()
    monitor_thread = threading.Thread(
        target=monitoring_loop,
        args=(asset_sessions, dst_on, bot_token, chat_id),
        daemon=True
    )
    monitor_thread.start()
    return jsonify({"success": True})


@app.route("/stop", methods=["POST"])
def stop():
    global monitor_thread
    stop_event.set()
    with state_lock:
        app_state["running"] = False
    if monitor_thread and monitor_thread.is_alive():
        monitor_thread.join(timeout=8)
    stop_event.clear()  # reset so next Start works cleanly
    return jsonify({"success": True})


@app.route("/status")
def status():
    with state_lock:
        now_wat = datetime.now(WAT)
        return jsonify({
            "running":          app_state["running"],
            "phase":            app_state["phase"],
            "current_time_wat": now_wat.strftime("%H:%M:%S WAT"),
            "session_data":     app_state["session_data"],
            "logs":             app_state["logs"][-60:],
        })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print("=" * 55)
    print("  Session Alert Trader  —  Asian, London, NY AM")
    print(f"  Running on port {port}")
    print("=" * 55)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
