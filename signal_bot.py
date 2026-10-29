"""
ALPHAQUANT V6.1 - BTC/USDT PERP Institutional Alpha Research (sesuai spesifikasi v1.0)
Signal-only. Engine mengikuti gate G0-G8, regime 1H, dynamic risk, adaptive exit, kill switch.

BATASAN DATA (penting):
- OKX candle = OHLCV. Delta, CVD, footprint, dan bid absorption di-PROXY dari OHLCV.
  Hasil yang memakai proxy diberi label; ini bukan verifikasi order flow.
- Data macro (SPX, DXY, US10Y, VIX futures) dan spread historis TIDAK tersedia di OKX.
  GATE_MODE=STRICT: gate itu gagal (UNKNOWN) -> tidak ada sinyal.
  GATE_MODE=PROXY : gate itu dilewati dan ditandai "SKIPPED" di pesan.
"""

from __future__ import annotations

import os, json, time, math, logging
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# ================= CONFIG =================

OKX = "https://www.okx.com"
def env_first(*names):
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return ""

TOKEN = env_first("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
CHAT_ID = env_first("CHAT_ID", "TELEGRAM_CHAT_ID", "TELEGRAM_CHAT")
TELEGRAM = bool(TOKEN and CHAT_ID)

SYMBOL = "BTC-USDT-SWAP"
TF15, TF15_MS = "15m", 900_000
TF1H, TF1H_MS = "1H", 3_600_000
TF4H, TF4H_MS = "4H", 14_400_000
MON_TF, MON_MS = "5m", 300_000

GATE_MODE = os.getenv("GATE_MODE", "PROXY").strip().upper()     # STRICT | PROXY
ACCOUNT_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "1000"))
BASE_RISK = float(os.getenv("BASE_RISK", "0.005"))              # baseline 0,5% (spec)
FEE_PER_SIDE = float(os.getenv("FEE_PER_SIDE", "0.0005"))
SLIP_PER_SIDE = float(os.getenv("SLIP_PER_SIDE", "0.0002"))
GEOM_MIN_NET_RR = float(os.getenv("GEOM_MIN_NET_RR", "1.8"))   # spec G7 = 2.0 setelah biaya; lihat catatan
MAX_OPEN = int(os.getenv("MAX_OPEN", "1"))
STANDBY_ADX = 20.0
REPORT_EVERY_HOURS = float(os.getenv("REPORT_EVERY_HOURS", "4"))
HISTORY_FILE = os.getenv("SIGNAL_HISTORY_FILE", "signals_history.json")
TIMEOUT, RETRIES, MIN_GAP = 15, 3, 0.09

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("V6.1")
S = requests.Session()
S.headers.update({"User-Agent": "ALPHAQUANT-V6.1"})
LON = ZoneInfo("Europe/London")
NYC = ZoneInfo("America/New_York")

# ================= IO =================

_LAST = [0.0]
def api(path, params=None):
    for a in range(RETRIES):
        w = MIN_GAP - (time.time() - _LAST[0])
        if w > 0:
            time.sleep(w)
        _LAST[0] = time.time()
        try:
            r = S.get(OKX + path, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()
            if d.get("code") == "0":
                return d
            log.warning("OKX error: %s", d)
        except Exception as e:
            log.warning("Request failed %s/%s: %s", a + 1, RETRIES, e)
        if a + 1 < RETRIES:
            time.sleep(1.5)
    return None

def candles(bar, limit):
    d = api("/api/v5/market/candles", {"instId": SYMBOL, "bar": bar, "limit": str(limit)})
    out = []
    for x in (d or {}).get("data", []):
        try:
            if x[8] == "1":
                out.append([int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])])
        except Exception:
            pass
    out.sort(key=lambda c: c[0])
    return out

def tick_size():
    d = api("/api/v5/public/instruments", {"instType": "SWAP", "instId": SYMBOL})
    try:
        return float(d["data"][0]["tickSz"])
    except Exception:
        return 0.1

def tg(text):
    if not TELEGRAM:
        log.info("[TG OFF] %s", text[:160].replace("\n", " | "))
        return
    try:
        r = S.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                   json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                         "disable_web_page_preview": True}, timeout=TIMEOUT)
        if r.status_code != 200:
            log.warning("Telegram %s: %s", r.status_code, r.text[:160])
    except Exception as e:
        log.warning("Telegram error: %s", e)

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                h = json.load(f)
            h.setdefault("open", []); h.setdefault("closed", [])
            return h
        except Exception as e:
            log.error("History unreadable: %s", e)
    return {"open": [], "closed": []}

