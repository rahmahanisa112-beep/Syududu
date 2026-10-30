#!/usr/bin/env python3
"""
BTC 15M — FROZEN BREAKOUT V1.0 | OKX EDITION
Spesifikasi: Panduan Teknis Detail, 30 Oktober 2026
Data Source: OKX Public API (BTC-USDT-SWAP)
Fitur: Kirim sinyal + Pantau SL/Trailing otomatis + Jurnal otomatis.
"""
import json, os, sys, time, math, statistics, urllib.request, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# KONFIGURASI
# ============================================================
# OKX Instrument ID untuk Perpetual Swap
INST_ID = "BTC-USDT-SWAP" 
TIMEFRAME = "15m"

STATE_FILE = Path(__file__).parent / "frozen_state.json"
JOURNAL_FILE = Path(__file__).parent / "frozen_journal.json"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")

# Parameter V1.0 (DIKUNCI)
P_ATR_PERIOD = 14
P_RANGE48 = 48
P_EMA400 = 400
P_EMA144 = 144
P_SLOPE_LOOKBACK = 72
P_VR_MEDIAN_LOOKBACK = 192
P_RETURN16H_CANDLES = 64
P_RETURN16H_MAX = 0.02
P_VOL_MULT = 1.5
P_BREAKOUT_MULT = 0.30
P_D_MULT = 2.5
P_MAX_MONTHLY = 15
P_FEE_PCT = 0.0005

