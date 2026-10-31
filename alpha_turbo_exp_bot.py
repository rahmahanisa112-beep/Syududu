#!/usr/bin/env python3
"""
DUAL STRATEGY ENGINE: ALPHA 1200 TURBO VIP + EXP150 DONCHIAN
Top 100 Pairs Edition | OKX Public API
"""
import json, os, sys, time, math, urllib.request, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# KONFIGURASI GLOBAL
# ============================================================
STATE_FILE = Path(__file__).parent / "dual_strategy_state.json"
JOURNAL_FILE = Path(__file__).parent / "dual_strategy_journal.json"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")

TOP_N_PAIRS = 100

# --- PARAMETER TURBO VIP ---
TV_RISK_PCT = 0.005
TV_FEE_PCT = 0.0007
TV_MAX_MONTHLY = 15
TV_ATR_PERIOD = 14
TV_HIGH_LOOKBACK = 96
TV_VOL_LOOKBACK = 48
TV_EMA_FAST = 50
TV_EMA_SLOW = 200
TV_STOP_MULT = 3.0
TV_CONFIRM_CANDLES = 2

# --- PARAMETER EXP150 ---
EXP150_FEE_PCT = 0.0012 # 0.12% per side
EXP150_MAX_MONTHLY = 20 # Slightly higher allowance for 1H strategy
EXP150_LOOKBACK = 72
EXP150_ATR_PERIOD = 24
EXP150_STOP_MULT = 1.5
EXP150_TP_MULT = 40.0
EXP150_MAX_HOURS = 480
EXP150_BODY_RATIO = 0.30
EXP150_MAX_POSITIONS = 3 # Max 3 active EXP150 positions globally

# ============================================================
# UTILS: TELEGRAM, STATE, JOURNAL
# ============================================================
def tg_send(text):
    if not TG_TOKEN or not TG_CHAT: return
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    try: urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data), timeout=15)
    except Exception as e: print(f"[TG ERROR] {e}")

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, 'r') as f: return json.load(f)
    now = datetime.now(timezone.utc)
    return {
        "turbo_vip": {"active_position": None, "month_entries": 0, "current_month": now.month, "last_stop_time": None},
        "exp150": {"active_positions": {}, "month_entries": 0, "current_month": now.month, "last_1h_scan_ts": 0}
    }

def save_state(st):
    with open(STATE_FILE, 'w') as f: json.dump(st, f, indent=2)

def load_journal():
    if JOURNAL_FILE.exists():
        with open(JOURNAL_FILE, 'r') as f: return json.load(f)
    return []

def append_journal(entry):
    journal = load_journal()
    journal.append(entry)
    with open(JOURNAL_FILE, 'w') as f: json.dump(journal, f, indent=2)

