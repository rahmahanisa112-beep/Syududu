"""
ALPHAQUANT V6.0 - Golden Pocket + Supertrend (15m) + 1H structure filter
Signal-only. Playbook riset: aturan dari "Playbook BTCUSDT Fibonacci Golden Pocket
+ Supertrend + Konfirmasi Struktur 1H". Diterapkan ke top N pair OKX USDT perpetual.

Aturan (per candle 15m yang sudah tutup):
  1. Supertrend(ATR14, x3) FLIP tepat di candle sinyal: bearish->bullish (LONG) / bullish->bearish (SHORT)
  2. Filter 1H: close 1H terakhir yang sudah tutup vs close 4 candle 1H sebelumnya (searah)
  3. Golden Pocket: candle sinyal menyentuh zona 50%-61,8% dari range 24 candle 15m SEBELUMNYA
  4. SL = swing 8 candle 15m (termasuk candle sinyal); jarak SL 0,05%-3% harga
  5. TP = 2R. Entry = close candle sinyal (proksi dari "open candle berikutnya")
  Satu posisi aktif sekaligus; setelah 2 loss beruntun di hari (UTC) yang sama, berhenti sampai besok.
"""

from __future__ import annotations

import os, json, time, math, logging
from collections import Counter
from datetime import datetime, timezone

import requests

# ================= CONFIG =================

OKX = "https://www.okx.com"

def env_first(*names: str) -> str:
    for name in names:
        v = os.getenv(name, "").strip()
        if v:
            return v
    return ""

TOKEN = env_first("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
CHAT_ID = env_first("CHAT_ID", "TELEGRAM_CHAT_ID", "TELEGRAM_CHAT")
TELEGRAM = bool(TOKEN and CHAT_ID)

TF, TF_MS = "15m", 900_000
HTF, HTF_MS = "1H", 3_600_000
MON_TF, MON_MS = "5m", 300_000

TOP_PAIRS = int(os.getenv("TOP_PAIRS", "150"))
MIN_VOLUME_USDT = float(os.getenv("MIN_VOLUME_USDT", "3000000"))

RANGE_BARS = 24          # range Fibonacci: 24 candle 15m sebelum candle sinyal
SWING_BARS = 8           # SL: swing 8 candle 15m termasuk candle sinyal
ST_PERIOD = 14           # Supertrend ATR
ST_MULT = 3.0            # Supertrend faktor
GP_LO, GP_HI = 0.50, 0.618
RR = 2.0
MIN_SL_PCT = 0.0005      # 0,05% (dari playbook)
MAX_SL_PCT = 0.03        # 3%
MAX_OPEN = int(os.getenv("MAX_OPEN", "1"))              # playbook: tidak boleh ada posisi aktif lain
MAX_LOSSES_PER_DAY = int(os.getenv("MAX_LOSSES_PER_DAY", "2"))
COOLDOWN_HOURS = float(os.getenv("COOLDOWN_HOURS", "0"))

RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.0025"))   # playbook contoh: 0,25%
ACCOUNT_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "1000"))
FEE_PER_SIDE = float(os.getenv("FEE_PER_SIDE", "0.0005"))
REPORT_EVERY_HOURS = float(os.getenv("REPORT_EVERY_HOURS", "4"))

HISTORY_FILE = os.getenv("SIGNAL_HISTORY_FILE", "signals_history.json")
MONITOR_MAX_BARS = 1400
TIMEOUT = 15
RETRIES = 3
MIN_REQUEST_GAP = 0.09   # OKX public limit 20 req / 2 s

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("V6.0")
S = requests.Session()
S.headers.update({"User-Agent": "ALPHAQUANT-V6.0"})

# ================= HELPERS =================

def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

_LAST_REQ = [0.0]