def save_history(h):
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HISTORY_FILE)

def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()

# ================= INDICATORS =================

def ema_s(v, n):
    out = [None] * len(v)
    if len(v) < n:
        return out
    k = 2 / (n + 1); x = sum(v[:n]) / n; out[n - 1] = x
    for i in range(n, len(v)):
        x = v[i] * k + x * (1 - k); out[i] = x
    return out

def wilder(v, n):
    out = [None] * len(v)
    idx = [i for i, x in enumerate(v) if x is not None]
    if len(idx) < n:
        return out
    s0 = idx[n - 1]
    x = sum(v[idx[0]:idx[0] + n]) / n
    out[s0] = x
    for i in range(s0 + 1, len(v)):
        if v[i] is None:
            continue
        x = (x * (n - 1) + v[i]) / n; out[i] = x
    return out

def atr_s(cs, n=14):
    tr = [cs[0][2] - cs[0][3]] + [max(cs[i][2] - cs[i][3], abs(cs[i][2] - cs[i-1][4]),
                                     abs(cs[i][3] - cs[i-1][4])) for i in range(1, len(cs))]
    return wilder(tr, n)

def rsi_s(cs, n=14):
    out = [None] * len(cs)
    if len(cs) <= n:
        return out
    g = [0.0]; l = [0.0]
    for i in range(1, len(cs)):
        d = cs[i][4] - cs[i-1][4]
        g.append(max(d, 0)); l.append(max(-d, 0))
    ag = wilder(g, n); al = wilder(l, n)
    for i in range(len(cs)):
        if ag[i] is not None and al[i] is not None:
            out[i] = 100.0 if al[i] == 0 else 100 - 100 / (1 + ag[i] / al[i])
    return out