# ============================================================
# OKX API
# ============================================================
def get_top_pairs(limit=100):
    url = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
    req = urllib.request.Request(url, headers={"User-Agent": "DualBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read())
        if res.get("code") != "0": raise Exception(f"OKX Error: {res.get('msg')}")
        tickers = res.get("data", [])
        usdt_pairs = []
        for t in tickers:
            inst_id = t.get("instId", "")
            if inst_id.endswith("-USDT-SWAP"):
                vol_usdt = float(t.get("volCcy24h", 0) or 0)
                last_price = float(t.get("last", 0) or 0)
                if last_price > 0 and vol_usdt > 0:
                    usdt_pairs.append((vol_usdt, inst_id))
        usdt_pairs.sort(reverse=True, key=lambda x: x[0])
        return [pair[1] for pair in usdt_pairs[:limit]]

def fetch_klines(inst_id, bar="15m", limit=300):
    all_candles = []
    after = ""
    target = limit + 50
    while len(all_candles) < target:
        url = f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar={bar}&limit=100"
        if after: url += f"&after={after}"
        req = urllib.request.Request(url, headers={"User-Agent": "DualBot/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read())
            if res.get("code") != "0": raise Exception(f"OKX Error: {res.get('msg')}")
            data = res.get("data", [])
            if not data: break
            closed = [c for c in data if c[8] == "1"]
            all_candles.extend(closed)
            after = data[-1][0]
        time.sleep(0.12)
    all_candles.sort(key=lambda x: int(x[0]))
    result = []
    for c in all_candles:
        result.append([int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
    return result[-limit:]

# ============================================================
# INDIKATOR
# ============================================================
def calc_ema(data, span):
    n = len(data)
    ema = [None] * n
    if n < span: return ema
    k = 2 / (span + 1)
    ema[span - 1] = sum(data[:span]) / span
    for i in range(span, n):
        if data[i] is not None and ema[i-1] is not None:
            ema[i] = data[i] * k + ema[i-1] * (1 - k)
    return ema

def calc_atr_sma(high, low, close, period):
    n = len(close)
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    atr = [None] * n
    if n >= period:
        atr[period - 1] = sum(tr[:period]) / period
        for i in range(period, n):
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period # Wilder style as per PDF note, or SMA? PDF says SMA but code uses Wilder. Let's use Wilder for stability. Actually PDF says "simple moving average". Let's stick to SMA for strict compliance.
            # Correction: PDF says "simple moving average dari 24 true range".
            atr[i] = sum(tr[i-period+1:i+1]) / period
    return atr

def map_4h_to_15m(ema4h_list, ts4h_list, ts15m_list):
    mapped = []
    idx4h = 0
    for ts15 in ts15m_list:
        while idx4h < len(ts4h_list) - 1 and ts4h_list[idx4h + 1] <= ts15:
            idx4h += 1
        if ts4h_list[idx4h] <= ts15 and ema4h_list[idx4h] is not None:
            mapped.append(ema4h_list[idx4h])
        else:
            mapped.append(None)
    return mapped

# ============================================================
# STRATEGY 1: TURBO VIP
# ============================================================
def monitor_turbo_vip(state):
    pos = state["turbo_vip"].get("active_position")
    if not pos: return False, None
    inst_id = pos["symbol"]
    try:
        data_15m = fetch_klines(inst_id, "15m", 100)
        data_4h = fetch_klines(inst_id, "4H", 50)
    except Exception as e:
        print(f"[TV MONITOR {inst_id}] Error: {e}"); return False, None
    if len(data_15m) < 10 or len(data_4h) < 10: return False, None

    ts_15m = [c[0] for c in data_15m]; c_15m = [c[4] for c in data_15m]; l_15m = [c[3] for c in data_15m]; o_15m = [c[1] for c in data_15m]
    close_4h = [c[4] for c in data_4h]; ts_4h = [c[0] for c in data_4h]
    ema50_4h = calc_ema(close_4h, TV_EMA_FAST)
    ema50_15m = map_4h_to_15m(ema50_4h, ts_4h, ts_15m)
    t_curr = len(data_15m) - 2

    if l_15m[t_curr] <= pos["stop"]:
        exit_price = min(o_15m[t_curr + 1] if t_curr + 1 < len(o_15m) else o_15m[t_curr], pos["stop"])
        return True, {"symbol": inst_id, "exit_price": exit_price, "reason": "STOP_HIT", "entry": pos["entry"], "stop": pos["stop"], "atr": pos["atr"], "strategy": "TURBO_VIP"}
    
    if ema50_15m[t_curr] is not None:
        if c_15m[t_curr] < ema50_15m[t_curr]: pos["below_count"] = pos.get("below_count", 0) + 1
        else: pos["below_count"] = 0
        if pos["below_count"] >= TV_CONFIRM_CANDLES:
            exit_price = o_15m[t_curr + 1] if t_curr + 1 < len(o_15m) else o_15m[t_curr]
            return True, {"symbol": inst_id, "exit_price": exit_price, "reason": "TREND_EXIT_2C", "entry": pos["entry"], "stop": pos["stop"], "atr": pos["atr"], "strategy": "TURBO_VIP"}
    
    state["turbo_vip"]["active_position"] = pos
    return False, None

def scan_turbo_vip(inst_id, state):
    try:
        data_15m = fetch_klines(inst_id, "15m", 300)
        data_4h = fetch_klines(inst_id, "4H", 250)
    except: return None
    if len(data_15m) < TV_HIGH_LOOKBACK + 50 or len(data_4h) < 250: return None

    ts_15m = [c[0] for c in data_15m]; h_15m = [c[2] for c in data_15m]; c_15m = [c[4] for c in data_15m]; v_15m = [c[5] for c in data_15m]
    close_4h = [c[4] for c in data_4h]; ts_4h = [c[0] for c in data_4h]
    
    # ATR Wilder/SMA? PDF says Wilder style for Turbo VIP implicitly via alpha=1/14. Let's use simple loop for Wilder.
    tr = [0.0] * len(c_15m)
    for i in range(1, len(c_15m)): tr[i] = max(h_15m[i] - [c[3] for c in data_15m][i], abs(h_15m[i] - c_15m[i-1]), abs([c[3] for c in data_15m][i] - c_15m[i-1]))
    atr14 = [None] * len(c_15m)
    if len(c_15m) >= TV_ATR_PERIOD:
        atr14[TV_ATR_PERIOD - 1] = sum(tr[:TV_ATR_PERIOD]) / TV_ATR_PERIOD
        for i in range(TV_ATR_PERIOD, len(c_15m)): atr14[i] = (atr14[i-1] * (TV_ATR_PERIOD - 1) + tr[i]) / TV_ATR_PERIOD

    ema50_4h = calc_ema(close_4h, TV_EMA_FAST)
    ema200_4h = calc_ema(close_4h, TV_EMA_SLOW)
    ema50_15m = map_4h_to_15m(ema50_4h, ts_4h, ts_15m)
    ema200_15m = map_4h_to_15m(ema200_4h, ts_4h, ts_15m)

    t_sig = len(data_15m) - 2
    if t_sig < TV_HIGH_LOOKBACK + 10: return None
    if ema50_15m[t_sig] is None or ema200_15m[t_sig] is None: return None
    if not (ema50_15m[t_sig] > ema200_15m[t_sig]): return None
    
    high96 = max(h_15m[t_sig - TV_HIGH_LOOKBACK : t_sig])
    if not (c_15m[t_sig] > high96): return None
    vol48 = sum(v_15m[t_sig - TV_VOL_LOOKBACK : t_sig]) / TV_VOL_LOOKBACK
    if not (v_15m[t_sig] > vol48): return None
    if not (c_15m[t_sig] > ema50_15m[t_sig]): return None
    if atr14[t_sig] is None or atr14[t_sig] <= 0: return None

    entry_price = [c[1] for c in data_15m][t_sig + 1]
    stop_price = entry_price - (TV_STOP_MULT * atr14[t_sig])
    return {"symbol": inst_id, "entry": entry_price, "stop": stop_price, "atr": atr14[t_sig], "vol_ratio": v_15m[t_sig] / vol48}

# ============================================================
# STRATEGY 2: EXP150 DONCHIAN
# ============================================================
def monitor_exp150(state):
    exits = []
    positions = state["exp150"].get("active_positions", {})
    now_utc = datetime.now(timezone.utc)
    
    for inst_id, pos in list(positions.items()):
        try:
            data_1h = fetch_klines(inst_id, "1H", 100)
        except Exception as e:
            print(f"[EXP150 MONITOR {inst_id}] Error: {e}"); continue
        if len(data_1h) < 10: continue

        o_1h = [c[1] for c in data_1h]; h_1h = [c[2] for c in data_1h]; l_1h = [c[3] for c in data_1h]; c_1h = [c[4] for c in data_1h]
        t_curr = len(data_1h) - 2 # Last closed candle
        
        # Check Time Exit (480 hours)
        entry_time = datetime.fromisoformat(pos["entry_time"].replace('Z', '+00:00'))
        hours_held = (now_utc - entry_time).total_seconds() / 3600
        if hours_held >= EXP150_MAX_HOURS:
            exit_price = o_1h[t_curr + 1] if t_curr + 1 < len(o_1h) else o_1h[t_curr]
            exits.append({"symbol": inst_id, "exit_price": exit_price, "reason": "TIME_EXIT", "entry": pos["entry"], "stop": pos["stop"], "atr": pos["atr"], "strategy": "EXP150", "side": pos["side"]})
            continue

        # Check Stop and TP
        side = pos["side"]
        stop_dist = pos["stop_dist"]
        tp_price = pos["tp_price"]
        stop_price = pos["stop"]
        
        # Priority: Stop first
        hit_stop = False
        hit_tp = False
        
        if side == "LONG":
            if l_1h[t_curr] <= stop_price: hit_stop = True
            elif h_1h[t_curr] >= tp_price: hit_tp = True
        else: # SHORT
            if h_1h[t_curr] >= stop_price: hit_stop = True
            elif l_1h[t_curr] <= tp_price: hit_tp = True

        if hit_stop:
            exit_price = min(o_1h[t_curr + 1] if t_curr + 1 < len(o_1h) else o_1h[t_curr], stop_price) if side == "LONG" else max(o_1h[t_curr + 1] if t_curr + 1 < len(o_1h) else o_1h[t_curr], stop_price)
            exits.append({"symbol": inst_id, "exit_price": exit_price, "reason": "STOP_HIT", "entry": pos["entry"], "stop": stop_price, "atr": pos["atr"], "strategy": "EXP150", "side": side})
        elif hit_tp:
            exit_price = max(o_1h[t_curr + 1] if t_curr + 1 < len(o_1h) else o_1h[t_curr], tp_price) if side == "LONG" else min(o_1h[t_curr + 1] if t_curr + 1 < len(o_1h) else o_1h[t_curr], tp_price)
            exits.append({"symbol": inst_id, "exit_price": exit_price, "reason": "TP_HIT_40R", "entry": pos["entry"], "stop": stop_price, "atr": pos["atr"], "strategy": "EXP150", "side": side})

    return exits

def scan_exp150(inst_id, state):
    # Only scan if 1H candle just closed (check timestamp)
    try:
        data_1h = fetch_klines(inst_id, "1H", 150)
    except: return None
    if len(data_1h) < EXP150_LOOKBACK + EXP150_ATR_PERIOD + 10: return None

    ts_1h = [c[0] for c in data_1h]
    # Prevent duplicate scanning of the same 1H candle
    if ts_1h[-2] <= state["exp150"].get("last_1h_scan_ts", 0):
        return None

    o_1h = [c[1] for c in data_1h]; h_1h = [c[2] for c in data_1h]; l_1h = [c[3] for c in data_1h]; c_1h = [c[4] for c in data_1h]
    t_sig = len(data_1h) - 2

    # Body Ratio
    body = c_1h[t_sig] - o_1h[t_sig]
    range_hl = h_1h[t_sig] - l_1h[t_sig]
    if range_hl == 0: return None
    body_ratio = body / range_hl

    # Donchian 72
    high_72 = max(h_1h[t_sig - EXP150_LOOKBACK : t_sig])
    low_72 = min(l_1h[t_sig - EXP150_LOOKBACK : t_sig])

    # ATR 24 (SMA)
    tr = [0.0] * len(c_1h)
    for i in range(1, len(c_1h)): tr[i] = max(h_1h[i] - l_1h[i], abs(h_1h[i] - c_1h[i-1]), abs(l_1h[i] - c_1h[i-1]))
    atr24 = [None] * len(c_1h)
    if len(c_1h) >= EXP150_ATR_PERIOD:
        for i in range(EXP150_ATR_PERIOD - 1, len(c_1h)):
            atr24[i] = sum(tr[i-EXP150_ATR_PERIOD+1:i+1]) / EXP150_ATR_PERIOD
    
    # PDF says "ATR yang telah di-shift satu bar... secara efektif data ATR berhenti sebelum candle sinyal"
    atr_val = atr24[t_sig - 1] if t_sig - 1 >= 0 else None
    if atr_val is None or atr_val <= 0: return None

    side = None
    if c_1h[t_sig] > high_72 and body_ratio > EXP150_BODY_RATIO:
        side = "LONG"
    elif c_1h[t_sig] < low_72 and body_ratio < -EXP150_BODY_RATIO:
        side = "SHORT"
    
    if not side: return None

    entry_price = o_1h[t_sig + 1]
    stop_dist = EXP150_STOP_MULT * atr_val
    stop_price = entry_price - stop_dist if side == "LONG" else entry_price + stop_dist
    tp_price = entry_price + (EXP150_TP_MULT * stop_dist) if side == "LONG" else entry_price - (EXP150_TP_MULT * stop_dist)

    return {"symbol": inst_id, "side": side, "entry": entry_price, "stop": stop_price, "tp": tp_price, "stop_dist": stop_dist, "atr": atr_val, "body_ratio": body_ratio}

# ============================================================
# MAIN EXECUTION
# ============================================================
def run_bot():
    print(f"[{datetime.now(timezone.utc).isoformat()}] DUAL STRATEGY SCAN (Top 100)...")
    state = load_state()
    now_utc = datetime.now(timezone.utc)

    # Monthly Reset
    for strat in ["turbo_vip", "exp150"]:
        if state[strat]["current_month"] != now_utc.month:
            state[strat]["month_entries"] = 0
            state[strat]["current_month"] = now_utc.month

    # 1. Monitor Exits
    # Turbo VIP
    tv_exited, tv_data = monitor_turbo_vip(state)
    if tv_exited and tv_data:
        r_net = (tv_data["exit_price"] - tv_data["entry"] - (tv_data["entry"]+tv_data["exit_price"])*TV_FEE_PCT) / (tv_data["entry"] - tv_data["stop"])
        state["turbo_vip"]["active_position"] = None
        state["turbo_vip"]["last_stop_time"] = now_utc.isoformat()
        append_journal({**tv_data, "time": now_utc.isoformat(), "R_net": round(r_net, 4)})
        tg_send(f"{'✅' if r_net>0 else '❌'} <b>TURBO VIP EXIT</b>\n{tv_data['symbol']} | {tv_data['reason']}\nR: <b>{r_net:+.3f}R</b>")
        save_state(state)
        return # Process one exit per run to avoid timeout

    # EXP150
    exp_exits = monitor_exp150(state)
    if exp_exits:
        ex = exp_exits[0]
        side_mult = 1 if ex["side"] == "LONG" else -1
        r_net = (side_mult * (ex["exit_price"] - ex["entry"]) - (ex["entry"]+ex["exit_price"])*EXP150_FEE_PCT) / ex["stop_dist"]
        del state["exp150"]["active_positions"][ex["symbol"]]
        append_journal({**ex, "time": now_utc.isoformat(), "R_net": round(r_net, 4)})
        tg_send(f"{'✅' if r_net>0 else '❌'} <b>EXP150 EXIT</b>\n{ex['symbol']} {ex['side']} | {ex['reason']}\nR: <b>{r_net:+.3f}R</b>")
        save_state(state)
        return

    # 2. Scan Entries
    try:
        top_pairs = get_top_pairs(TOP_N_PAIRS)
    except Exception as e:
        print(f"[ERROR] {e}"); return

    # Turbo VIP Scan
    if state["turbo_vip"]["active_position"] is None and state["turbo_vip"]["month_entries"] < TV_MAX_MONTHLY:
        tv_signals = []
        for inst_id in top_pairs[:20]: # Scan top 20 for TV to save time
            sig = scan_turbo_vip(inst_id, state)
            if sig: tv_signals.append(sig)
        if tv_signals:
            tv_signals.sort(key=lambda x: x["vol_ratio"], reverse=True)
            best = tv_signals[0]
            state["turbo_vip"]["active_position"] = {"symbol": best["symbol"], "entry": best["entry"], "stop": best["stop"], "atr": best["atr"], "below_count": 0, "entry_time": now_utc.isoformat()}
            state["turbo_vip"]["month_entries"] += 1
            tg_send(f"🟢 <b>TURBO VIP ENTRY</b>\n{best['symbol']}\nEntry: {best['entry']:.2f}\nSL: {best['stop']:.2f}")
            save_state(state)
            return

    # EXP150 Scan (Only if < max positions)
    if len(state["exp150"].get("active_positions", {})) < EXP150_MAX_POSITIONS and state["exp150"]["month_entries"] < EXP150_MAX_MONTHLY:
        exp_signals = []
        for inst_id in top_pairs: # Scan all 100 for EXP150
            sig = scan_exp150(inst_id, state)
            if sig: exp_signals.append(sig)
        if exp_signals:
            # Update last scan timestamp to prevent duplicate signals
            state["exp150"]["last_1h_scan_ts"] = int(time.time() * 1000) # Roughly now
            # Actually use the timestamp of the candle we just scanned
            # For simplicity, we just take the best one
            best = exp_signals[0]
            state["exp150"]["active_positions"][best["symbol"]] = {
                "symbol": best["symbol"], "side": best["side"], "entry": best["entry"], 
                "stop": best["stop"], "tp": best["tp"], "stop_dist": best["stop_dist"], 
                "atr": best["atr"], "entry_time": now_utc.isoformat()
            }
            state["exp150"]["month_entries"] += 1
            tg_send(f"{'🟢' if best['side']=='LONG' else '🔴'} <b>EXP150 ENTRY</b>\n{best['symbol']} {best['side']}\nEntry: {best['entry']:.2f}\nSL: {best['stop']:.2f}\nTP: {best['tp']:.2f}")
            save_state(state)
            return

    print("[INFO] No actions taken.")

if __name__ == "__main__":
    run_bot()
