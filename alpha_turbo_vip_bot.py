#!/usr/bin/env python3
"""
ALPHA 1200 TURBO VIP - TOP 100 PAIRS EDITION
Spesifikasi: Long Only, 2-Candle Exit Confirmation
Data Source: OKX Public API (Top 100 USDT Pairs)
Fitur: Multi-asset scanning, Max 1 posisi total, Auto-monitoring
"""
import json, os, sys, time, math, urllib.request, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# KONFIGURASI
# ============================================================
STATE_FILE = Path(__file__).parent / "alpha_turbo_state.json"
JOURNAL_FILE = Path(__file__).parent / "alpha_turbo_journal.json"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")

# Parameter Terkunci (PDF TURBO VIP)
RISK_PCT = 0.005       # 0.5% ekuitas per trade
FEE_PCT = 0.0007       # 0.07% per sisi
MAX_MONTHLY = 15       # Max 15 entry/bulan (global)
ATR_PERIOD = 14
HIGH_LOOKBACK = 96
VOL_LOOKBACK = 48
EMA_TREND_FAST = 50    # 4H
EMA_TREND_SLOW = 200   # 4H
STOP_MULT = 3.0        # 3 x ATR14
CONFIRM_CANDLES = 2    # 2 candle konfirmasi exit
TOP_N_PAIRS = 100      # Top 100 pairs by volume

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
        "active_position": None,  # {"symbol": "...", "entry": ..., "stop": ..., "below_count": 0, ...}
        "month_entries": 0,
        "current_month": now.month,
        "last_stop_time": None,  # Untuk cooldown global
        "pair_states": {}  # Track indikator per pair
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
# OKX API: GET TOP 100 PAIRS
# ============================================================
def get_top_pairs(limit=100):
    """Ambil Top N pairs USDT by volume 24h dari OKX"""
    url = "https://www.okx.com/api/v5/market/tickers?instType=SWAP&uly=BTC-USDT"  # Crypto swaps
    req = urllib.request.Request(url, headers={"User-Agent": "AlphaBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read())
        if res.get("code") != "0": raise Exception(f"OKX Error: {res.get('msg')}")
        
        tickers = res.get("data", [])
        # Filter hanya USDT pairs dan volume valid
        usdt_pairs = []
        for t in tickers:
            inst_id = t.get("instId", "")
            if "-USDT" in inst_id and "-SWAP" in inst_id:
                vol_24h = float(t.get("vol24h", 0) or 0)
                last_price = float(t.get("last", 0) or 0)
                if vol_24h > 0 and last_price > 0:
                    usdt_pairs.append((vol_24h, inst_id))
        
        # Sort by volume dan ambil top N
        usdt_pairs.sort(reverse=True, key=lambda x: x[0])
        top_pairs = [pair[1] for pair in usdt_pairs[:limit]]
        print(f"[OKX] Top {len(top_pairs)} pairs: {top_pairs[:5]}...")
        return top_pairs

def fetch_klines(inst_id, bar="15m", limit=1000):
    """Fetch OHLCV dari OKX dengan pagination"""
    all_candles = []
    after = ""
    target = limit + 100
    
    while len(all_candles) < target:
        url = f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar={bar}&limit=100"
        if after: url += f"&after={after}"
        
        req = urllib.request.Request(url, headers={"User-Agent": "AlphaBot/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read())
            if res.get("code") != "0": raise Exception(f"OKX Error: {res.get('msg')}")
            data = res.get("data", [])
            if not data: break
            
            closed = [c for c in data if c[8] == "1"]  # confirm="1"
            all_candles.extend(closed)
            after = data[-1][0]
        time.sleep(0.1)

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

def calc_atr_wilder(high, low, close, period=14):
    n = len(close)
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    
    atr = [None] * n
    if n >= period:
        atr[period - 1] = sum(tr[:period]) / period
        for i in range(period, n):
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
    return atr

def map_4h_to_15m(ema4h_list, ts4h_list, ts15m_list):
    """Map EMA 4H ke timeline 15M"""
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
# SCANNER PER PAIR
# ============================================================
def scan_pair(inst_id, state):
    """Scan satu pair, return sinyal jika valid"""
    try:
        data_15m = fetch_klines(inst_id, "15m", 1000)
        data_4h = fetch_klines(inst_id, "4H", 300)
    except Exception as e:
        print(f"[{inst_id}] Error fetch: {e}")
        return None
    
    if len(data_15m) < HIGH_LOOKBACK + 50 or len(data_4h) < 250:
        return None
    
    # Parse data
    ts_15m = [c[0] for c in data_15m]
    o_15m = [c[1] for c in data_15m]
    h_15m = [c[2] for c in data_15m]
    l_15m = [c[3] for c in data_15m]
    c_15m = [c[4] for c in data_15m]
    v_15m = [c[5] for c in data_15m]
    
    close_4h = [c[4] for c in data_4h]
    ts_4h = [c[0] for c in data_4h]
    
    # Hitung indikator
    atr14 = calc_atr_wilder(h_15m, l_15m, c_15m, ATR_PERIOD)
    ema50_4h = calc_ema(close_4h, EMA_TREND_FAST)
    ema200_4h = calc_ema(close_4h, EMA_TREND_SLOW)
    
    ema50_15m = map_4h_to_15m(ema50_4h, ts_4h, ts_15m)
    ema200_15m = map_4h_to_15m(ema200_4h, ts_4h, ts_15m)
    
    t_sig = len(data_15m) - 2
    if t_sig < HIGH_LOOKBACK + 10:
        return None
    
    # === CEK SYARAT ENTRY (TURBO VIP) ===
    
    # 1. Filter Tren 4H
    if ema50_15m[t_sig] is None or ema200_15m[t_sig] is None:
        return None
    if not (ema50_15m[t_sig] > ema200_15m[t_sig]):
        return None
    
    # 2. Breakout 15M
    high96 = max(h_15m[t_sig - HIGH_LOOKBACK : t_sig])
    if not (c_15m[t_sig] > high96):
        return None
    
    # 3. Volume
    vol48 = sum(v_15m[t_sig - VOL_LOOKBACK : t_sig]) / VOL_LOOKBACK
    if not (v_15m[t_sig] > vol48):
        return None
    
    # 4. Alignment
    if not (c_15m[t_sig] > ema50_15m[t_sig]):
        return None
    
    # 5. ATR Valid
    if atr14[t_sig] is None or atr15m[t_sig] <= 0:
        return None
    
    # 6. Cooldown & Kuota
    if state["active_position"] is not None:
        return None
    
    # Cek cooldown global (12 jam dari last stop)
    if state["last_stop_time"]:
        last_stop_dt = datetime.fromisoformat(state["last_stop_time"].replace('Z', '+00:00'))
        hours_since = (datetime.now(timezone.utc) - last_stop_dt).total_seconds() / 3600
        if hours_since < 12:
            return None
    
    if state["month_entries"] >= MAX_MONTHLY:
        return None
    
    # === SIGNAL VALID ===
    entry_price = o_15m[t_sig + 1]
    stop_distance = STOP_MULT * atr14[t_sig]
    stop_price = entry_price - stop_distance
    
    return {
        "symbol": inst_id,
        "entry": entry_price,
        "stop": stop_price,
        "atr": atr15m[t_sig],
        "high96": high96,
        "vol_ratio": v_15m[t_sig] / vol48,
        "close_sig": c_15m[t_sig],
        "ema50_4h": ema50_15m[t_sig]
    }

# ============================================================
# MONITOR POSISI AKTIF
# ============================================================
def monitor_active_position(state):
    """Monitor posisi aktif untuk exit"""
    if state["active_position"] is None:
        return False, None
    
    pos = state["active_position"]
    inst_id = pos["symbol"]
    
    try:
        data_15m = fetch_klines(inst_id, "15m", 100)
        data_4h = fetch_klines(inst_id, "4H", 50)
    except Exception as e:
        print(f"[MONITOR {inst_id}] Error: {e}")
        return False, None
    
    if len(data_15m) < 10 or len(data_4h) < 10:
        return False, None
    
    ts_15m = [c[0] for c in data_15m]
    o_15m = [c[1] for c in data_15m]
    l_15m = [c[3] for c in data_15m]
    c_15m = [c[4] for c in data_15m]
    
    close_4h = [c[4] for c in data_4h]
    ts_4h = [c[0] for c in data_4h]
    
    ema50_4h = calc_ema(close_4h, EMA_TREND_FAST)
    ema50_15m = map_4h_to_15m(ema50_4h, ts_4h, ts_15m)
    
    t_curr = len(data_15m) - 2  # Candle terakhir yang close
    
    # Cek Stop Loss
    if l_15m[t_curr] <= pos["stop"]:
        exit_price = min(o_15m[t_curr + 1], pos["stop"])
        return True, {
            "symbol": inst_id,
            "exit_price": exit_price,
            "reason": "STOP_HIT",
            "entry": pos["entry"],
            "stop": pos["stop"],
            "atr": pos["atr"]
        }
    
    # Cek Exit Tren (2 candle berturut-turut di bawah EMA50 4H)
    if ema50_15m[t_curr] is not None:
        if c_15m[t_curr] < ema50_15m[t_curr]:
            pos["below_count"] = pos.get("below_count", 0) + 1
        else:
            pos["below_count"] = 0
        
        if pos["below_count"] >= CONFIRM_CANDLES:
            exit_price = o_15m[t_curr + 1]
            return True, {
                "symbol": inst_id,
                "exit_price": exit_price,
                "reason": "TREND_EXIT_2C",
                "entry": pos["entry"],
                "stop": pos["stop"],
                "atr": pos["atr"]
            }
    
    return False, None

# ============================================================
# MAIN EXECUTION
# ============================================================
def run_bot():
    print(f"[{datetime.now(timezone.utc).isoformat()}] ALPHA 1200 TURBO VIP SCAN (Top 100)...")
    
    state = load_state()
    
    # Reset bulanan
    now_utc = datetime.now(timezone.utc)
    if state["current_month"] != now_utc.month:
        state["month_entries"] = 0
        state["current_month"] = now_utc.month
        save_state(state)
        print(f"[INFO] Reset kuota bulan ke 0")
    
    # 1. Monitor posisi aktif terlebih dahulu
    exited, exit_data = monitor_active_position(state)
    if exited and exit_data:
        # Hitung R_net
        fee_entry = exit_data["entry"] * FEE_PCT
        fee_exit = exit_data["exit_price"] * FEE_PCT
        stop_dist = exit_data["entry"] - exit_data["stop"]
        r_net = (exit_data["exit_price"] - exit_data["entry"] - fee_entry - fee_exit) / stop_dist
        
        # Update state
        state["active_position"] = None
        state["last_stop_time"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        
        # Jurnal & Notif
        append_journal({
            "time": now_utc.isoformat(),
            "symbol": exit_data["symbol"],
            "side": "LONG",
            "entry": exit_data["entry"],
            "exit": exit_data["exit_price"],
            "reason": exit_data["reason"],
            "R_net": round(r_net, 4)
        })
        
        emoji = "✅" if r_net > 0 else ""
        tg_send(f"{emoji} <b>ALPHA TURBO VIP EXIT</b>\n\nPair: <code>{exit_data['symbol']}</code>\nAlasan: <b>{exit_data['reason']}</b>\nEntry: <code>{exit_data['entry']:,.2f}</code>\nExit: <code>{exit_data['exit_price']:,.2f}</code>\nR_net: <b>{r_net:+.3f}R</b>")
        print(f"EXIT: {exit_data['symbol']} | {exit_data['reason']} | R: {r_net:+.3f}")
        return
    
    # 2. Scan semua top pairs
    try:
        top_pairs = get_top_pairs(TOP_N_PAIRS)
    except Exception as e:
        print(f"[ERROR] Gagal ambil top pairs: {e}")
        return
    
    signals = []
    for inst_id in top_pairs:
        sig = scan_pair(inst_id, state)
        if sig:
            signals.append(sig)
            print(f"[SIGNAL] {inst_id}: Entry {sig['entry']:,.2f} | Vol ratio {sig['vol_ratio']:.2f}x")
    
    if not signals:
        print("[INFO] Tidak ada sinyal valid.")
        return
    
    # 3. Pilih sinyal terbaik (volume ratio tertinggi)
    signals.sort(key=lambda x: x["vol_ratio"], reverse=True)
    best_sig = signals[0]
    
    # 4. Eksekusi entry
    state["active_position"] = {
        "symbol": best_sig["symbol"],
        "entry": best_sig["entry"],
        "stop": best_sig["stop"],
        "atr": best_sig["atr"],
        "below_count": 0,
        "entry_time": now_utc.isoformat()
    }
    state["month_entries"] += 1
    save_state(state)
    
    # Notifikasi
    tg_send(f" <b>ALPHA TURBO VIP ENTRY</b>\n\nPair: <code>{best_sig['symbol']}</code>\nEntry: <code>{best_sig['entry']:,.2f}</code>\nStop: <code>{best_sig['stop']:,.2f}</code>\nATR14: <code>{best_sig['atr']:,.2f}</code>\nVol Ratio: {best_sig['vol_ratio']:.2f}x\nKuota: {state['month_entries']}/{MAX_MONTHLY}")
    print(f"ENTRY: {best_sig['symbol']} @ {best_sig['entry']:,.2f} | SL: {best_sig['stop']:,.2f}")

if __name__ == "__main__":
    run_bot()
