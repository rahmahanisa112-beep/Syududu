"""
ALPHAQUANT ICP V3.0 (Pro Quant Recap Edition)
Strategi: Institutional Confluence Pullback (ICP)
Fitur: Sinyal Entry + Rekap Performa Detail (Per Sesi, Per Side, Streak, DD)
"""

import os
import json
import time
import math
import logging
import requests
from datetime import datetime, timezone, timedelta

# ==========================================
# 1. KONFIGURASI DASAR & ENV
# ==========================================
OKX = "https://www.okx.com"
HISTORY_FILE = "history_icp.json"
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
RUN_FOREVER = os.getenv("RUN_FOREVER", "1") == "1"
RETRIES = 3
MIN_REQUEST_GAP = 0.2
TIMEOUT = 10

# Telegram
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM = bool(TOKEN and CHAT_ID)

# Money Management & Target
TARGET_RR = float(os.getenv("TARGET_RR", "1.5"))
MAX_OPEN = int(os.getenv("MAX_OPEN", "5"))
COOLDOWN_HOURS = float(os.getenv("COOLDOWN_HOURS", "4.0"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))
ACCOUNT_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "1000.0"))
FEE_PER_SIDE = float(os.getenv("FEE_PER_SIDE", "0.0005"))
TOP_PAIRS = int(os.getenv("TOP_PAIRS", "20"))
MIN_VOLUME_USDT = float(os.getenv("MIN_VOLUME_USDT", "5000000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s| %(levelname)s| %(message)s")
log = logging.getLogger("ICP_BOT")
S = requests.Session()
S.headers.update({"User-Agent": "ALPHAQUANT-ICP-V3"})
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
def rma(v, n):
    out = [None] * len(v)
    if len(v) < n: return out
    x = sum(v[:n]) / n
    out[n - 1] = x
    for i in range(n, len(v)):
        x = ((x * (n - 1)) + v[i]) / n
        out[i] = x
    return out

def atr(cs, n=14):
    tr = []
    for i, c in enumerate(cs):
        if i == 0: tr.append(c[2] - c[3])
        else:
            pc = cs[i-1][4]
            tr.append(max(c[2]-c[3], abs(c[2]-pc), abs(c[3]-pc)))
    return rma(tr, n)

def rsi(cs, n=14):
    closes = [x[4] for x in cs]
    gains, losses = [0.0], [0.0]
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i-1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    ag, al = rma(gains, n), rma(losses, n)
    out = [None] * len(cs)
    for i in range(len(cs)):
        if ag[i] is None or al[i] is None: continue
        if al[i] == 0: out[i] = 100.0
        else:
            rs = ag[i] / al[i]
            out[i] = 100 - 100 / (1 + rs)
    return out

def calc_ema(closes, period):
    if len(closes) < period: return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema

# ==========================================
# 4. STRATEGI INTI: ICP
# ==========================================
def build_icp_signal(inst, cs_15m, cs_1h, meta):
    if len(cs_15m) < 50 or len(cs_1h) < 200: return None
        
    closes_1h = [c[4] for c in cs_1h]
    ema50_1h = calc_ema(closes_1h, 50)
    ema200_1h = calc_ema(closes_1h, 200)
    last_close_1h = closes_1h[-1]
    
    if not (ema50_1h and ema200_1h): return None
        
    is_bull_1h = last_close_1h > ema50_1h > ema200_1h
    is_bear_1h = last_close_1h < ema50_1h < ema200_1h
    
    if not (is_bull_1h or is_bear_1h): return None
        
    closes_15m = [c[4] for c in cs_15m]
    ema21_15m = calc_ema(closes_15m, 21)
    rsi_val = rsi(cs_15m, 14)[-1]
    atr_val = atr(cs_15m, 14)[-1]
    
    vol_avg = sum(c[5] for c in cs_15m[-21:-1]) / 20
    cur_vol = cs_15m[-1][5]
    
    if not (ema21_15m and rsi_val is not None and atr_val): return None
        
    last_c = cs_15m[-1]
    prev_c = cs_15m[-2]
    o, h, l, cl = last_c[1], last_c[2], last_c[3], last_c[4]
    
    if is_bull_1h:
        if l > ema21_15m * 1.01: return None
        if rsi_val >= 50: return None
        if cl <= o or cl <= prev_c[4]: return None
        if cur_vol < vol_avg * 1.0: return None
            
        side = "LONG"
        swing_low = min(c[3] for c in cs_15m[-21:-1])
        sl_raw = swing_low - (1.0 * atr_val)
        risk = cl - sl_raw
        if risk <= 0: return None
        tp_raw = cl + (TARGET_RR * risk)
        
    else:
        if h < ema21_15m * 0.99: return None
        if rsi_val <= 50: return None
        if cl >= o or cl >= prev_c[4]: return None
        if cur_vol < vol_avg * 1.0: return None
            
        side = "SHORT"
        swing_high = max(c[2] for c in cs_15m[-21:-1])
        sl_raw = swing_high + (1.0 * atr_val)
        risk = sl_raw - cl
        if risk <= 0: return None
        tp_raw = cl - (TARGET_RR * risk)
        
    tick = meta.get(inst, {}).get("tick", 0.1)
    entry = tick_round(cl, tick)
    sl = tick_round(sl_raw, tick)
    tp = tick_round(tp_raw, tick)
    
    risk_pct = (risk / cl) * 100
    min_risk = 0.002 if "BTC" in inst else 0.003
    if risk_pct < (min_risk * 100): return None
        
    note = (
        f"HTF 1H: {'BULL' if is_bull_1h else 'BEAR'} (EMA50>EMA200)\n"
        f"15m Pullback: EMA21 @ {ema21_15m:g}\n"
        f"RSI: {rsi_val:.1f} (Diskon)\n"
        f"Vol: {cur_vol/vol_avg:.2f}x Avg"
    )
    
    return {
        "signal_id": f"{inst}|{side}|{last_c[0]}|ICP",
        "symbol": inst, "direction": side, "timeframe": "15m", "setup": "ICP_PULLBACK",
        "note": note, "signal_candle_time": last_c[0], "signal_candle_iso": iso(last_c[0]),
        "created_at": now(), "entry": entry, "sl": sl, "tp": tp, "rr": TARGET_RR,
        "atr": atr_val, "sl_atr": risk / atr_val, "risk_pct": risk_pct, "rsi": rsi_val,
        "volume_ratio": cur_vol / vol_avg, "risk_usdt": ACCOUNT_EQUITY * RISK_PER_TRADE,
        "status": "OPEN", "exit_price": None, "exit_time": None, "result_r": None, "result": None,
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

def is_blocked(h, sig):
    sid = sig["signal_id"]
    if any(x.get("signal_id") == sid for x in h["open"] + h["closed"]): return True
    cutoff = time.time() - COOLDOWN_HOURS * 3600
    for x in h["closed"]:
        if x.get("symbol") != sig["symbol"]: continue
        try:
            if datetime.fromisoformat(x["exit_time"]).timestamp() > cutoff: return True
        except: pass
    return False

def monitor(t):
    mon_tf = "5m"
    start = t["signal_candle_time"]
    age_ms = max(0, int(time.time() * 1000) - start)
    needed = max(60, age_ms // 300000 + 24)
    if needed > 1400: needed = 1400
        
    cs = candles(t["symbol"], mon_tf, needed)
    if not cs: return None
        
    for c in [x for x in cs if x[0] >= start]:
        hi, lo = c[2], c[3]
        sl, tp = t["sl"], t["tp"]
        
        hit_sl = (lo <= sl) if t["direction"] == "LONG" else (hi >= sl)
        hit_tp = (hi >= tp) if t["direction"] == "LONG" else (lo <= tp)
        
        if hit_sl and hit_tp: exit_price, r, result = sl, -1.0, "LOSS"
        elif hit_tp: exit_price, r, result = tp, float(t.get("rr", TARGET_RR)), "WIN"
        elif hit_sl: exit_price, r, result = sl, -1.0, "LOSS"
        else: continue
            
        t_out = dict(t)
        t_out.update({
            "status": "CLOSED", "exit_price": exit_price, "exit_time": iso(c[0]),
            "result_r": r, "result": result
        })
        return t_out
    return None

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
                f"<b>{emoji} TRADE CLOSED</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<b>{t['symbol']}</b> {t['direction']}\n\n"
                f"<b>HASIL: {'WIN 🎯' if t['result'] == 'WIN' else 'LOSS 🛑'}</b>\n"
                f"R: <b>{t['result_r']:+.2f}R</b>\n\n"
                f"Entry: <code>{t['entry']}</code>\n"
                f"Exit: <code>{t['exit_price']}</code>\n"
                f"━━━━━━━━━━━━━━━━━━"
            )
        # Kirim Rekap Detail setelah ada trade yang closed
        send_pro_quant_recap(h)
    return len(closed)

# ==========================================
# 6. FITUR REKAP PRO QUANT (BARU!)
# ==========================================
def get_session_wib(exit_time_iso):
    """Menentukan sesi trading berdasarkan waktu exit (WIB = UTC+7)"""
    try:
        dt = datetime.fromisoformat(exit_time_iso)
        dt_wib = dt + timedelta(hours=7)
        hour = dt_wib.hour
        if 6 <= hour < 14: return "ASIA (06-14)"
        elif 14 <= hour < 20: return "LONDON (14-20)"
        elif 20 <= hour or hour < 3: return "NEW YORK (20-03)"
        else: return "QUIET (03-06)"
    except: return "UNKNOWN"

def send_pro_quant_recap(h):
    """Mengirim laporan performa detail seperti STATS PRO QUANT"""
    ts = h["closed"]
    if not ts: return
    
    total = len(ts)
    wins = [x for x in ts if x.get("result") == "WIN"]
    losses = [x for x in ts if x.get("result") == "LOSS"]
    bes = [x for x in ts if x.get("result") == "BREAKEVEN"]
    
    wr = (len(wins) / total) * 100 if total > 0 else 0
    total_r = sum(float(x.get("result_r", 0)) for x in ts)
    
    # Avg Win/Loss
    avg_win = sum(float(x.get("result_r", 0)) for x in wins) / len(wins) if wins else 0
    avg_loss = sum(float(x.get("result_r", 0)) for x in losses) / len(losses) if losses else 0
    
    # Profit Factor
    gp = sum(max(float(x.get("result_r", 0)), 0) for x in ts)
    gl = abs(sum(min(float(x.get("result_r", 0)), 0) for x in ts))
    pf = gp / gl if gl else float("inf")
    
    # Expectancy
    expectancy = total_r / total if total > 0 else 0
    
    # Max Drawdown
    eq = peak = dd = 0.0
    for x in ts:
        eq += float(x.get("result_r", 0))
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        
    # 20-Trade Sample (Douglas)
    last_20 = ts[-20:]
    r20 = sum(float(x.get("result_r", 0)) for x in last_20)
    w20 = len([x for x in last_20 if x.get("result") == "WIN"])
    wr20 = (w20 / len(last_20)) * 100 if last_20 else 0
    
    # High Quality Wins (>= 2.0R)
    hq_wins = len([x for x in wins if float(x.get("result_r", 0)) >= 2.0])
    hq_pct = (hq_wins / len(wins)) * 100 if wins else 0
    
    # Streak Calculation
    current_streak = 0
    streak_type = ""
    for x in reversed(ts):
        res = x.get("result")
        if res == "WIN":
            if streak_type == "L": break
            streak_type = "W"
            current_streak += 1
        elif res == "LOSS":
            if streak_type == "W": break
            streak_type = "L"
            current_streak += 1
        else: break
    streak_str = f"{current_streak}{streak_type}"
    
    # Per Session
    sessions = {"ASIA (06-14)": [], "LONDON (14-20)": [], "NEW YORK (20-03)": [], "QUIET (03-06)": []}
    for x in ts:
        ses = get_session_wib(x.get("exit_time", ""))
        if ses in sessions: sessions[ses].append(x)
            
    session_report = ""
    for ses_name, trades_in_ses in sessions.items():
        if not trades_in_ses: continue
        t_ses = len(trades_in_ses)
        w_ses = len([x for x in trades_in_ses if x.get("result") == "WIN"])
        l_ses = len([x for x in trades_in_ses if x.get("result") == "LOSS"])
        r_ses = sum(float(x.get("result_r", 0)) for x in trades_in_ses)
        wr_ses = (w_ses / t_ses) * 100 if t_ses > 0 else 0
        session_report += f"• {ses_name}: {t_ses}t ({w_ses}W/{l_ses}L) → {r_ses:+.2f}R ({wr_ses:.0f}%)\n"
        
    # Per Side
    longs = [x for x in ts if x.get("direction") == "LONG"]
    shorts = [x for x in ts if x.get("direction") == "SHORT"]
    
    def side_report(trades_list, side_name):
        if not trades_list: return ""
        t_s = len(trades_list)
        w_s = len([x for x in trades_list if x.get("result") == "WIN"])
        l_s = len([x for x in trades_list if x.get("result") == "LOSS"])
        r_s = sum(float(x.get("result_r", 0)) for x in trades_list)
        wr_s = (w_s / t_s) * 100 if t_s > 0 else 0
        return f"• {side_name}: {t_s}t ({w_s}W/{l_s}L) → {r_s:+.2f}R ({wr_s:.0f}%)\n"
        
    side_report = side_report(longs, "LONG") + side_report(shorts, "SHORT")
    
    # 5 Trade Terakhir
    last_5 = ts[-5:]
    last_5_str = ""
    for x in reversed(last_5):
        emoji = "✅" if x.get("result") == "WIN" else "❌"
        last_5_str += f"{emoji} {x.get('exit_time', '-')[:10]} {x.get('symbol','')} [{x.get('setup','')}] {x.get('direction','')} → {x.get('result_r',0):+.2f}R\n"

    # Kirim ke Telegram
    recap_text = (
        f"<b>📊 STATS PRO QUANT (ICP Strategy)</b>\n"
        f"Update {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n\n"
        f"<b>🎯 OVERALL PERFORMANCE</b>\n"
        f"Trades: {total} → W {len(wins)} | L {len(losses)} | BE {len(bes)}\n"
        f"Winrate: {wr:.1f}%\n"
        f"Total PnL: {total_r:+.2f}R | PF: {pf:.2f}\n"
        f"Avg Win: +{avg_win:.2f}R | Avg Loss: {avg_loss:.2f}R\n"
        f"Expectancy: {expectancy:+.2f}R/trade | Max DD: {dd:.2f}R\n\n"
        f"<b>🧠 METRIK PSIKOLOGI & KUALITAS</b>\n"
        f" 20-Trade Sample (Douglas): PnL {r20:+.2f}R | WR {wr20:.0f}%\n"
        f"🏆 High Quality Wins (≥2.0R): {hq_pct:.0f}% dari total win\n"
        f" Streak: {streak_str}\n\n"
        f"<b>🕐 PER SESI (WIB)</b>\n"
        f"{session_report}\n"
        f"<b>⚖️ PER SIDE</b>\n"
        f"{side_report}\n"
        f"<b>🕒 5 TRADE TERAKHIR</b>\n"
        f"{last_5_str}"
    )
    
    tg(recap_text)

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
        f"<b>🚨 ALPHAQUANT ICP SIGNAL (WR Target 70%)</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>{s['symbol']}</b> · <b>{s['direction']}</b> · {s['timeframe']}\n\n"
        f"{s['note']}\n\n"
        f"<b>TRADE PLAN</b>\n"
        f"Entry: <code>{s['entry']}</code>\n"
        f"SL: <code>{s['sl']}</code>\n"
        f"TP: <code>{s['tp']}</code>\n"
        f"RR: <b>1:{float(s.get('rr', TARGET_RR)):.2f}</b>\n"
        f"SL: {s['sl_atr']:.2f} ATR · {s['risk_pct']:.2f}% harga\n"
        f"Fee ≈ {fee_r:.2f}R\n\n"
        f"Risk: {s['risk_usdt']:.2f} USDT\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

def scan(h):
    pairs = top_pairs()
    if not pairs:
        log.warning("No pairs found. Check API or MIN_VOLUME_USDT.")
        return
        
    log.info("Scanning %s pairs for ICP setups...", len(pairs))
    created = 0
    meta = get_meta()
    
    for inst in pairs:
        if len(h["open"]) >= MAX_OPEN:
            log.info("Open cap (%s) reached - stopping scan.", MAX_OPEN)
            break
            
        cs_15m = candles(inst, "15m", 100)
        cs_1h = candles(inst, "1H", 250)
        
        if not cs_15m or not cs_1h: continue
            
        sig = build_icp_signal(inst, cs_15m, cs_1h, meta)
        
        if isinstance(sig, dict):
            if is_blocked(h, sig): continue
            h["open"].append(sig)
            save_history(h)
            tg(signal_message(sig))
            created += 1
            log.info("NEW SIGNAL: %s %s @ %s", inst, sig["direction"], sig["entry"])
            
    log.info("Scan complete. Created %s signals.", created)

def main():
    log.info("==========================================")
    log.info("ALPHAQUANT ICP V3.0 - PRO QUANT RECAP MODE")
    log.info(f"Target WR: ~70% | RR: 1:{TARGET_RR} | MaxOpen: {MAX_OPEN}")
    log.info("==========================================")
    
    if not TELEGRAM:
        log.warning("Telegram dinonaktifkan.")
    else:
        tg(f"<b> ALPHAQUANT ICP ONLINE</b>\nPro Quant Recap Mode (Target WR ~70%)\nRR: 1:{TARGET_RR}")
        
    h = load_history()
    
    while True:
        started = time.time()
        try:
            closed_n = check_open(h)
            scan(h)
        except KeyboardInterrupt:
            log.info("Bot dihentikan manual.")
            break
        except Exception as e:
            log.error("Loop error: %s", e)
            
        elapsed = time.time() - started
        sleep_for = max(1, SCAN_INTERVAL - elapsed)
        
        if not RUN_FOREVER:
            log.info("Mode sekali jalan (RUN_FOREVER=0). Selesai.")
            break
            
        log.info("Siklus selesai. Tidur %s detik...", sleep_for)
        time.sleep(sleep_for)

if __name__ == "__main__":
    main()