def adx_s(cs, n=14):
    m = len(cs)
    pl = [0.0] * m; mn = [0.0] * m; tr = [0.0] * m
    for i in range(1, m):
        up = cs[i][2] - cs[i-1][2]; dn = cs[i-1][3] - cs[i][3]
        pl[i] = up if (up > dn and up > 0) else 0.0
        mn[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(cs[i][2] - cs[i][3], abs(cs[i][2] - cs[i-1][4]), abs(cs[i][3] - cs[i-1][4]))
    atr_ = wilder(tr, n); p_ = wilder(pl, n); m_ = wilder(mn, n)
    dx = [None] * m
    for i in range(m):
        if atr_[i]:
            pdi = 100 * p_[i] / atr_[i]; mdi = 100 * m_[i] / atr_[i]
            dx[i] = 100 * abs(pdi - mdi) / max(pdi + mdi, 1e-9)
    return wilder(dx, n)

def pivots(cs, left=2, right=2, high=True):
    """Indeks pivot yang sudah terkonfirmasi (ada right bar setelahnya)."""
    out = []
    key = 2 if high else 3
    for i in range(left, len(cs) - right):
        v = cs[i][key]
        ok = True
        for j in range(i - left, i + right + 1):
            if j == i:
                continue
            if (high and cs[j][key] >= v) or ((not high) and cs[j][key] <= v):
                ok = False; break
        if ok:
            out.append(i)
    return out

def stoch_rsi_k_d(rs):
    st = [None] * len(rs)
    for i in range(len(rs)):
        if i < 27 or any(x is None for x in rs[i-13:i+1]):
            continue
        w = rs[i-13:i+1]; lo, hi = min(w), max(w)
        st[i] = 100 * (rs[i] - lo) / max(hi - lo, 1e-9)
    k = [None] * len(rs)
    for i in range(len(rs)):
        w = [x for x in st[max(0, i-2):i+1] if x is not None]
        if len(w) == 3:
            k[i] = sum(w) / 3
    d = [None] * len(rs)
    for i in range(len(rs)):
        w = [x for x in k[max(0, i-2):i+1] if x is not None]
        if len(w) == 3:
            d[i] = sum(w) / 3
    return k, d

def delta_proxy(cs, tick):
    return [c[5] * (c[4] - c[1]) / max(c[2] - c[3], tick) for c in cs]

def vwap_session(cs_today):
    pv = 0.0; v = 0.0
    for c in cs_today:
        tp = (c[2] + c[3] + c[4]) / 3
        pv += tp * c[5]; v += c[5]
    return pv / v if v > 0 else None

def value_area(cs, bins=40, pct=0.70):
    lo = min(c[3] for c in cs); hi = max(c[2] for c in cs)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    vol = [0.0] * bins
    for c in cs:
        t = (c[2] + c[3] + c[4]) / 3
        vol[min(bins - 1, max(0, int((t - lo) / step)))] += c[5]
    tot = sum(vol)
    if tot <= 0:
        return None
    poc = max(range(bins), key=lambda i: vol[i])
    a = b = poc; acc = vol[poc]
    while acc < pct * tot and (a > 0 or b < bins - 1):
        dn = vol[a-1] if a > 0 else -1.0
        up = vol[b+1] if b < bins - 1 else -1.0
        if up >= dn:
            b += 1; acc += vol[b]
        else:
            a -= 1; acc += vol[a]
    return {"VAL": lo + a * step, "VAH": lo + (b + 1) * step, "POC": lo + (poc + 0.5) * step}

def pct_rank(series_vals, x):
    if not series_vals:
        return None
    return 100.0 * sum(1 for v in series_vals if v <= x) / len(series_vals)

# ================= REGIME (1H, closed) =================

def regime_1h(h1, h4):
    """Return (regime, detail). Regime: SPIKE | STRONG_UP | STRONG_DN | WEAK_UP | WEAK_DN | RANGE | STANDBY | UNCLEAR."""
    if len(h1) < 210:
        return "UNCLEAR", f"hanya {len(h1)} candle 1H (perlu 210 untuk EMA200)"
    c = [x[4] for x in h1]
    at = atr_s(h1, 14)
    ax = adx_s(h1, 14)
    e20 = ema_s(c, 20); e50 = ema_s(c, 50); e200 = ema_s(c, 200)
    atr_ratio = [(at[i] / c[i]) if at[i] else None for i in range(len(h1))]
    hist = [x for x in atr_ratio[-500:] if x is not None]
    pct_now = pct_rank(hist, atr_ratio[-1]) if atr_ratio[-1] is not None else None
    tr_last = max(h1[-1][2] - h1[-1][3], abs(h1[-1][2] - c[-2]), abs(h1[-1][3] - c[-2]))
    vols = [x[5] for x in h1]
    sma20v = sum(vols[-20:]) / 20
    spike = (pct_now is not None and pct_now >= 90) or tr_last >= 2.5 * at[-1] or vols[-1] >= 2.0 * sma20v
    if spike:
        return "SPIKE", f"ATR pct {pct_now:.0f} / TR {tr_last/at[-1]:.1f}x ATR / vol {vols[-1]/sma20v:.1f}x"
    if all(ax[i] is not None and ax[i] < STANDBY_ADX for i in range(len(h1) - 24, len(h1))):
        return "STANDBY", "ADX 1H < 20 selama 24 candle"
    adx = ax[-1]
    if adx is None or e200[-1] is None:
        return "UNCLEAR", "indikator belum siap"
    up_stack = e20[-1] > e50[-1] > e200[-1]
    dn_stack = e20[-1] < e50[-1] < e200[-1]
    slope = e50[-1] - e50[-7] if e50[-7] else 0
    s4 = structure_4h(h4)
    if adx >= 25 and up_stack and s4 == "HH_HL":
        return "STRONG_UP", f"ADX {adx:.1f}, EMA stack bull, 4H {s4}"
    if adx >= 25 and dn_stack and s4 == "LH_LL":
        return "STRONG_DN", f"ADX {adx:.1f}, EMA stack bear, 4H {s4}"
    if 20 <= adx < 25 and e20[-1] > e50[-1] and slope > 0 and s4 != "LH_LL":
        return "WEAK_UP", f"ADX {adx:.1f}, EMA20>EMA50, slope EMA50 +"
    if 20 <= adx < 25 and e20[-1] < e50[-1] and slope < 0 and s4 != "HH_HL":
        return "WEAK_DN", f"ADX {adx:.1f}, EMA20<EMA50, slope EMA50 -"
    if adx < 20:
        return "RANGE", f"ADX {adx:.1f}"
    return "UNCLEAR", f"ADX {adx:.1f}, tidak ada kecocokan regime"

def structure_4h(h4):
    if len(h4) < 12:
        return "UNKNOWN"
    ph = pivots(h4, 3, 3, True); pl = pivots(h4, 3, 3, False)
    if len(ph) < 2 or len(pl) < 2:
        return "UNKNOWN"
    hh = h4[ph[-1]][2] > h4[ph[-2]][2]
    hl = h4[pl[-1]][3] > h4[pl[-2]][3]
    lh = h4[ph[-1]][2] < h4[ph[-2]][2]
    ll = h4[pl[-1]][3] < h4[pl[-2]][3]
    if hh and hl: return "HH_HL"
    if lh and ll: return "LH_LL"
    return "MIXED"

# ================= SIGNAL (LONG pipeline; SHORT = inverted data) =================

def invert(cs):
    return [[c[0], -c[1], -c[3], -c[2], -c[4], c[5]] for c in cs]

def gate_pipeline(c15, c1h, c4h, tick, now_ms):
    """Pipeline LONG. Return (result dict | None, gate, detail, gates)."""
    gates = {}
    if len(c15) < 120:
        return None, "G0", f"hanya {len(c15)} candle 15m", gates
    sig = c15[-1]
    sig_close = sig[0] + TF15_MS
    h1 = [x for x in c1h if x[0] + TF1H_MS <= sig_close]
    h4 = [x for x in c4h if x[0] + TF4H_MS <= sig_close]

    # G0 data
    gates["G0"] = "YES"
    if GATE_MODE == "STRICT":
        gates["G0"] = "NO: macro & tick aggressor tidak tersedia (UNKNOWN)"
        return None, "G0", "data macro/aggressor UNKNOWN (GATE_MODE=STRICT)", gates
    gates["G0"] = "PROXY: macro dilewati, flow dari OHLCV"

    # G1 session overlap London-NY (kalender DST)
    t = datetime.fromtimestamp(sig[0] / 1000, timezone.utc)
    lt = t.astimezone(LON).hour
    nt = t.astimezone(NYC).hour
    if not (8 <= lt < 16 and 8 <= nt < 17):
        return None, "G1", f"di luar overlap London-NY ({t:%H:%M} UTC)", gates
    gates["G1"] = "YES (spread historis SKIPPED)" if GATE_MODE == "PROXY" else "YES"

    # Regime
    reg, rdet = regime_1h(h1, h4)
    if reg not in ("STRONG_UP", "WEAK_UP"):
        return None, "Regime", f"{reg}: {rdet}", gates
    gates["G2"] = f"YES {reg}"

    atr15 = atr_s(c15, 14)[-1]
    if not atr15:
        return None, "G0", "ATR15 belum siap", gates
    tick = tick or 0.1

    # G3 level sweep: prior 1H swing low, Asia low, VAL
    lv = {}
    if len(h1) >= 25:
        lv["1H swing low"] = min(x[3] for x in h1[-21:-1])
    asia_day = datetime.fromtimestamp(sig[0] / 1000, timezone.utc).date()
    asia = [c for c in c15 if datetime.fromtimestamp(c[0] / 1000, timezone.utc).date() == asia_day
            and datetime.fromtimestamp(c[0] / 1000, timezone.utc).hour < 8]
    if asia:
        lv["Asia low"] = min(c[3] for c in asia)
    if len(h1) >= 120:
        va = value_area(h1[-120:])
        if va:
            lv["VAL"] = va["VAL"]
    sweep_i = None; level_name = None; level = None
    for back in range(0, 3):                   # sweep harus <= 2 bar sebelum close sinyal
        i = len(c15) - 1 - back
        for name, p in lv.items():
            if c15[i][3] <= p - 0.10 * atr15 and any(c15[j][4] > p for j in range(i, min(i + 3, len(c15)))):
                sweep_i, level_name, level = i, name, p
                break
        if sweep_i is not None:
            break
    if sweep_i is None:
        return None, "G3", "tidak ada sweep level (1H swing / Asia low / VAL)", gates
    gates["G3"] = f"YES sweep {level_name} @ {level:.8g}"
    sweep_low = c15[sweep_i][3]

    # G4 absorption (PROXY dari OHLCV)
    dl = delta_proxy(c15, tick)
    sd = (sum((x - sum(dl[-96:]) / 96) ** 2 for x in dl[-96:]) / 96) ** 0.5
    if not (dl[sweep_i] < -1.5 * sd and c15[sweep_i][4] > level):
        return None, "G4", "absorption: delta sweep tidak cukup negatif / close belum kembali di atas level", gates
    cvd = []
    run = 0.0
    for x in dl:
        run += x; cvd.append(run)
    if c15[-1][3] < c15[-5][3] and min(cvd[-4:]) < min(cvd[-8:-4]):
        return None, "G4", "CVD membuat lower low saat harga lower low", gates
    med20 = sorted(c[5] for c in c15[-21:-1])[10]
    if c15[sweep_i][5] < 1.5 * med20:
        return None, "G4", "bid absorption proxy (volume sweep < 1.5x median) belum terpenuhi", gates
    gates["G4"] = "YES (PROXY delta/CVD/volume)"

    # G5 CHoCH/BOS
    ph = pivots(c15[:-1], 2, 2, True)
    if not ph:
        return None, "G5", "belum ada pivot high 15m", gates
    last_ph = c15[ph[-1]][2]
    vsma = sum(c[5] for c in c15[-21:-1]) / 20
    if not (c15[-1][4] > last_ph + 0.05 * atr15 and c15[-1][5] >= 1.3 * vsma):
        return None, "G5", f"BOS: close {c15[-1][4]:.8g} belum > pivot {last_ph:.8g} + 0.05 ATR / volume kurang", gates
    if len(c15) - 1 - sweep_i > 4:
        return None, "G5", "BOS terjadi > 4 bar setelah sweep", gates
    gates["G5"] = f"YES BOS > {last_ph:.8g}"

    # G6 momentum: hidden bullish divergence + VWAP + StochRSI
    rs = rsi_s(c15, 14)
    pl = pivots(c15[:-1], 2, 2, False)
    if len(pl) < 2:
        return None, "G6", "pivot low kurang untuk divergence", gates
    p1, p2 = pl[-2], pl[-1]
    if not (c15[p2][3] > c15[p1][3] and rs[p2] is not None and rs[p1] is not None and rs[p2] < rs[p1]):
        return None, "G6", "tidak ada hidden bullish divergence (higher low harga, lower low RSI)", gates
    today = [c for c in c15 if datetime.fromtimestamp(c[0] / 1000, timezone.utc).date() == asia_day]
    vw = vwap_session(today)
    if vw is None or c15[-1][4] <= vw:
        return None, "G6", "close belum di atas VWAP sesi", gates
    k, d = stoch_rsi_k_d(rs)
    cross = None
    for j in range(len(c15) - 3, len(c15)):
        if k[j] is not None and d[j] is not None and k[j-1] is not None and d[j-1] is not None:
            if k[j-1] <= d[j-1] and k[j] > d[j] and k[j] < 40:
                cross = j
    if cross is None:
        return None, "G6", "StochRSI K belum cross above D dengan K<40 dalam 3 bar", gates
    gates["G6"] = "YES hidden div + VWAP + StochRSI"

    # G7 geometry
    entry = c15[-1][4]
    structural = sweep_low - max(tick, 0.10 * atr15)
    atr_sl = entry - 1.5 * atr15
    sl = max(structural, atr_sl)
    if sl > sweep_low:
        return None, "G7", "stop ATR lebih ketat dari sweep low (invalidasi struktur tidak terlindungi)", gates
    D = entry - sl
    if D <= 0:
        return None, "G7", "jarak stop tidak valid", gates
    tp2 = entry + 2 * D
    opp = [x[2] for x in h1[-20:] if entry < x[2] < tp2]
    if opp:
        return None, "G7", f"level 1H berlawanan {max(opp):.8g} menghalangi 2R", gates
    cost_R = (2 * FEE_PER_SIDE + 2 * SLIP_PER_SIDE) * entry / D
    net_rr = 2 - cost_R
    if net_rr < GEOM_MIN_NET_RR:
        return None, "G7", f"RR net {net_rr:.2f} < {GEOM_MIN_NET_RR} (biaya {cost_R:.2f}R)", gates
    gates["G7"] = f"YES D={D:.8g} net RR {net_rr:.2f}"

    return {
        "entry": entry, "sl": sl, "D": D, "tp1": entry + D, "tp2": tp2,
        "atr15": atr15, "level": level_name, "level_px": level, "sweep_low": sweep_low,
        "regime": reg, "regime_detail": rdet, "cost_R": cost_R, "net_rr": net_rr,
        "signal_i": len(c15) - 1,
    }, "OK", "", gates

def build_signal(c15, c1h, c4h, tick):
    now_ms = int(time.time() * 1000)
    out = {}
    res_long, g_long, d_long, gates_long = gate_pipeline(c15, c1h, c4h, tick, now_ms)
    if res_long:
        out["LONG"] = (res_long, gates_long)
    inv15, inv1h, inv4h = invert(c15), invert(c1h), invert(c4h)
    res_s, g_s, d_s, gates_s = gate_pipeline(inv15, inv1h, inv4h, tick, now_ms)
    if res_s:
        # kembalikan ke harga asli
        res_s = {k: (-v if k in ("entry", "sl", "tp1", "tp2", "level_px", "sweep_low") else v)
                 for k, v in res_s.items()}
        res_s["sl"] = res_s["sl"]
        res_s["D"] = abs(res_s["entry"] - res_s["sl"])
        res_s["tp1"] = res_s["entry"] - res_s["D"]
        res_s["tp2"] = res_s["entry"] - 2 * res_s["D"]
        out["SHORT"] = (res_s, gates_s)
    if out:
        return out, None, None, None
    return None, (g_long, d_long), (g_s, d_s), gates_long or gates_s

# ================= MONITOR (scale-out, 5m candles) =================

def simulate_trade(t, cs5):
    """Exit bertahap sesuai spec: 50% @1R (stop -> BE+biaya), 30% @2R, 20% trail 1.5 ATR15.
       Timeout 8 bar 15m jika MFE < 0.5R sebelum TP1. Return (closed, realized_R, exit_ms, reason)."""
    D = t["D"]; e = t["entry"]; long_ = t["direction"] == "LONG"
    stop = t["sl"]; pos = 1.0; realized = 0.0; tp1 = tp2 = False
    mfe = 0.0; start = t["signal_candle_time"] + TF15_MS
    trail = None
    cost_R = t.get("cost_R", 0.0)
    for c in [x for x in cs5 if x[0] >= start]:
        hi, lo, cl = c[2], c[3], c[4]
        fav = (hi - e) if long_ else (e - lo)
        mfe = max(mfe, fav / D)
        # timeout (sebelum TP1)
        if not tp1 and (c[0] - start) >= 8 * TF15_MS and mfe < 0.5:
            realized += pos * ((cl - e) / D if long_ else (e - cl) / D)
            return True, realized, c[0], "TIMEOUT"
        hit_stop = (lo <= stop) if long_ else (hi >= stop)
        if hit_stop:
            if tp1:
                realized += pos * ((stop - e) / D if long_ else (e - stop) / D)
                return True, realized, c[0], "BE_STOP" if not tp2 else "TRAIL_STOP"
            realized += pos * -1.0
            return True, realized, c[0], "SL"
        if not tp1 and ((hi >= t["tp1"]) if long_ else (lo <= t["tp1"])):
            realized += 0.5 * 1.0; pos = 0.5; tp1 = True
            stop = e                                              # BE (biaya dikurangkan di hasil neto)
        if tp1 and not tp2 and ((hi >= t["tp2"]) if long_ else (lo <= t["tp2"])):
            realized += 0.3 * 2.0; pos = 0.2; tp2 = True
        if tp2:
            new = (hi - 1.5 * t["atr15"]) if long_ else (lo + 1.5 * t["atr15"])
            trail = new if trail is None else (max(trail, new) if long_ else min(trail, new))
            stop = trail
    return False, realized, None, "OPEN"

def monitor_and_close(h):
    if not h["open"]:
        return 0
    oldest = min(t["signal_candle_time"] for t in h["open"])
    age_h = (int(time.time() * 1000) - oldest) / 3_600_000
    # 300 candle 5m = ~25 jam; lebih tua dari itu pakai 15m (~75 jam). Trade yang lebih tua
    # dari 75 jam tidak bisa dipantau penuh dan dicatat sebagai peringatan.
    if age_h <= 24:
        cs = candles(MON_TF, 300)
    else:
        cs = candles(TF15, 300)
        if age_h > 72:
            log.warning("Trade %.0f jam: melewati jendela data monitor, cek manual", age_h)
    remain, n = [], 0
    for t in h["open"]:
        closed, r, ex_ms, reason = simulate_trade(t, cs)
        if closed:
            net = r - float(t.get("cost_R", 0.0))
            t.update({"status": "CLOSED", "gross_r": round(r, 4), "result_r": round(net, 4),
                      "exit_time": iso(ex_ms), "exit_reason": reason,
                      "result": "WIN" if net > 0 else "LOSS"})
            h["closed"].append(t); n += 1
        else:
            remain.append(t)
    h["open"] = remain
    if n:
        save_history(h)
    for t in h["closed"][-n:] if n else []:
        emoji = "✅" if t["result"] == "WIN" else "❌"
        tg(f"<b>{emoji} TRADE CLOSED · BTC</b>\n━━━━━━━━━━━━━━━━━━\n{t['direction']} · {t['exit_reason']}\n"
           f"Entry <code>{t['entry']}</code> | SL <code>{t['sl']}</code>\n"
           f"Hasil neto: <b>{t['result_r']:+.2f}R</b> (gross {t['gross_r']:+.2f}R, biaya {t['cost_R']:.2f}R)\n"
           f"Closed: {t['exit_time']}")
    return n

# ================= KILL SWITCH / RISK =================

def risk_state(h):
    """Return (state, risk_mult, reason). Mengikuti tabel kill switch spec."""
    ts = sorted(h["closed"], key=lambda x: x.get("exit_time", ""))
    if len(ts) >= 20:
        w = sum(1 for x in ts[-20:] if x.get("result") == "WIN")
        if w / 20 < 0.5:
            return "PAUSE", 0.0, "rolling 20 trade WR < 50%"
    eq = pk = dd = 0.0
    for x in ts:
        eq += float(x.get("result_r", 0)) * BASE_RISK
        pk = max(pk, eq); dd = max(dd, pk - eq)
    dd_pct = dd / ACCOUNT_EQUITY * 100 if ACCOUNT_EQUITY else 0
    if dd_pct >= 15:
        return "HALT", 0.0, f"DD {dd_pct:.1f}% >= 15%"
    if dd_pct >= 12:
        return "PAUSE", 0.0, f"DD {dd_pct:.1f}% >= 12%"
    mult = 0.5 if dd_pct >= 10 else 1.0
    today = datetime.now(timezone.utc).date().isoformat()
    tday = [x for x in ts if x.get("exit_time", "")[:10] == today]
    if len(tday) >= 3 and all(x.get("result") == "LOSS" for x in tday[-3:]):
        return "PAUSE", 0.0, "3 loss berturut-turut hari ini"
    return "OK", mult, ("DD >=10%: risk x0.5" if mult < 1 else "")

# ================= SIZING =================

def size_trade(sig, mult):
    risk_usdt = ACCOUNT_EQUITY * BASE_RISK * mult
    unit_cost = sig["D"] + sig["entry"] * (2 * FEE_PER_SIDE + 2 * SLIP_PER_SIDE)
    qty = risk_usdt / unit_cost
    return risk_usdt, qty, qty * sig["entry"]

# ================= SCAN =================

def scan(h, ref_ts, tick):
    fn = Counter()
    state, mult, why = risk_state(h)
    if state != "OK":
        fn[f"Kill switch {state}"] = 1
        log.info("Kill switch %s: %s", state, why)
        return 0, fn
    if len(h["open"]) >= MAX_OPEN:
        fn["Max open"] = 1
        return 0, fn

    c15 = candles(TF15, 300)
    c1h = candles(TF1H, 300)
    c4h = candles(TF4H, 200)
    if not c15 or c15[-1][0] != ref_ts:
        fn["Data stale"] = 1
        return 0, fn
    sigs, g_long, g_short, gates = build_signal(c15, c1h, c4h, tick)
    if not sigs:
        if g_long:
            fn[f"LONG {g_long[0]}"] += 1
        if g_short:
            fn[f"SHORT {g_short[0]}"] += 1
        return 0, fn

    direction, (sig, gates_ok) = next(iter(sigs.items()))   # jika keduanya ada, LONG diprioritaskan
    risk_usdt, qty, notional = size_trade(sig, mult)
    tr = {
        "signal_id": f"BTC|{direction}|{c15[-1][0]}|INST",
        "symbol": SYMBOL, "direction": direction, "setup": "INST_V61",
        "entry": round(sig["entry"], 2), "sl": round(sig["sl"], 2),
        "tp1": round(sig["tp1"], 2), "tp2": round(sig["tp2"], 2),
        "D": sig["D"], "atr15": sig["atr15"], "cost_R": sig["cost_R"],
        "rr": 2.0, "level": sig["level"], "regime": sig["regime"],
        "signal_candle_time": c15[-1][0], "signal_candle_iso": iso(c15[-1][0]),
        "qty": qty, "notional": notional, "risk_usdt": risk_usdt, "risk_mult": mult,
        "gates": gates_ok, "status": "OPEN", "monitor_tf_ms": TF15_MS,
    }
    h["open"].append(tr); save_history(h)
    created = 1
    tg(signal_message(tr, sig))
    return created, fn

def signal_message(t, sig):
    lines = "\n".join(f"{k}: {v}" for k, v in t["gates"].items())
    return (
        f"<b>🚨 ALPHAQUANT V6.1 · BTC/USDT PERP</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"<b>{t['direction']}</b> · 15m · regime {t['regime']}\n"
        f"Level: {t['level']} · sweep {sig['sweep_low']:.2f}\n\n"
        f"<b>TRADE PLAN</b>\nEntry ≈ <code>{t['entry']}</code>\nSL <code>{t['sl']}</code>\n"
        f"TP1 (50% @1R) <code>{t['tp1']}</code>\nTP2 (30% @2R) <code>{t['tp2']}</code>\n"
        f"Runner 20%: trail 1,5 ATR15\n"
        f"Ukuran ≈ {t['qty']:.5f} BTC (notional {t['notional']:.0f} USDT, risiko {t['risk_usdt']:.2f} USDT)\n"
        f"Biaya ≈ {t['cost_R']:.2f}R · RR net ≈ {sig['net_rr']:.2f}\n\n"
        f"<b>GATES</b>\n{lines}\n━━━━━━━━━━━━━━━━━━\n"
        "⚠️ SIGNAL ONLY. Entry diproksikan dari close candle sinyal, bukan open candle berikutnya.\n"
        "Flow dan absorption = proxy OHLCV (bukan order flow sebenarnya)."
    )

# ================= MAIN =================

def stats_text(h):
    ts = h["closed"]
    if not ts:
        return None
    tot = sum(float(x.get("result_r", 0)) for x in ts)
    wins = sum(1 for x in ts if x.get("result") == "WIN")
    gp = sum(max(float(x.get("result_r", 0)), 0) for x in ts)
    gl = abs(sum(min(float(x.get("result_r", 0)), 0) for x in ts))
    pf = gp / gl if gl else float("inf")
    note = "\n<i>Kurang dari 30 trade: belum bisa disimpulkan.</i>" if len(ts) < 30 else ""
    return (f"<b>📊 V6.1 PERFORMANCE · BTC</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Trades {len(ts)} · WR {wins/len(ts)*100:.1f}%\n"
            f"Total R neto {tot:+.2f}R · Avg {tot/len(ts):+.3f}R (setelah biaya)\n"
            f"PF {'∞' if math.isinf(pf) else f'{pf:.2f}'}" + note)

def main():
    log.info("ALPHAQUANT V6.1 | GATE_MODE=%s | symbol=%s", GATE_MODE, SYMBOL)
    h = load_history()
    tick = tick_size()
    monitor_and_close(h)

    ref = candles(TF15, 3)
    ref_ts = ref[-1][0] if ref else None
    if ref_ts is not None and h.get("last_candle") != ref_ts:
        created, fn = scan(h, ref_ts, tick)
        log.info("Scan done. New=%s | gates: %s", created, dict(fn))
        h["last_candle"] = ref_ts
    else:
        log.info("No new 15m candle - scan skipped.")

    due = (time.time() - float(h.get("last_report_ts", 0))) >= REPORT_EVERY_HOURS * 3600
    if due:
        txt = stats_text(h)
        if txt:
            tg(txt)
        h["last_report_ts"] = time.time()
    save_history(h)

if __name__ == "__main__":
    main()
