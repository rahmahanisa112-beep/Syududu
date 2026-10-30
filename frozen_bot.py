#!/usr/bin/env python3
"""
ALPHA 1200 GOD MODE - MONSTER TREND RIDER
Spesifikasi: 30 Oktober 2026
LONG ONLY | Multi-Timeframe (4H Trend + 15M Breakout)
Data Source: OKX Public API (BTC-USDT-SWAP)
"""
import json, os, sys, time, math, urllib.request, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# KONFIGURASI
# ============================================================
INST_ID = "BTC-USDT-SWAP"
STATE_FILE = Path(__file__).parent / "alpha_state.json"
JOURNAL_FILE = Path(__file__).parent / "alpha_journal.json"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")

# Parameter Terkunci (PDF Section 2)
RISK_PCT = 0.005      # 0.5% ekuitas per trade
FEE_PCT = 0.0007      # 0.07% per sisi
MAX_MONTHLY = 15
COOLDOWN_BARS = 48    # 48 bar 15M setelah stop
ATR_PERIOD = 14
HIGH_LOOKBACK = 96
VOL_LOOKBACK = 48
EMA_TREND_FAST = 50   # 4H
EMA_TREND_SLOW = 200  # 4H
STOP_MULT = 3.0       # 3 x ATR14

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
    return {"active": False, "pending_entry": False, "entry_price": 0, "stop_price": 0, 
            "last_stop_idx": -100, "month_entries": 0, "current_month": now.month, "trade_start_idx": 0}

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
# OKX API FETCHER (Pagination)
# ============================================================
def fetch_okx_data(bar, limit):
    all_candles = []
    after = ""
    target = limit + 50
    
    while len(all_candles) < target:
        url = f"https://www.okx.com/api/v5/market/candles?instId={INST_ID}&bar={bar}&limit=100"
        if after: url += f"&after={after}"
        
        req = urllib.request.Request(url, headers={"User-Agent": "AlphaBot/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read())
            if res.get("code") != "0": raise Exception(f"OKX Error: {res.get('msg')}")
            data = res.get("data", [])
            if not data: break
            
            # Filter hanya candle closed (confirm="1")
            closed = [c for c in data if c[8] == "1"]
            all_candles.extend(closed)
            after = data[-1][0]
        time.sleep(0.2)

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
    # Wilder Smoothing: ATR[t] = (ATR[t-1] * (period-1) + TR[t]) / period
    if n >= period:
        atr[period - 1] = sum(tr[:period]) / period
        for i in range(period, n):
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
    return atr

def map_4h_ema_to_15m(ema4h_list, ts4h_list, ts15m_list):
    """Memetakan nilai EMA 4H ke timeline 15M (menggunakan EMA candle 4H terakhir yang selesai)"""
    mapped = []
    idx4h = 0
    for ts15 in ts15m_list:
        # Cari EMA 4H terakhir yang timestamp-nya <= timestamp 15M ini
        while idx4h < len(ts4h_list) - 1 and ts4h_list[idx4h + 1] <= ts15:
            idx4h += 1
        
        if ts4h_list[idx4h] <= ts15 and ema4h_list[idx4h] is not None:
            mapped.append(ema4h_list[idx4h])
        else:
            mapped.append(None)
    return mapped

# ============================================================
# LOGIKA UTAMA
# ============================================================
def run_bot():
    print(f"[{datetime.now(timezone.utc).isoformat()}] ALPHA 1200 GOD MODE SCAN...")
    
    # 1. Fetch Data
    try:
        data_4h = fetch_okx_data("4H", 300)  # Butuh ~250 untuk EMA200
        data_15m = fetch_okx_data("15m", 1000) # Butuh ~100 untuk High96 + warmup
    except Exception as e:
        print(f"[ERROR] {e}"); return

    # 2. Hitung Indikator 4H
    close_4h = [c[4] for c in data_4h]
    ema50_4h = calc_ema(close_4h, EMA_TREND_FAST)
    ema200_4h = calc_ema(close_4h, EMA_TREND_SLOW)
    ts_4h = [c[0] for c in data_4h]

    # 3. Hitung Indikator 15M
    n = len(data_15m)
    ts_15m = [c[0] for c in data_15m]
    o_15m = [c[1] for c in data_15m]
    h_15m = [c[2] for c in data_15m]
    l_15m = [c[3] for c in data_15m]
    c_15m = [c[4] for c in data_15m]
    v_15m = [c[5] for c in data_15m]

    atr14 = calc_atr_wilder(h_15m, l_15m, c_15m, ATR_PERIOD)
    
    # Map EMA 4H ke 15M
    ema50_15m = map_4h_ema_to_15m(ema50_4h, ts_4h, ts_15m)
    ema200_15m = map_4h_ema_to_15m(ema200_4h, ts_4h, ts_15m)

    # 4. Tentukan Index Candle
    # t_sig = n-2 (Candle sinyal yang baru saja close)
    # t_curr = n-1 (Candle saat ini yang sedang berjalan / Open untuk entry)
    t_sig = n - 2
    t_curr = n - 1
    
    if t_sig < HIGH_LOOKBACK + 10:
        print("[SKIP] Data belum cukup untuk warmup.")
        return

    state = load_state()
    
    # Reset Bulanan
    now_utc = datetime.now(timezone.utc)
    if state["current_month"] != now_utc.month:
        state["month_entries"] = 0
        state["current_month"] = now_utc.month

    # ============================================================
    # A. MANAJEMEN POSISI (Jika ada posisi terbuka)
    # ============================================================
    if state["active"]:
        exit_price = None
        exit_reason = ""
        
        # 1. Cek Gap (PDF Section 5.4)
        if ts_15m[t_curr] - ts_15m[t_sig] > 15 * 60 * 1000 + 60000: # Toleransi 1 menit
            exit_price = o_15m[t_curr]
            exit_reason = "GAP_OPEN"
        
        # 2. Cek Stop Loss tersentuh di candle saat ini (PDF Section 5.2)
        if not exit_reason and l_15m[t_curr] <= state["stop_price"]:
            # Exit di min(Open, Stop) untuk simulasi konservatif
            exit_price = min(o_15m[t_curr], state["stop_price"])
            exit_reason = "STOP_HIT"
        
        # 3. Cek Trend Exit dari candle sinyal terakhir (PDF Section 5.3)
        # Jika Close[t_sig] < EMA50 4H, exit di Open[t_curr]
        if not exit_reason and ema50_15m[t_sig] is not None:
            if c_15m[t_sig] < ema50_15m[t_sig]:
                exit_price = o_15m[t_curr]
                exit_reason = "TREND_EXIT"

        if exit_reason:
            # Hitung R Net
            fee_entry = state["entry_price"] * FEE_PCT
            fee_exit = exit_price * FEE_PCT
            stop_dist = state["entry_price"] - state["stop_price"] # D
            r_net = (exit_price - state["entry_price"] - fee_entry - fee_exit) / stop_dist
            
            # Update State
            state["active"] = False
            state["pending_entry"] = False
            if exit_reason == "STOP_HIT":
                state["last_stop_idx"] = t_curr
            save_state(state)
            
            # Jurnal & Notif
            append_journal({
                "time": datetime.fromtimestamp(ts_15m[t_curr]/1000, tz=timezone.utc).isoformat(),
                "side": "LONG", "entry": state["entry_price"], "exit": exit_price,
                "reason": exit_reason, "R_net": round(r_net, 4)
            })
            
            emoji = "✅" if r_net > 0 else ""
            tg_send(f"{emoji} <b>ALPHA 1200 EXIT</b>\n\nAlasan: <b>{exit_reason}</b>\nEntry: <code>{state['entry_price']:,.0f}</code>\nExit: <code>{exit_price:,.0f}</code>\nR_net: <b>{r_net:+.3f}R</b>")
            print(f"EXIT: {exit_reason} | R: {r_net:+.3f}")
            return # Selesai siklus ini

    # ============================================================
    # B. EKSEKUSI PENDING ENTRY
    # ============================================================
    if state["pending_entry"]:
        entry_price = o_15m[t_curr]
        stop_price = entry_price - (STOP_MULT * atr14[t_sig])
        
        state["active"] = True
        state["pending_entry"] = False
        state["entry_price"] = entry_price
        state["stop_price"] = stop_price
        state["trade_start_idx"] = t_curr
        state["month_entries"] += 1
        save_state(state)
        
        tg_send(f"🟢 <b>ALPHA 1200 ENTRY</b>\n\nHarga: <code>{entry_price:,.0f}</code>\nStop: <code>{stop_price:,.0f}</code>\nATR14: <code>{atr14[t_sig]:,.0f}</code>")
        print(f"ENTRY LONG @ {entry_price:,.0f} | SL: {stop_price:,.0f}")
        return

    # ============================================================
    # C. SCANNER SINYAL (PDF Section 4 & 5)
    # ============================================================
    # Cek Syarat pada Candle Sinyal (t_sig)
    
    # 1. Filter Tren 4H
    if ema50_15m[t_sig] is None or ema200_15m[t_sig] is None:
        return
    if not (ema50_15m[t_sig] > ema200_15m[t_sig]):
        return # Tren tidak bullish

    # 2. Breakout 15M (Close > Max High 96 sebelumnya)
    high96 = max(h_15m[t_sig - HIGH_LOOKBACK : t_sig])
    if not (c_15m[t_sig] > high96):
        return

    # 3. Volume 15M (Vol > Rata-rata 48 sebelumnya)
    vol48 = sum(v_15m[t_sig - VOL_LOOKBACK : t_sig]) / VOL_LOOKBACK
    if not (v_15m[t_sig] > vol48):
        return

    # 4. Alignment (Close > EMA50 4H)
    if not (c_15m[t_sig] > ema50_15m[t_sig]):
        return

    # 5. ATR Valid
    if atr14[t_sig] is None or atr14[t_sig] <= 0:
        return

    # 6. Cooldown (PDF Section 9: 48 bars setelah stop)
    if (t_sig - state["last_stop_idx"]) < COOLDOWN_BARS:
        return

    # 7. Kuota Bulanan
    if state["month_entries"] >= MAX_MONTHLY:
        return

    # SEMUA SYARAT TERPENUHI -> Jadwalkan Entry
    state["pending_entry"] = True
    save_state(state)
    
    # Kirim Notifikasi Sinyal (Akan dieksekusi di siklus berikutnya / Open candle saat ini)
    tg_send(f"🔍 <b>ALPHA 1200 SIGNAL DETECTED</b>\n\nBreakout 15M terdeteksi!\nClose: <code>{c_15m[t_sig]:,.0f}</code>\nHigh96: <code>{high96:,.0f}</code>\nVol Ratio: {v_15m[t_sig]/vol48:.2f}x\n\nMenunggu eksekusi di Open candle berikutnya...")
    print("SINYAL TERDETEKSI! Pending entry...")

if __name__ == "__main__":
    run_bot()