def api(path, params=None):
    for attempt in range(RETRIES):
        wait = MIN_REQUEST_GAP - (time.time() - _LAST_REQ[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_REQ[0] = time.time()
        try:
            r = S.get(OKX + path, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()
            if d.get("code") == "0":
                return d
            log.warning("OKX error: %s", d)
        except Exception as e:
            log.warning("Request failed %s/%s: %s", attempt + 1, RETRIES, e)
        if attempt + 1 < RETRIES:
            time.sleep(1.5)
    return None

def tg(text: str):
    if not TELEGRAM:
        log.info("[TG OFF] %s", text[:200].replace("\n", " | "))
        return
    try:
        r = S.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                   json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                         "disable_web_page_preview": True}, timeout=TIMEOUT)
        if r.status_code != 200:
            log.warning("Telegram %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Telegram error: %s", e)

def load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                h = json.load(f)
            h.setdefault("open", []); h.setdefault("closed", [])
            return h
        except Exception as e:
            log.error("History unreadable (%s), starting empty", e)
    return {"open": [], "closed": []}

def save_history(h: dict):
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HISTORY_FILE)

# ================= MARKET DATA =================

_TICK = {}

def instruments():
    if _TICK:
        return _TICK
    d = api("/api/v5/public/instruments", {"instType": "SWAP"})
    for x in (d or {}).get("data", []):
        try:
            _TICK[x["instId"]] = float(x["tickSz"])
        except Exception:
            pass
    return _TICK

def top_pairs():
    d = api("/api/v5/market/tickers", {"instType": "SWAP"})
    if not d:
        return []
    rows = []
    for x in d.get("data", []):
        inst = x.get("instId", "")
        if not inst.endswith("-USDT-SWAP"):
            continue
        try:
            v = float(x.get("volCcy24h", 0))
        except Exception:
            continue
        if v >= MIN_VOLUME_USDT:
            rows.append((inst, v))
    rows.sort(key=lambda r: r[1], reverse=True)
    return [r[0] for r in rows[:TOP_PAIRS]]

def candles(inst, bar, limit):
    """Candle TERKONFIRMASI, urut lama -> baru: [ts, open, high, low, close, volume]."""
    d = api("/api/v5/market/candles", {"instId": inst, "bar": bar, "limit": str(limit)})
    if not d:
        return []
    out = []
    for x in d.get("data", []):
        try:
            if x[8] != "1":
                continue
            out.append([int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])])
        except Exception:
            continue
    out.sort(key=lambda c: c[0])
    return out

def price(inst):
    d = api("/api/v5/market/ticker", {"instId": inst})
    try:
        return float(d["data"][0]["last"])
    except Exception:
        return None

def tick_round(x, tick):
    if not tick or tick <= 0:
        return x
    prec = max(0, round(-math.log10(tick)))
    return round(round(x / tick) * tick, prec)

# ================= INDICATORS =================

def supertrend_bull(cs, period=ST_PERIOD, mult=ST_MULT):
    """
    Supertrend standar. Return list: True = bullish (close di atas supertrend), None = belum siap.
    ATR = RMA (Wilder), bands di-trail dengan aturan klasik.
    """
    n = len(cs)
    out = [None] * n
    if n < period + 2:
        return out
    tr = [0.0] * n
    for i in range(n):
        h, l = cs[i][2], cs[i][3]
        tr[i] = h - l if i == 0 else max(h - l, abs(h - cs[i - 1][4]), abs(l - cs[i - 1][4]))
    atr = [None] * n
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    fub = [None] * n
    flb = [None] * n
    st = [None] * n
    for i in range(period - 1, n):
        h, l, c = cs[i][2], cs[i][3], cs[i][4]
        hl2 = (h + l) / 2
        bub = hl2 + mult * atr[i]
        blb = hl2 - mult * atr[i]
        if i == period - 1:
            fub[i], flb[i] = bub, blb
            st[i] = fub[i] if c <= fub[i] else flb[i]
        else:
            pc = cs[i - 1][4]
            fub[i] = bub if (bub < fub[i - 1] or pc > fub[i - 1]) else fub[i - 1]
            flb[i] = blb if (blb > flb[i - 1] or pc < flb[i - 1]) else flb[i - 1]
            if st[i - 1] == fub[i - 1]:
                st[i] = fub[i] if c <= fub[i] else flb[i]
            else:
                st[i] = flb[i] if c >= flb[i] else fub[i]
        out[i] = c > st[i]
    return out

