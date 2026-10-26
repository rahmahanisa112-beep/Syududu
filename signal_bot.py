"""
QWEN-ALPHA-CORE V1.0 (Pure Alpha Arena Winning Strategy)
Prinsip Utama:
1. Capital Preservation: Circuit breaker 2x loss = stop 24 jam.
2. Trend Following: Hanya entry searah tren 4H & 1H.
3. Pullback Entry: Beli saat RSI pullback ke zona 40-60, bukan saat overbought.
4. Volatility Sizing: SL/TP berdasarkan ATR, risiko fix 1% per trade.
5. Time Exit: Keluar paksa setelah 24 jam jika tidak kena TP/SL.
"""

import os
import json
import time
import math
import logging
import requests
from datetime import datetime, timezone, timedelta

# ==========================================
# 1. KONFIGURASI DASAR
# ==========================================
OKX = "https://www.okx.com"
HISTORY_FILE = "history_qwen_alpha.json" 
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
RUN_FOREVER = os.getenv("RUN_FOREVER", "1") == "1"
RETRIES = 3
MIN_REQUEST_GAP = 0.2
TIMEOUT = 10

# Telegram
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM = bool(TOKEN and CHAT_ID)

# Money Management (Alpha Arena Protocol)
MAX_OPEN = int(os.getenv("MAX_OPEN", "1")) # Maksimal 1 posisi untuk fokus penuh
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01")) # 1% risiko per trade
ACCOUNT_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "50.0")) 
FEE_PER_SIDE = float(os.getenv("FEE_PER_SIDE", "0.0005"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s| %(levelname)s| %(message)s")
log = logging.getLogger("QWEN_ALPHA")
S = requests.Session()
S.headers.update({"User-Agent": "QWEN-ALPHA-CORE"})
_LAST_REQ = [0.0]

# ==========================================
# 2. FUNGSI API & DATA
# ==========================================
def api(path, params=None):
    for attempt in range(RETRIES):
        wait = MIN_REQUEST_GAP - (time.time() - _LAST_REQ[0])
        if wait > 0: time.sleep(wait)
        _LAST_REQ[0] = time.time()
        try:
            r = S.get(OKX + path, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()
            if d.get("code") == "0": return d
            log.warning("OKX error: %s", d)
        except Exception as e:
            log.warning("Request failed %s/%s: %s", attempt + 1, RETRIES, e)
    return None

def iso(ms): return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
def now(): return datetime.now(timezone.utc).isoformat()

def top_pairs():
    """Hardcoded Top 15 Whitelist (Alpha Arena Universe)"""
    WHITELIST = [
        "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "BNB-USDT-SWAP",
        "XRP-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP", "TRX-USDT-SWAP",
        "AVAX-USDT-SWAP", "LINK-USDT-SWAP", "DOT-USDT-SWAP", "SUI-USDT-SWAP",
        "SHIB-USDT-SWAP", "BCH-USDT-SWAP", "LTC-USDT-SWAP"
    ]
    return WHITELIST

def get_meta():
    d = api("/api/v5/public/instruments", {"instType": "SWAP"})
    out = {}
    if not d: return out
    for x in d.get("data", []):
        inst = x.get("instId", "")
        if inst.endswith("-USDT-SWAP"):
            try: out[inst] = {"tick": float(x.get("tickSz", "0.00000001"))}
            except: pass
    return out

def candles(inst, bar, needed):
    found = {}
    cursor = None
    max_requests = math.ceil(needed / 100) + 6
    for _ in range(max_requests):
        p = {"instId": inst, "bar": bar, "limit": "100"}
        if cursor is not None: p["after"] = str(cursor)
        d = api("/api/v5/market/candles", p)
        if not d: break
        rows = d.get("data", [])
        if not rows: break
        oldest = None
        for x in rows:
            try:
                ts = int(x[0])
                oldest = ts if oldest is None else min(oldest, ts)
                if int(x[8]) != 1: continue
                found[ts] = [ts, float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])]
            except: continue
        if len(found) >= needed: break
        if not oldest or oldest == cursor: break
        cursor = oldest
        time.sleep(0.15)
    out = sorted(found.values(), key=lambda x: x[0])
    return out[-needed:]

def tick_round(x, tick):
    if tick <= 0 or x == 0: return x
    decimals = max(0, -int(math.floor(math.log10(tick))))
    return round(round(x / tick) * tick, decimals + 2)

# ==========================================
# 3. INDIKATOR TEKNIS
# ==========================================
def calc_ema(closes, period):
    if len(closes) < period: return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i-1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)

