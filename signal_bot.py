"""
ALPHAQUANT MULTI-STRATEGY BOT V1.1 (Fixed)
Strategy 1: SCALP 15M STRICT (RSI 7, ATR Ratio, RR 1:3)
Strategy 2: ILM INTRADAY (EMA 200/50, Stoch RSI, Time Filter London/NY, RR 1:1.75)
+ Auto Stats Report setiap trade closed
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
HISTORY_FILE = "history_multi.json" 
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
RUN_FOREVER = os.getenv("RUN_FOREVER", "1") == "1"
RETRIES = 3
MIN_REQUEST_GAP = 0.2
TIMEOUT = 10

# Telegram
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM = bool(TOKEN and CHAT_ID)

# Money Management
MAX_OPEN = int(os.getenv("MAX_OPEN", "5"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))
ACCOUNT_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "1000.0"))
FEE_PER_SIDE = float(os.getenv("FEE_PER_SIDE", "0.0005"))
TOP_PAIRS = int(os.getenv("TOP_PAIRS", "50"))
MIN_VOLUME_USDT = float(os.getenv("MIN_VOLUME_USDT", "3000000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s| %(levelname)s| %(message)s")
log = logging.getLogger("MULTI_BOT")
S = requests.Session()
S.headers.update({"User-Agent": "ALPHAQUANT-MULTI-V1.1"})
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
    d = api("/api/v5/market/tickers", {"instType": "SWAP"})
    if not d: return []
    a = []
    for x in d.get("data", []):
        inst = x.get("instId", "")
        if not inst.endswith("-USDT-SWAP"): continue
        try: v = float(x.get("volCcy24h", 0))
        except: continue
        if v >= MIN_VOLUME_USDT: a.append((inst, v))
    a.sort(key=lambda x: x[1], reverse=True)
    return [x[0] for x in a[:TOP_PAIRS]]

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

def calc_rsi_list(closes, period=14):
    rsi_list = [None] * len(closes)
    if len(closes) < period + 1: return rsi_list
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i-1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0: rsi_list[period] = 100.0
    else: rsi_list[period] = 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0: rsi_list[i+1] = 100.0
        else: rsi_list[i+1] = 100 - 100 / (1 + avg_gain / avg_loss)
    return rsi_list

def calc_atr_list(cs, period=14):
    atr_list = [None] * len(cs)
    if len(cs) < period + 1: return atr_list
    tr_list = []
    for i in range(1, len(cs)):
        h, l, c_prev = cs[i][2], cs[i][3], cs[i-1][4]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_list.append(tr)
    atr = sum(tr_list[:period]) / period
    atr_list[period] = atr
    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period
        atr_list[i+1] = atr
    return atr_list

def calc_sma(values, period):
    if len(values) < period: return None
    return sum(values[-period:]) / period

def calc_stoch_rsi(closes, rsi_period=14, stoch_period=14, k_smooth=3, d_smooth=3):
    rsi_vals = calc_rsi_list(closes, rsi_period)
    valid_rsi = [x for x in rsi_vals if x is not None]
    if len(valid_rsi) < stoch_period: return None, None
    
    stoch_k_raw = []
    for i in range(len(rsi_vals)):
        if rsi_vals[i] is None:
            stoch_k_raw.append(None)
            continue
        window = []
        for j in range(max(0, i - stoch_period + 1), i + 1):
            if rsi_vals[j] is not None:
                window.append(rsi_vals[j])
        if len(window) < stoch_period:
            stoch_k_raw.append(None)
            continue
        min_rsi = min(window)
        max_rsi = max(window)
        if max_rsi == min_rsi: stoch_k_raw.append(50.0)
        else: stoch_k_raw.append(100 * (rsi_vals[i] - min_rsi) / (max_rsi - min_rsi))
        
    k_list = [None] * len(stoch_k_raw)
    valid_k = [x for x in stoch_k_raw if x is not None]
    if len(valid_k) < k_smooth: return None, None
    
    for i in range(len(stoch_k_raw)):
        if stoch_k_raw[i] is None: continue
        window = []
        for j in range(max(0, i - k_smooth + 1), i + 1):
            if stoch_k_raw[j] is not None: window.append(stoch_k_raw[j])
        if len(window) == k_smooth: k_list[i] = sum(window) / k_smooth
        
    d_list = [None] * len(k_list)
    valid_k2 = [x for x in k_list if x is not None]
    if len(valid_k2) < d_smooth: return None, None
    
    for i in range(len(k_list)):
        if k_list[i] is None: continue
        window = []
        for j in range(max(0, i - d_smooth + 1), i + 1):
            if k_list[j] is not None: window.append(k_list[j])
        if len(window) == d_smooth: d_list[i] = sum(window) / d_smooth
        
    return k_list[-1], d_list[-1]

# ==========================================
# 4. STRATEGY 1: SCALP 15M STRICT
# ==========================================
def build_scalp_signal(inst, cs, meta):
    if len(cs) < 300: return None
    closes = [c[4] for c in cs]
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200)
    rsi_list = calc_rsi_list(closes, 7)
    atr_list = calc_atr_list(cs, 14)
    
    curr_c = cs[-1]
    curr_close = curr_c[4]
    curr_rsi = rsi_list[-1]
    prev_rsi = rsi_list[-2]
    curr_atr = atr_list[-1]
    
    if curr_rsi is None or prev_rsi is None or curr_atr is None: return None
    if ema50 is None or ema200 is None: return None
    
    valid_atrs = [x for x in atr_list[-60:] if x is not None]
    if len(valid_atrs) < 50: return None
    sma_atr = calc_sma(valid_atrs, 50)
    if sma_atr is None or sma_atr == 0: return None
    atr_ratio = curr_atr / sma_atr
    if atr_ratio <= 1.20: return None 
    
    direction = None
    if curr_close > ema50 and curr_close > ema200:
        if curr_rsi > 30 and prev_rsi <= 30:
            direction = "LONG"
    elif curr_close < ema50 and curr_close < ema200:
        if curr_rsi < 70 and prev_rsi >= 70:
            direction = "SHORT"
            
    if direction is None: return None
    
    tick = meta.get(inst, {}).get("tick", 0.1)
    entry = tick_round(curr_close, tick)
    sl_raw = curr_close - (1.0 * curr_atr) if direction == "LONG" else curr_close + (1.0 * curr_atr)
    tp_raw = curr_close + (3.0 * curr_atr) if direction == "LONG" else curr_close - (3.0 * curr_atr)
    sl = tick_round(sl_raw, tick)
    tp = tick_round(tp_raw, tick)
    
    return {
        "signal_id": f"{inst}|{direction}|{curr_c[0]}|SCALP",
        "symbol": inst, "direction": direction, "timeframe": "15m", "setup": "SCALP_15M",
        "strategy": "SCALP",
        "note": f"RSI(7) Cross · ATR Ratio {atr_ratio:.2f}\nTrend: {'BULL' if direction=='LONG' else 'BEAR'}",
        "signal_candle_time": curr_c[0], "signal_candle_iso": iso(curr_c[0]),
        "created_at": now(), "entry": entry, "sl": sl, "tp": tp, "rr": 3.0,
        "atr": curr_atr, "risk_pct": (abs(entry-sl)/entry)*100, "rsi": curr_rsi,
        "risk_usdt": ACCOUNT_EQUITY * RISK_PER_TRADE,
        "status": "OPEN", "exit_price": None, "exit_time": None, "result_r": None, "result": None,
        "max_hold_ms": 12 * 15 * 60 * 1000
    }

# ==========================================
# 5. STRATEGY 2: ILM INTRADAY
# ==========================================
def is_valid_ilm_time(timestamp_ms):
    dt_utc = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    dt_wib = dt_utc + timedelta(hours=7)
    time_val = dt_wib.hour * 60 + dt_wib.minute
    
    if (840 <= time_val <= 1020) or (1170 <= time_val <= 1350):
        return True
    return False

def build_ilm_signal(inst, cs_15m, cs_1h, meta):
    if len(cs_15m) < 250 or len(cs_1h) < 250: return None
    
    if not is_valid_ilm_time(cs_15m[-1][0]): return None
    
    closes_1h = [c[4] for c in cs_1h]
    ema200_1h = calc_ema(closes_1h, 200)
    if ema200_1h is None: return None
    last_close_1h = closes_1h[-1]
    
    is_bull_1h = last_close_1h > ema200_1h
    is_bear_1h = last_close_1h < ema200_1h
    if not (is_bull_1h or is_bear_1h): return None
    
    closes_15m = [c[4] for c in cs_15m]
    ema50_15m = calc_ema(closes_15m, 50)
    atr_list = calc_atr_list(cs_15m, 14)
    curr_atr = atr_list[-1]
    
    if ema50_15m is None or curr_atr is None: return None
    
    k_curr, d_curr = calc_stoch_rsi(closes_15m, 14, 14, 3, 3)
    k_prev, d_prev = calc_stoch_rsi(closes_15m[:-1], 14, 14, 3, 3)
    
    if k_curr is None or d_curr is None or k_prev is None: return None
    
    curr_c = cs_15m[-1]
    curr_close = curr_c[4]
    
    direction = None
    
    if is_bull_1h:
        if abs(curr_close - ema50_15m) / ema50_15m > 0.005: return None
        if k_prev < 20 and k_curr > d_curr and k_prev <= d_prev:
            direction = "LONG"
            
    elif is_bear_1h:
        if abs(curr_close - ema50_15m) / ema50_15m > 0.005: return None
        if k_prev > 80 and k_curr < d_curr and k_prev >= d_prev:
            direction = "SHORT"
            
    if direction is None: return None
    
    tick = meta.get(inst, {}).get("tick", 0.1)
    entry = tick_round(curr_close, tick)
    sl_raw = curr_close - (1.2 * curr_atr) if direction == "LONG" else curr_close + (1.2 * curr_atr)
    tp_raw = curr_close + (2.1 * curr_atr) if direction == "LONG" else curr_close - (2.1 * curr_atr)
    sl = tick_round(sl_raw, tick)
    tp = tick_round(tp_raw, tick)
    
    return {
        "signal_id": f"{inst}|{direction}|{curr_c[0]}|ILM",
        "symbol": inst, "direction": direction, "timeframe": "15m", "setup": "ILM_INTRADAY",
        "strategy": "ILM",
        "note": f"1H {'BULL' if is_bull_1h else 'BEAR'} · Stoch RSI Cross\nPrice @ EMA50 15m",
        "signal_candle_time": curr_c[0], "signal_candle_iso": iso(curr_c[0]),
        "created_at": now(), "entry": entry, "sl": sl, "tp": tp, "rr": 1.75,
        "atr": curr_atr, "risk_pct": (abs(entry-sl)/entry)*100, "stoch_k": k_curr,
        "risk_usdt": ACCOUNT_EQUITY * RISK_PER_TRADE,
        "status": "OPEN", "exit_price": None, "exit_time": None, "result_r": None, "result": None,
        "max_hold_ms": 16 * 15 * 60 * 1000
    }

# ==========================================
# 6. HISTORY & MONITORING
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
    mon_tf = "5m"
    start = t["signal_candle_time"]
    age_ms = max(0, int(time.time() * 1000) - start)
    needed = max(60, age_ms // 300000 + 24)
    if needed > 1400: needed = 1400
        
    cs = candles(t["symbol"], mon_tf, needed)
    if not cs: return None
        
    max_hold = t.get("max_hold_ms", 3 * 60 * 60 * 1000)
    
    for c in [x for x in cs if x[0] >= start]:
        hi, lo = c[2], c[3]
        sl, tp = t["sl"], t["tp"]
        curr_time = c[0]
        
        hit_sl = (lo <= sl) if t["direction"] == "LONG" else (hi >= sl)
        hit_tp = (hi >= tp) if t["direction"] == "LONG" else (lo <= tp)
        time_out = (curr_time - start) >= max_hold
        
        if hit_sl and hit_tp: exit_price, r, result = sl, -1.0, "LOSS"
        elif hit_tp: exit_price, r, result = tp, float(t.get("rr", 1.5)), "WIN"
        elif hit_sl: exit_price, r, result = sl, -1.0, "LOSS"
        elif time_out: 
            exit_price = c[4] 
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
# 7. STATS REPORT (FIXED)
# ==========================================
def send_stats_report(h):
    """Kirim laporan stats setiap kali ada trade closed"""
    ts = h.get("closed", [])
    if not ts:
        log.info("No closed trades yet, skipping stats report.")
        return
    
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
            
        # Streak
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
        
        # Per Strategy
        scalp_trades = [x for x in ts if x.get("strategy") == "SCALP"]
        ilm_trades = [x for x in ts if x.get("strategy") == "ILM"]
        
        def strategy_stats(trades_list, name):
            if not trades_list: return ""
            t = len(trades_list)
            w = len([x for x in trades_list if x.get("result") == "WIN"])
            r = sum(float(x.get("result_r", 0)) for x in trades_list)
            wr_s = (w / t) * 100 if t > 0 else 0
            return f"• {name}: {t}t ({w}W/{t-w}L) → {r:+.2f}R (WR {wr_s:.0f}%)\n"
        
        strategy_report = strategy_stats(scalp_trades, "SCALP 15M") + strategy_stats(ilm_trades, "ILM INTRADAY")
        
        # 5 Trade Terakhir
        last_5 = ts[-5:]
        last_5_str = ""
        for x in reversed(last_5):
            emoji = "✅" if x.get("result") == "WIN" else "❌"
            last_5_str += f"{emoji} {x.get('symbol','')} {x.get('direction','')} [{x.get('strategy','')}] → {x.get('result_r',0):+.2f}R\n"
        
        pf_text = "" if math.isinf(pf) else f"{pf:.2f}"
        
        recap_text = (
            f"<b>📊 MULTI-STRATEGY STATS</b>\n"
            f"Update: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n\n"
            f"<b>🎯 OVERALL</b>\n"
            f"Total Trade: {total}\n"
            f"Win / Loss: {len(wins)} / {len(losses)}\n"
            f"Win Rate: <b>{wr:.1f}%</b>\n"
            f"Total PnL: <b>{total_r:+.2f}R</b>\n"
            f"Profit Factor: {pf_text}\n"
            f"Max Drawdown: {dd:.2f}R\n"
            f"Streak: {streak}{st_type}\n\n"
            f"<b>📈 PER STRATEGY</b>\n"
            f"{strategy_report}\n"
            f"<b>🕒 5 TRADE TERAKHIR</b>\n"
            f"{last_5_str}"
        )
        
        tg(recap_text)
        log.info("Stats report sent successfully.")
        
    except Exception as e:
        log.error(f"Error sending stats report: {e}")
        import traceback
        log.error(traceback.format_exc())

# ==========================================
# 8. TELEGRAM & SCAN LOOP
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
    
    if s["strategy"] == "SCALP":
        title = "🚨 SCALP 15M STRICT"
        rr_text = "1:3.00"
        hold_text = "Max Hold 3 Jam"
    else:
        title = "🏛️ ILM INTRADAY HIGH EDGE"
        rr_text = "1:1.75"
        hold_text = "Max Hold 4 Jam (London/NY Only)"
        
    return (
        f"<b>{title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>{s['symbol']}</b> · <b>{s['direction']}</b> · 15m\n\n"
        f"{s['note']}\n\n"
        f"<b>TRADE PLAN</b>\n"
        f"Entry: <code>{s['entry']}</code>\n"
        f"SL: <code>{s['sl']}</code>\n"
        f"TP: <code>{s['tp']}</code>\n"
        f"RR: <b>{rr_text}</b>\n"
        f"Fee ≈ {fee_r:.2f}R\n\n"
        f"Risk: {s['risk_usdt']:.2f} USDT\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ {hold_text}"
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
        
        # Kirim notifikasi per trade
        for t in closed:
            emoji = "✅" if t["result"] == "WIN" else "❌"
            tg(
                f"<b>{emoji} TRADE CLOSED ({t.get('exit_reason', '')})</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<b>{t['symbol']}</b> {t['direction']} [{t.get('strategy','')}]\n\n"
                f"<b>HASIL: {'WIN ' if t['result'] == 'WIN' else 'LOSS '}</b>\n"
                f"R: <b>{t['result_r']:+.2f}R</b>\n\n"
                f"Entry: <code>{t['entry']}</code>\n"
                f"Exit: <code>{t['exit_price']}</code>\n"
                f"━━━━━━━━━━━━━━━━━━"
            )
        
        # KIRIM STATS REPORT SETELAH ADA TRADE CLOSED
        log.info(f"Sending stats report after {len(closed)} trade(s) closed...")
        send_stats_report(h)
        
    return len(closed)

def scan(h):
    pairs = top_pairs()
    if not pairs: return
        
    log.info("Scanning %s pairs for MULTI strategies...", len(pairs))
    created = 0
    meta = get_meta()
    
    for inst in pairs:
        if len(h["open"]) >= MAX_OPEN: break
            
        cs_15m = candles(inst, "15m", 300)
        cs_1h = candles(inst, "1H", 250)
        if not cs_15m or not cs_1h or len(cs_15m) < 300: continue
            
        # Run Strategy 1
        sig1 = build_scalp_signal(inst, cs_15m, meta)
        if isinstance(sig1, dict):
            if not any(x["signal_id"] == sig1["signal_id"] for x in h["open"] + h["closed"]):
                h["open"].append(sig1)
                save_history(h)
                tg(signal_message(sig1))
                created += 1
                log.info("NEW SCALP SIGNAL: %s %s", inst, sig1["direction"])
                
        # Run Strategy 2
        sig2 = build_ilm_signal(inst, cs_15m, cs_1h, meta)
        if isinstance(sig2, dict):
            if not any(x["signal_id"] == sig2["signal_id"] for x in h["open"] + h["closed"]):
                h["open"].append(sig2)
                save_history(h)
                tg(signal_message(sig2))
                created += 1
                log.info("NEW ILM SIGNAL: %s %s", inst, sig2["direction"])
            
    log.info("Scan complete. Created %s signals.", created)

def main():
    log.info("==========================================")
    log.info("ALPHAQUANT MULTI-STRATEGY BOT V1.1")
    log.info("Strategy 1: SCALP 15M STRICT")
    log.info("Strategy 2: ILM INTRADAY (PDF)")
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