def golden_pocket(range_bars, side):
    """Zona retracement 50%-61,8% dari range H/L. Return (zona_bawah, zona_atas, H, L)."""
    H = max(x[2] for x in range_bars)
    L = min(x[3] for x in range_bars)
    span = H - L
    if side == "LONG":                       # retrace dari H turun ke L
        lo, hi = H - GP_HI * span, H - GP_LO * span
    else:                                    # retrace dari L naik ke H
        lo, hi = L + GP_LO * span, L + GP_HI * span
    return lo, hi, H, L

# ================= SIGNAL =================

def build_gp(inst, c15, c1h, meta):
    """
    Return (signal_dict | None, stage, detail). stage = alasan gugur (untuk funnel).
    """
    if len(c15) < RANGE_BARS + SWING_BARS + ST_PERIOD + 5:
        return None, "Data", f"hanya {len(c15)} candle 15m"
    bull = supertrend_bull(c15)
    if bull[-1] is None or bull[-2] is None:
        return None, "Data", "supertrend belum siap"

    sig = c15[-1]
    sig_close_ms = sig[0] + TF_MS
    flip_up = bull[-1] and not bull[-2]
    flip_dn = (not bull[-1]) and bull[-2]
    if not (flip_up or flip_dn):
        return None, "Supertrend", "tidak ada flip di candle sinyal"

    side = "LONG" if flip_up else "SHORT"

    # 1H: hanya candle yang sudah tutup sebelum candle sinyal ditutup
    h1 = [x for x in c1h if x[0] + HTF_MS <= sig_close_ms]
    if len(h1) < 5:
        return None, "1H", f"hanya {len(h1)} candle 1H tertutup"
    trend_up = h1[-1][4] > h1[-5][4]
    trend_dn = h1[-1][4] < h1[-5][4]
    if (side == "LONG" and not trend_up) or (side == "SHORT" and not trend_dn):
        return None, "1H", f"{side} tapi close 1H tidak searah (4 candle)"

    rng = c15[-1 - RANGE_BARS:-1]
    lo_z, hi_z, H, L = golden_pocket(rng, side)
    if not (sig[2] >= lo_z and sig[3] <= hi_z):
        return None, "GoldenPocket", (f"{side} candle sinyal tidak menyentuh zona "
                                      f"{lo_z:.8g}-{hi_z:.8g}")

    entry = sig[4]
    window = c15[-SWING_BARS:]
    if side == "LONG":
        sl = min(x[3] for x in window)
        if sl >= entry:
            return None, "SL", "LONG: SL di sisi yang salah"
        risk = entry - sl
        tp = entry + RR * risk
    else:
        sl = max(x[2] for x in window)
        if sl <= entry:
            return None, "SL", "SHORT: SL di sisi yang salah"
        risk = sl - entry
        tp = entry - RR * risk

    sl_pct = risk / entry
    if sl_pct < MIN_SL_PCT or sl_pct > MAX_SL_PCT:
        return None, "SL", f"{side} jarak SL {sl_pct*100:.2f}% (perlu 0,05%-3%)"

    tick = meta.get(inst) or 1e-8
    entry_t = tick_round(entry, tick)
    sl_t = tick_round(sl, tick)
    tp_t = tick_round(tp, tick)
    return {
        "signal_id": f"{inst}|{side}|{sig[0]}|GP",
        "symbol": inst,
        "direction": side,
        "setup": "GP",
        "timeframe": TF,
        "entry": entry_t,
        "sl": sl_t,
        "tp": tp_t,
        "rr": RR,
        "sl_pct": sl_pct * 100,
        "gp_zone": [round(lo_z, 8), round(hi_z, 8)],
        "range_hl": [round(L, 8), round(H, 8)],
        "htf_close": h1[-1][4],
        "htf_ref": h1[-5][4],
        "signal_candle_time": sig[0],
        "signal_candle_iso": iso(sig[0]),
        "created_at": now_iso(),
        "risk_usdt": ACCOUNT_EQUITY * RISK_PER_TRADE,
        "status": "OPEN",
        "exit_price": None, "exit_time": None, "result_r": None, "result": None,
        "monitor_tf_ms": TF_MS,
    }, "OK", ""