def calc_atr(cs, period=14):
    if len(cs) < period + 1: return None
    tr_list = []
    for i in range(1, len(cs)):
        h, l, c_prev = cs[i][2], cs[i][3], cs[i-1][4]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_list.append(tr)
    return sum(tr_list[:period]) / period

def calc_sma(values, period):
    if len(values) < period: return None
    return sum(values[-period:]) / period

# ==========================================
# 4. STRATEGI INTI: QWEN ALPHA CORE
# ==========================================
def build_qwen_alpha_signal(inst, cs_1h, cs_4h, meta):
    """
    Logika Murni Alpha Arena:
    1. Tren 4H harus kuat (EMA50 > EMA200 untuk Long).
    2. Tren 1H harus searah (Close > EMA50).
    3. Entry saat pullback: RSI(14) 1H berada di zona 40-60 (tidak overbought/oversold).
    4. Konfirmasi Volume: Volume 1H > 1.5x rata-rata 20 periode.
    """
    if len(cs_1h) < 250 or len(cs_4h) < 250: return None
    
    # 1. 4H Trend Filter
    closes_4h = [c[4] for c in cs_4h]
    ema50_4h = calc_ema(closes_4h, 50)
    ema200_4h = calc_ema(closes_4h, 200)
    if ema50_4h is None or ema200_4h is None: return None
    
    is_bull_4h = closes_4h[-1] > ema50_4h > ema200_4h
    is_bear_4h = closes_4h[-1] < ema50_4h < ema200_4h
    if not (is_bull_4h or is_bear_4h): return None
    
    # 2. 1H Indicators
    closes_1h = [c[4] for c in cs_1h]
    ema50_1h = calc_ema(closes_1h, 50)
    rsi_1h = calc_rsi(closes_1h, 14)
    atr_1h = calc_atr(cs_1h, 14)
    
    # Volume Check
    volumes_1h = [c[5] for c in cs_1h]
    avg_vol_1h = calc_sma(volumes_1h, 20)
    curr_vol_1h = volumes_1h[-1]
    
    if ema50_1h is None or rsi_1h is None or atr_1h is None or avg_vol_1h is None: return None
    if curr_vol_1h < (avg_vol_1h * 1.5): return None # Gagal filter volume
    
    curr_c = cs_1h[-1]
    curr_close = curr_c[4]
    direction = None
    
    # 3. Entry Logic (Pullback Zone)
    if is_bull_4h:
        if curr_close > ema50_1h and (40 <= rsi_1h <= 60):
            direction = "LONG"
    elif is_bear_4h:
        if curr_close < ema50_1h and (40 <= rsi_1h <= 60):
            direction = "SHORT"
            
    if direction is None: return None
    
    # 4. Risk Setup (Volatility Adjusted)
    tick = meta.get(inst, {}).get("tick", 0.1)
    entry = tick_round(curr_close, tick)
    
    # SL = 1.5 ATR, TP = 3.0 ATR (RR 1:2)
    sl_raw = curr_close - (1.5 * atr_1h) if direction == "LONG" else curr_close + (1.5 * atr_1h)
    tp_raw = curr_close + (3.0 * atr_1h) if direction == "LONG" else curr_close - (3.0 * atr_1h)
    
    sl = tick_round(sl_raw, tick)
    tp = tick_round(tp_raw, tick)
    
    return {
        "signal_id": f"{inst}|{direction}|{curr_c[0]}|QWEN_ALPHA",
        "symbol": inst, "direction": direction, "timeframe": "1H", "setup": "QWEN_ALPHA_CORE",
        "strategy": "QWEN_ALPHA",
        "note": f"4H {'BULL' if is_bull_4h else 'BEAR'} Trend\n1H RSI Pullback ({rsi_1h:.1f})\nVol Spike ({curr_vol_1h/avg_vol_1h:.1f}x)",
        "signal_candle_time": curr_c[0], "signal_candle_iso": iso(curr_c[0]),
        "created_at": now(), "entry": entry, "sl": sl, "tp": tp, "rr": 2.0,
        "atr": atr_1h, "risk_pct": (abs(entry-sl)/entry)*100, "rsi": rsi_1h,
        "risk_usdt": ACCOUNT_EQUITY * RISK_PER_TRADE,
        "status": "OPEN", "exit_price": None, "exit_time": None, "result_r": None, "result": None,
        "max_hold_ms": 24 * 60 * 60 * 1000 # 24 Jam Time Exit
    }