SIGNAL_HOUR_START = 0
SIGNAL_HOUR_END = 7

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
    return {"active_position": False, "current_month": now.month, "month_entries": 0, "active_trade": None}

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
# OKX API FETCHER (Dengan Pagination)
# ============================================================
def fetch_klines(limit=1500):
    """Mengambil data OHLCV dari OKX dengan pagination untuk mendapatkan 1500 candle CLOSED."""
    all_candles = []
    after = ""  # OKX 'after' = ambil data yang lebih tua dari timestamp ini
    
    print(f"[OKX] Mengambil {limit} candle untuk {INST_ID}...")
    
    # Kita fetch lebih banyak sedikit untuk memastikan kita punya cukup candle closed
    target_fetch = limit + 100 
    
    while len(all_candles) < target_fetch:
        url = f"https://www.okx.com/api/v5/market/candles?instId={INST_ID}&bar={TIMEFRAME}&limit=100"
        if after:
            url += f"&after={after}"
        
        req = urllib.request.Request(url, headers={"User-Agent": "FrozenBot/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read())
            if res.get("code") != "0":
                raise Exception(f"OKX API Error: {res.get('msg')}")
            
            data = res.get("data", [])
            if not data:
                break
            
            # OKX Format: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
            # confirm="1" berarti candle sudah close. Kita hanya ambil yang sudah close!
            closed_data = [c for c in data if c[8] == "1"]
            all_candles.extend(closed_data)
            
            # Set 'after' ke timestamp tertua di batch ini (OKX urutkan dari terbaru)
            after = data[-1][0]
            
        time.sleep(0.2) # Jeda sopan untuk rate limit OKX

    # Urutkan dari yang tertua ke terbaru (index 0 = tertua)
    all_candles.sort(key=lambda x: int(x[0]))
    
    # Konversi ke format standar bot: [ts, o, h, l, c, v]
    result = []
    for c in all_candles:
        result.append([
            int(c[0]),      # ts (ms)
            float(c[1]),    # o
            float(c[2]),    # h
            float(c[3]),    # l
            float(c[4]),    # c
            float(c[5])     # v
        ])
        
    print(f"[OKX] Berhasil mengambil {len(result)} candle closed.")
    return result[-limit:] # Kembalikan tepat sebanyak 'limit'

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

# ============================================================
# MESIN MONITORING POSISI (AUTO-EXIT)
# ============================================================
def monitor_position():
    state = load_state()
    if not state.get("active_position") or not state.get("active_trade"):
        return False

    trade = state["active_trade"]
    side = trade["side"]
    entry = trade["entry_fill"]
    current_stop = trade["current_stop"]
    D = trade["D"]
    atr_signal = trade["ATR_signal"]
    trailing_active = trade.get("trailing_active", False)
    highest_high = trade.get("highest_high", entry)
    lowest_low = trade.get("lowest_low", entry)

    try:
        raw = fetch_klines(100)
    except Exception as e:
        print(f"[MONITOR ERROR] {e}")
        return False

    n = len(raw)
    t = n - 2
    o = float(raw[t][1]); h = float(raw[t][2]); l = float(raw[t][3]); c = float(raw[t][4])
    ts_exit = int(raw[t][0])
    exit_time_utc = datetime.fromtimestamp(ts_exit / 1000, tz=timezone.utc)

    exit_price = None
    exit_reason = ""
    intrabar_ambiguous = False

    # 1. Cek Gap Open
    if side == "LONG" and o < current_stop:
        exit_price = o; exit_reason = "SL_GAP_DOWN"
    elif side == "SHORT" and o > current_stop:
        exit_price = o; exit_reason = "SL_GAP_UP"
    else:
        # 2. Cek sentuhan stop lama
        if (side == "LONG" and l <= current_stop) or (side == "SHORT" and h >= current_stop):
            exit_price = current_stop; exit_reason = "SL_HIT"
        else:
            # 3. Update ekstrem harga
            if side == "LONG": highest_high = max(highest_high, h)
            else: lowest_low = min(lowest_low, l)

            # 4. Cek trigger trailing (+6R)
            if not trailing_active:
                if (side == "LONG" and h >= entry + 6 * D) or (side == "SHORT" and l <= entry - 6 * D):
                    trailing_active = True
                    trade["trailing_activation_time"] = exit_time_utc.isoformat()
            
            # 5. Hitung trailing stop baru
            if trailing_active:
                if side == "LONG":
                    candidate_stop = highest_high - 12 * atr_signal
                    new_stop = max(current_stop, candidate_stop)
                else:
                    candidate_stop = lowest_low + 12 * atr_signal
                    new_stop = min(current_stop, candidate_stop)
                
                if (side == "LONG" and l <= new_stop) or (side == "SHORT" and h >= new_stop):
                    exit_price = new_stop
                    exit_reason = "TRAILING_HIT"
                    intrabar_ambiguous = True
                else:
                    trade["current_stop"] = new_stop
                    trade["highest_high"] = highest_high
                    trade["lowest_low"] = lowest_low
                    trade["trailing_active"] = True
                    save_state(state)
                    return False

    # === EXIT TERJADI ===
    fee_entry = P_FEE_PCT * entry
    fee_exit = P_FEE_PCT * exit_price
    side_mult = 1 if side == "LONG" else -1
    r_net = (side_mult * (exit_price - entry) - fee_entry - fee_exit) / D

    journal_entry = {
        "signal_time_utc": trade["signal_time"],
        "entry_time_utc": trade.get("entry_time", trade["signal_time"]),
        "exit_time_utc": exit_time_utc.isoformat(),
        "side": side,
        "venue": "OKX",
        "symbol": INST_ID,
        "entry_fill": entry,
        "exit_fill": exit_price,
        "initial_stop": trade["initial_stop"],
        "final_stop": exit_price,
        "D": D,
        "R_net": round(r_net, 4),
        "exit_reason": exit_reason,
        "intrabar_ambiguity_flag": intrabar_ambiguous,
        "month_entry_count": state["month_entries"]
    }
    append_journal(journal_entry)

    emoji = "✅" if r_net > 0 else ("⚪" if r_net == 0 else "❌")
    msg = (
        f"{emoji} <b>🧊 FROZEN EXIT</b>\n\n"
        f"Side: <b>{side}</b>\n"
        f"Waktu Exit: <code>{exit_time_utc.strftime('%Y-%m-%d %H:%M')}</code> UTC\n"
        f"Alasan: <b>{exit_reason}</b> {'(Ambiguous)' if intrabar_ambiguous else ''}\n\n"
        f" Entry: <code>{entry:,.2f}</code>\n"
        f" Exit: <code>{exit_price:,.2f}</code>\n"
        f"📏 D (1R): <code>{D:,.2f}</code>\n"
        f"💰 <b>R_net: {r_net:+.4f}R</b>\n\n"
        f"🔄 Posisi ditutup. Bot siap mencari sinyal baru."
    )
    tg_send(msg)
    print(f"✅ POSISI {side} DITUTUP: {exit_reason} | R_net: {r_net:+.4f}")

    state["active_position"] = False
    state["active_trade"] = None
    save_state(state)
    return True

# ============================================================
# MESIN SCANNER SINYAL
# ============================================================
def scan_signals():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Scanning for new signals...")
    try:
        raw = fetch_klines(1500)
    except Exception as e:
        print(f"[ERROR] {e}")
        tg_send(f"️ <b>FROZEN BOT ERROR</b>\n\nGagal mengambil data dari OKX.\nError: {e}")
        return

    n = len(raw)
    # Validasi gap 220 candle terakhir
    for i in range(n - 221, n - 1):
        if int(raw[i+1][0]) - int(raw[i][0]) != 900000:
            print("[SKIP] Gap data terdeteksi.")
            return

    timestamps = [int(c[0]) for c in raw]
    opens = [float(c[1]) for c in raw]
    highs = [float(c[2]) for c in raw]
    lows = [float(c[3]) for c in raw]
    closes = [float(c[4]) for c in raw]
    volumes = [float(c[5]) for c in raw]

    # TR & ATR14
    trs = [0.0]
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    
    atr = [None] * n
    for i in range(P_ATR_PERIOD - 1, n):
        atr[i] = sum(trs[i - P_ATR_PERIOD + 1:i + 1]) / P_ATR_PERIOD

    ema400 = calc_ema(closes, P_EMA400)
    ema144 = calc_ema(closes, P_EMA144)

    t = n - 2
    ts_t = timestamps[t]
    candle_time_utc = datetime.fromtimestamp(ts_t / 1000, tz=timezone.utc)

    # 1. Filter Waktu
    if not (SIGNAL_HOUR_START <= candle_time_utc.hour <= SIGNAL_HOUR_END):
        print(f"[SKIP] Di luar jam sinyal ({candle_time_utc.hour}:00 UTC)")
        return

    if atr[t] is None or atr[t] <= 0: return

    # 2. Range48 & Volume48
    hh48 = max(highs[t - P_RANGE48 : t])
    ll48 = min(lows[t - P_RANGE48 : t])
    vol48 = sum(volumes[t - P_RANGE48 : t]) / P_RANGE48

    # 3. Slope EMA144
    if ema144[t] is None or ema144[t - P_SLOPE_LOOKBACK] is None or ema144[t - P_SLOPE_LOOKBACK] == 0:
        return
    slope_144 = (ema144[t] / ema144[t - P_SLOPE_LOOKBACK]) - 1

    # 4. VR & Median VR
    vr = [None] * n
    for i in range(n):
        if atr[i] is not None and closes[i] != 0: vr[i] = atr[i] / closes[i]
    
    vr_vals = [vr[i] for i in range(t - P_VR_MEDIAN_LOOKBACK, t) if vr[i] is not None]
    if len(vr_vals) < P_VR_MEDIAN_LOOKBACK: return
    vr_median = statistics.median(vr_vals)
    vr_t = vr[t]

    # 5. Return 16 jam
    ret_16h = abs(closes[t] / closes[t - P_RETURN16H_CANDLES] - 1)

    # 6. Filter Umum
    rejections = []
    if volumes[t] <= P_VOL_MULT * vol48: rejections.append(f"Vol <= {P_VOL_MULT}x")
    if vr_t <= vr_median: rejections.append("VR <= Median")
    if ret_16h >= P_RETURN16H_MAX: rejections.append(f"Return16h >= 2%")
    
    if rejections:
        append_journal({"time": candle_time_utc.isoformat(), "status": "REJECTED", "reason": "; ".join(rejections)})
        return

    # 7. Syarat Entry
    side = None; reason = ""
    if (closes[t] > ema400[t] and closes[t] > opens[t] and closes[t] > hh48 and 
        (closes[t] - hh48) / atr[t] >= P_BREAKOUT_MULT and slope_144 < 0):
        side = "LONG"; reason = "Breakout HH48 + Bullish + Slope144 Negatif"
    elif (closes[t] < ema400[t] and closes[t] < opens[t] and closes[t] < ll48 and 
          (ll48 - closes[t]) / atr[t] >= P_BREAKOUT_MULT and slope_144 > 0):
        side = "SHORT"; reason = "Breakdown LL48 + Bearish + Slope144 Positif"

    if not side:
        append_journal({"time": candle_time_utc.isoformat(), "status": "REJECTED", "reason": "Tidak memenuhi syarat LONG/SHORT"})
        return

    # 8. Hitung Level
    D = P_D_MULT * atr[t]
    entry_price = opens[n - 1]
    stop_loss = entry_price - D if side == "LONG" else entry_price + D
    trail_trigger = entry_price + (6 * D) if side == "LONG" else entry_price - (6 * D)

    # 9. Cek Kuota Bulanan
    state = load_state()
    if state["current_month"] != candle_time_utc.month:
        state["month_entries"] = 0
        state["current_month"] = candle_time_utc.month

    if state["month_entries"] >= P_MAX_MONTHLY:
        print(f"[SKIP] Kuota sinyal bulan ini sudah {state['month_entries']}/{P_MAX_MONTHLY}.")
        append_journal({"time": candle_time_utc.isoformat(), "status": "REJECTED", "reason": "Kuota bulanan tercapai"})
        return

    # 10. Simpan State
    state["month_entries"] += 1
    state["active_position"] = True
    state["active_trade"] = {
        "signal_time": candle_time_utc.isoformat(),
        "side": side,
        "entry_fill": entry_price,
        "current_stop": stop_loss,
        "initial_stop": stop_loss,
        "D": D,
        "ATR_signal": atr[t],
        "trailing_active": False,
        "highest_high": entry_price,
        "lowest_low": entry_price
    }
    save_state(state)

    # 11. Kirim Telegram
    emoji = "🟢" if side == "LONG" else "🔴"
    msg = (
        f"{emoji} <b> FROZEN BREAKOUT V1.0 (OKX)</b>\n\n"
        f"Side: <b>{side}</b>\n"
        f"Waktu: <code>{candle_time_utc.strftime('%Y-%m-%d %H:%M')}</code> UTC\n\n"
        f" <b>Entry (Open t+1):</b> <code>{entry_price:,.2f}</code>\n"
        f"🛑 <b>Stop Loss Awal:</b> <code>{stop_loss:,.2f}</code>\n"
        f"📏 <b>D (1R = 2.5×ATR):</b> <code>{D:,.2f}</code>\n"
        f"🔄 <b>Trailing Trigger (+6R):</b> <code>{trail_trigger:,.2f}</code>\n\n"
        f"📊 <b>Indikator:</b>\n"
        f"• EMA400: {ema400[t]:,.2f} | Slope144: {slope_144*100:.3f}%\n"
        f"• HH48/LL48: {hh48:,.2f} / {ll48:,.2f}\n"
        f"• Vol ratio: {volumes[t]/vol48:.2f}× | VR: {vr_t:.5f}\n\n"
        f"📝 <b>Alasan:</b> {reason}\n"
        f"🔢 <b>Kuota Bulan:</b> {state['month_entries']}/{P_MAX_MONTHLY}\n\n"
        f"⚠️ <i>Bot akan memantau SL/Trailing otomatis via OKX.</i>"
    )
    tg_send(msg)
    print(f"✅ SINYAL {side} TERKIRIM & POSISI DIKUNCI!")

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    exited = monitor_position()
    if not exited:
        scan_signals()