def fee_r(t):
    try:
        risk = abs(float(t["entry"]) - float(t["sl"]))
        return (2 * FEE_PER_SIDE * float(t["entry"]) / risk) if risk > 0 else 0.0
    except Exception:
        return 0.0

# ================= MONITOR =================

def monitor(trade):
    start = trade.get("signal_candle_time", 0) + TF_MS
    age_ms = max(0, int(time.time() * 1000) - start)
    needed = min(MONITOR_MAX_BARS, max(60, age_ms // MON_MS + 24))
    cs = candles(trade["symbol"], MON_TF, min(needed, 300))
    if not cs:
        return None
    sl, tp = trade["sl"], trade["tp"]
    for c in [x for x in cs if x[0] >= start]:
        hi, lo = c[2], c[3]
        if trade["direction"] == "LONG":
            hit_sl, hit_tp = lo <= sl, hi >= tp
        else:
            hit_sl, hit_tp = hi >= sl, lo <= tp
        if hit_sl and hit_tp:                 # satu candle menyentuh keduanya: SL dulu (konservatif)
            px, r, res = sl, -1.0, "LOSS"
        elif hit_tp:
            px, r, res = tp, float(trade.get("rr", RR)), "WIN"
        elif hit_sl:
            px, r, res = sl, -1.0, "LOSS"
        else:
            continue
        t = dict(trade)
        t.update({"status": "CLOSED", "exit_price": px, "exit_time": iso(c[0]),
                  "result_r": r, "result": res})
        return t
    return None

def check_open(h):
    if not h["open"]:
        return 0
    remain, closed = [], []
    for t in h["open"]:
        try:
            r = monitor(t)
        except Exception as e:
            log.error("Monitor %s failed: %s", t["symbol"], e)
            r = None
        (closed if r else remain).append(r or t)
    h["open"] = remain
    if closed:
        h["closed"].extend(closed)
        save_history(h)
    for t in closed:
        emoji = "✅" if t["result"] == "WIN" else "❌"
        tg(f"<b>{emoji} TRADE CLOSED</b>\n━━━━━━━━━━━━━━━━━━\n"
           f"<b>{t['symbol']}</b> {t['direction']} · Golden Pocket\n"
           f"Entry: <code>{t['entry']}</code>\nExit: <code>{t['exit_price']}</code>\n"
           f"Result: <b>{t['result_r']:+.2f}R</b>\nClosed: {t['exit_time']}")
    return len(closed)

# ================= RISK RULES =================

def daily_locked(h) -> bool:
    """Playbook: setelah 2 loss beruntun di hari (UTC) yang sama, berhenti sampai besok."""
    closed = [x for x in h["closed"] if x.get("exit_time")]
    if len(closed) < MAX_LOSSES_PER_DAY:
        return False
    closed.sort(key=lambda x: x["exit_time"])
    last = closed[-MAX_LOSSES_PER_DAY:]
    if any(x.get("result") != "LOSS" for x in last):
        return False
    return last[-1]["exit_time"][:10] == datetime.now(timezone.utc).date().isoformat()

def symbol_blocked(h, inst) -> bool:
    if any(x.get("symbol") == inst for x in h["open"]):
        return True
    if COOLDOWN_HOURS <= 0:
        return False
    cutoff = time.time() - COOLDOWN_HOURS * 3600
    for x in h["closed"]:
        if x.get("symbol") == inst:
            try:
                if datetime.fromisoformat(x["exit_time"]).timestamp() > cutoff:
                    return True
            except Exception:
                pass
    return False

# ================= REPORTS =================

def running_report(h):
    if not h["open"]:
        return
    blocks, rs = [], []
    for t in h["open"]:
        p = price(t["symbol"])
        if p is None:
            continue
        risk = abs(t["entry"] - t["sl"])
        r = ((p - t["entry"]) if t["direction"] == "LONG" else (t["entry"] - p)) / risk if risk else 0
        rs.append(r)
        blocks.append(f"{'🟢' if r > 0 else '🔴' if r < 0 else '⚪'} <b>{t['symbol']}</b> · {t['direction']}\n"
                      f"Entry <code>{t['entry']}</code> | Now <code>{p}</code>\n"
                      f"SL <code>{t['sl']}</code> | TP <code>{t['tp']}</code> | Float <b>{r:+.2f}R</b>")
    if blocks:
        tg("<b>📡 RUNNING POSITIONS</b>\n━━━━━━━━━━━━━━━━━━\n" + "\n\n".join(blocks)
           + f"\n\nFloating R: <b>{sum(rs):+.2f}R</b> (belum terealisasi)")

def stats(h):
    ts = h["closed"]
    if not ts:
        return
    total = sum(float(x.get("result_r", 0)) for x in ts)
    fees = sum(fee_r(x) for x in ts)
    wins = sum(1 for x in ts if x.get("result") == "WIN")
    avg_rr = sum(float(x.get("rr", RR)) for x in ts) / len(ts)
    bep_g = 100 / (1 + avg_rr)
    bep_n = 100 * (1 + fees / len(ts)) / (1 + avg_rr)
    eq = pk = dd = 0.0
    for x in sorted(ts, key=lambda x: x.get("exit_time", "")):
        eq += float(x.get("result_r", 0)); pk = max(pk, eq); dd = min(dd, eq - pk)
    note = "\n\n<i>Kurang dari 30 trade: belum bisa disimpulkan.</i>" if len(ts) < 30 else ""
    tg("<b>📊 V6.0 PERFORMANCE · Golden Pocket</b>\n━━━━━━━━━━━━━━━━━━\n"
       f"Trades        : {len(ts)}\nWin Rate      : <b>{wins/len(ts)*100:.2f}%</b>\n"
       f"Total R gross : {total:+.2f}R\nTotal R net   : <b>{total-fees:+.2f}R</b> (fee ≈)\n"
       f"Avg R net     : {(total-fees)/len(ts):+.3f}R\nMax Drawdown  : {dd:.2f}R\n"
       f"BEP WR        : {bep_g:.2f}% gross | <b>{bep_n:.2f}%</b> net" + note)

def signal_message(s):
    risk = abs(s["entry"] - s["sl"])
    fee_pct = 2 * FEE_PER_SIDE * 100
    return (
        "<b>🚨 ALPHAQUANT V6.0 SIGNAL</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"<b>{s['symbol']}</b> · <b>{s['direction']}</b> · 15m\n"
        f"Golden Pocket {s['gp_zone'][0]:.8g} – {s['gp_zone'][1]:.8g}\n"
        f"Range 24 bar: L {s['range_hl'][0]:.8g} · H {s['range_hl'][1]:.8g}\n"
        f"Supertrend flip ✔ · 1H searah ✔ (close {s['htf_close']:.8g} vs {s['htf_ref']:.8g})\n\n"
        "<b>TRADE PLAN</b>\n"
        f"Entry: <code>{s['entry']}</code>\nSL: <code>{s['sl']}</code> ({s['sl_pct']:.2f}%)\n"
        f"TP: <code>{s['tp']}</code>\nRR: <b>1:{s['rr']:.1f}</b>\n"
        f"Fee ≈ {fee_pct:.2f}% nilai posisi\n\n"
        f"Risk: {s['risk_usdt']:.2f} USDT ({RISK_PER_TRADE*100:.2f}% dari {ACCOUNT_EQUITY:g})\n"
        "━━━━━━━━━━━━━━━━━━\n⚠️ SIGNAL ONLY — tidak ada order otomatis\n"
        f"Candle: {s['signal_candle_iso']}\n"
        "<i>Entry diproksikan dari close candle sinyal; open candle berikutnya bisa berbeda.</i>"
    )

# ================= SCAN =================

def scan(h, ref_ts):
    funnel = Counter()
    if len(h["open"]) >= MAX_OPEN:
        log.info("Max open (%s) reached - scan skipped.", MAX_OPEN)
        funnel["Max open"] = 1
        return 0, 0, funnel

    if daily_locked(h):
        log.info("Daily stop: %s losses in a row today.", MAX_LOSSES_PER_DAY)
        funnel["Harian"] = 1
        return 0, 0, funnel

    pairs = top_pairs()
    if not pairs:
        log.warning("No pairs from OKX; scan aborted.")
        return 0, 0, funnel
    meta = instruments()
    log.info("GP scan: %s pairs on %s...", len(pairs), TF)

    created, scanned = 0, 0
    for inst in pairs:
        if len(h["open"]) >= MAX_OPEN:
            break
        if symbol_blocked(h, inst):
            funnel["Diblokir"] += 1
            continue
        scanned += 1
        try:
            c15 = candles(inst, TF, 300)
            if not c15 or c15[-1][0] != ref_ts:
                funnel["Data"] += 1          # candle tidak sinkron dengan referensi
                continue
            c1h = candles(inst, HTF, 100)
            s, stage, detail = build_gp(inst, c15, c1h, meta)
            if not s:
                funnel[stage] += 1
                continue
            h["open"].append(s)
            save_history(h)
            created += 1
            log.info("NEW SIGNAL %s %s entry=%s sl=%s tp=%s", s["symbol"], s["direction"], s["entry"], s["sl"], s["tp"])
            tg(signal_message(s))
            time.sleep(0.3)
        except Exception as e:
            log.exception("Scan error %s: %s", inst, e)
            funnel["Error"] += 1
    log.info("GP scan complete. Pairs: %s | New signals: %s", scanned, created)
    return created, scanned, funnel

# ================= MAIN =================

def main():
    global TELEGRAM
    log.info("ALPHAQUANT V6.0 starting | pairs=%s | max_open=%s | risk=%.2f%% | Telegram=%s",
             TOP_PAIRS, MAX_OPEN, RISK_PER_TRADE * 100, "ON" if TELEGRAM else "OFF")
    if not TELEGRAM:
        TELEGRAM = False

    h = load_history()
    log.info("Open: %s | Closed: %s", len(h["open"]), len(h["closed"]))

    today = datetime.now(timezone.utc).date().isoformat()
    if TELEGRAM and h.get("last_online_day") != today:
        h["last_online_day"] = today
        tg("<b>🟢 ALPHAQUANT V6.0 ONLINE</b>\n━━━━━━━━━━━━━━━━━━\n"
           f"Golden Pocket 15m + Supertrend + filter 1H\n"
           f"Top {TOP_PAIRS} pair · maks {MAX_OPEN} posisi aktif\n"
           f"Risiko {RISK_PER_TRADE*100:.2f}% · stop harian setelah {MAX_LOSSES_PER_DAY} loss\n"
           "Signal-only")

    closed_n = check_open(h)

    ref = candles("BTC-USDT-SWAP", TF, 3)
    ref_ts = ref[-1][0] if ref else None
    new_candle = ref_ts is not None and h.get("last_candle") != ref_ts

    due = (time.time() - float(h.get("last_report_ts", 0))) >= REPORT_EVERY_HOURS * 3600
    if due or closed_n:
        running_report(h)
        stats(h)
        h["last_report_ts"] = time.time()

    if new_candle:
        created, scanned, funnel = scan(h, ref_ts)
        if funnel or created:
            parts = " · ".join(f"{k} {v}" for k, v in funnel.most_common())
            log.info("Funnel: %s", parts or "-")
        h["last_candle"] = ref_ts
    else:
        log.info("No new 15m candle - scan skipped.")

    save_history(h)
    log.info("Done. Open: %s | Closed: %s", len(h["open"]), len(h["closed"]))

if __name__ == "__main__":
    main()