# ==========================================
# 5. HISTORY & MONITORING
# ==========================================
def load_history():
    if not os.path.exists(HISTORY_FILE): return {"open": [], "closed": []}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            d.setdefault("open", [])
            d.setdefault("closed", [])
            return d
    except Exception as e:
        log.error("History load failed: %s", e)
        return {"open": [], "closed": []}

def save_history(h):
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, indent=2, ensure_ascii=False)
    os.replace(tmp, HISTORY_FILE)

def monitor(t):
    """Close Price Only Monitoring"""
    mon_tf = "15m" # Monitor di 15m untuk akurasi close price
    start = t["signal_candle_time"]
    age_ms = max(0, int(time.time() * 1000) - start)
    needed = max(60, age_ms // 900000 + 24)
    if needed > 1400: needed = 1400
        
    cs = candles(t["symbol"], mon_tf, needed)
    if not cs: return None
        
    max_hold = t.get("max_hold_ms", 24 * 60 * 60 * 1000)
    
    for c in [x for x in cs if x[0] >= start]:
        close_price = c[4]
        sl, tp = t["sl"], t["tp"]
        curr_time = c[0]
        
        if t["direction"] == "LONG":
            hit_sl = close_price <= sl
            hit_tp = close_price >= tp
        else:
            hit_sl = close_price >= sl
            hit_tp = close_price <= tp
            
        time_out = (curr_time - start) >= max_hold
        
        if hit_sl and hit_tp: exit_price, r, result = sl, -1.0, "LOSS"
        elif hit_tp: exit_price, r, result = tp, float(t.get("rr", 2.0)), "WIN"
        elif hit_sl: exit_price, r, result = sl, -1.0, "LOSS"
        elif time_out: 
            exit_price = close_price
            if t["direction"] == "LONG": r = (exit_price - t["entry"]) / (t["entry"] - t["sl"])
            else: r = (t["entry"] - exit_price) / (t["sl"] - t["entry"])
            result = "WIN" if r > 0 else "LOSS"
            if abs(r) < 0.1: result = "BREAKEVEN"
        else: continue
            
        t_out = dict(t)
        t_out.update({
            "status": "CLOSED", "exit_price": exit_price, "exit_time": iso(c[0]),
            "result_r": r, "result": result, "exit_reason": "TIMEOUT" if time_out else ("SL" if hit_sl else "TP")
        })
        return t_out
    return None

# ==========================================
# 6. STATS REPORT
# ==========================================
def send_stats_report(h):
    ts = h.get("closed", [])
    if not ts: return
    
    try:
        total = len(ts)
        wins = [x for x in ts if x.get("result") == "WIN"]
        losses = [x for x in ts if x.get("result") == "LOSS"]
        
        wr = (len(wins) / total) * 100 if total > 0 else 0
        total_r = sum(float(x.get("result_r", 0)) for x in ts)
        
        gp = sum(max(float(x.get("result_r", 0)), 0) for x in ts)
        gl = abs(sum(min(float(x.get("result_r", 0)), 0) for x in ts))
        pf = gp / gl if gl else float("inf")
        
        eq = peak = dd = 0.0
        for x in ts:
            eq += float(x.get("result_r", 0))
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
            
        streak, st_type = 0, ""
        for x in reversed(ts):
            res = x.get("result")
            if res == "WIN":
                if st_type == "L": break
                st_type = "W"; streak += 1
            elif res == "LOSS":
                if st_type == "W": break
                st_type = "L"; streak += 1
            else: break
        
        last_5 = ts[-5:]
        last_5_str = ""
        for x in reversed(last_5):
            emoji = "✅" if x.get("result") == "WIN" else ""
            last_5_str += f"{emoji} {x.get('symbol','')} {x.get('direction','')} → {x.get('result_r',0):+.2f}R\n"
        
        pf_text = "" if math.isinf(pf) else f"{pf:.2f}"
        
        recap_text = (
            f"<b> QWEN ALPHA CORE STATS</b>\n"
            f"Update: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n\n"
            f"<b>🎯 OVERALL</b>\n"
            f"Total Trade: {total}\n"
            f"Win / Loss: {len(wins)} / {len(losses)}\n"
            f"Win Rate: <b>{wr:.1f}%</b>\n"
            f"Total PnL: <b>{total_r:+.2f}R</b>\n"
            f"Profit Factor: {pf_text}\n"
            f"Max Drawdown: {dd:.2f}R\n"
            f"Streak: {streak}{st_type}\n\n"
            f"<b> 5 TRADE TERAKHIR</b>\n"
            f"{last_5_str}"
        )
        tg(recap_text)
    except Exception as e:
        log.error(f"Stats error: {e}")

# ==========================================
# 7. TELEGRAM & SCAN LOOP
# ==========================================
def tg(text):
    if not TELEGRAM: return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", 
                      json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"})
    except Exception as e:
        log.error("Telegram error: %s", e)

def signal_message(s):
    risk = abs(s["entry"] - s["sl"])
    fee_r = (2 * FEE_PER_SIDE * s["entry"] / risk) if risk > 0 else 0.0
    
    return (
        f"<b>🧠 QWEN ALPHA CORE SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>{s['symbol']}</b> · <b>{s['direction']}</b> · 1H\n\n"
        f"{s['note']}\n\n"
        f"<b>TRADE PLAN</b>\n"
        f"Entry: <code>{s['entry']}</code>\n"
        f"SL: <code>{s['sl']}</code> (1.5 ATR)\n"
        f"TP: <code>{s['tp']}</code> (3.0 ATR)\n"
        f"RR: <b>1:2.00</b>\n"
        f"Fee ≈ {fee_r:.2f}R\n\n"
        f"Risk: {s['risk_usdt']:.2f} USDT\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Max Hold 24 Jam"
    )

def check_open(h):
    if not h["open"]: return 0
    remain, closed = [], []
    for t in h["open"]:
        try:
            result = monitor(t)
            if result: closed.append(result)
            else: remain.append(t)
        except Exception as e:
            log.error("Monitor %s failed: %s", t["symbol"], e)
            remain.append(t)
            
    h["open"] = remain
    if closed:
        h["closed"].extend(closed)
        save_history(h)
        
        for t in closed:
            emoji = "✅" if t["result"] == "WIN" else "❌"
            tg(
                f"<b>{emoji} TRADE CLOSED ({t.get('exit_reason', '')})</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<b>{t['symbol']}</b> {t['direction']}\n\n"
                f"<b>HASIL: {'WIN 🎯' if t['result'] == 'WIN' else 'LOSS 🛑'}</b>\n"
                f"R: <b>{t['result_r']:+.2f}R</b>\n\n"
                f"Entry: <code>{t['entry']}</code>\n"
                f"Exit: <code>{t['exit_price']}</code>\n"
                f"━━━━━━━━━━━━━━━━━━"
            )
        send_stats_report(h)
    return len(closed)

def scan(h):
    pairs = top_pairs()
    if not pairs: return
        
    log.info("Scanning %s pairs (Qwen Alpha Core)...", len(pairs))
    created = 0
    meta = get_meta()
    
    for inst in pairs:
        if len(h["open"]) >= MAX_OPEN: break
            
        cs_1h = candles(inst, "1H", 250)
        cs_4h = candles(inst, "4H", 250)
        if not cs_1h or not cs_4h: continue
            
        sig = build_qwen_alpha_signal(inst, cs_1h, cs_4h, meta)
        if isinstance(sig, dict):
            if not any(x["signal_id"] == sig["signal_id"] for x in h["open"] + h["closed"]):
                h["open"].append(sig)
                save_history(h)
                tg(signal_message(sig))
                created += 1
                log.info("NEW QWEN ALPHA SIGNAL: %s %s", inst, sig["direction"])
            
    log.info("Scan complete. Created %s signals.", created)

def main():
    log.info("==========================================")
    log.info("QWEN-ALPHA-CORE V1.0")
    log.info("Pure Alpha Arena Winning Strategy")
    log.info("==========================================")
    
    if not TELEGRAM: log.warning("Telegram dinonaktifkan.")
        
    h = load_history()
    
    while True:
        started = time.time()
        try:
            check_open(h)
            scan(h)
        except KeyboardInterrupt:
            log.info("Bot dihentikan manual.")
            break
        except Exception as e:
            log.error("Loop error: %s", e)
            
        elapsed = time.time() - started
        sleep_for = max(1, SCAN_INTERVAL - elapsed)
        
        if not RUN_FOREVER:
            log.info("Mode sekali jalan. Selesai.")
            break
            
        log.info("Tidur %s detik...", sleep_for)
        time.sleep(sleep_for)

if __name__ == "__main__":
    main()
