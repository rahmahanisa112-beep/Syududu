"""
ALPHAQUANT V5.2
SMC Scalp 15m - Telegram signal engine (signal-only) for the top 50 OKX USDT perpetuals

Only one strategy remains: Smart Money Concept scalp on the 15m timeframe.

  1. Liquidity sweep of a recent swing (wick through, close back inside)
  2. Structure BEFORE the sweep agrees with the trade (BOS / CHOCH)
  3. Displacement candle after the sweep
  4. Order block = last opposing candle before the displacement
  5. Retest of the order block AFTER the displacement + confirmation candle
     (must be the latest candle; entry = its close)
  6. SL beyond OB / swept level / sweep wick, TP at TARGET_RR

V5.2 changes (tighter filters)
- Volume gate on the signal candle: BOS >= MIN_VOLUME_RATIO_BOS, CHOCH >= MIN_VOLUME_RATIO_CHOCH
- RSI deadzone (47-50): mode PENALTY = extra volume required, BLOCK = rejected, OFF = ignored
- Crowding: max MAX_ACTIVE_SMC_PER_DIRECTION open positions per direction and
  max MAX_NEW_SMC_PER_CANDLE new signals per candle (best-ranked pairs first)
- TARGET_RR replaces SMC_RR

V5.1 changes
- Trades of the removed strategies (Breakout, Mean Rev, ...) are moved to
  h["archive"] on start-up: no more monitoring, notifications or statistics for them
- Notifications and statistics now only cover SMC scalp

V5.0 changes
- 4H setups, ICT and Adaptive removed (they failed backtests)
- SMC scans the top TOP_PAIRS pairs by 24h volume (default 50) on every new 15m candle,
  24 hours a day (there is no session filter; run the workflow every 15 minutes)
- Cost gate: SL must be >= SMC_MIN_RISK_PCT of price (default 0.4%)
- Portfolio cap: at most SMC_MAX_OPEN open scalps (default 5)
- Periodic report (every REPORT_EVERY_HOURS) with the scan funnel + near misses

Backtests of the previous BTC-only version were negative after fees. Treat every
signal as unproven until backtest.py shows an edge on the pairs you actually scan.
"""

from __future__ import annotations

import os, json, time, math, logging, hmac, hashlib, urllib.parse
from datetime import datetime, timezone
import requests

# ================= CONFIG =================

OKX = "https://www.okx.com"

def env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""

TOKEN = env_first("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
CHAT_ID = env_first("CHAT_ID", "TELEGRAM_CHAT_ID", "TELEGRAM_CHAT")

# --- SMC scalp ---
SMC_TF = "15m"
SMC_TF_MS = 15 * 60_000
SMC_MONITOR_TF = "5m"                 # finer candles to resolve TP/SL of open scalps
SMC_MONITOR_MS = 5 * 60_000
SMC_SYMBOL = "BTC-USDT-SWAP"          # reference symbol for the "new 15m candle" gate
SMC_HISTORY_BARS = 200
SMC_MIN_BARS = 80
SMC_SWING = 3

# ---- CORE ----
TARGET_RR = float(os.getenv("TARGET_RR", "1.75"))

# ---- VOLUME: volume of the signal candle / average volume of the previous candles ----
MIN_VOLUME_RATIO_BOS = float(os.getenv("MIN_VOLUME_RATIO_BOS", "0.50"))
MIN_VOLUME_RATIO_CHOCH = float(os.getenv("MIN_VOLUME_RATIO_CHOCH", "0.75"))

# ---- POSITION CROWDING (0 = no limit) ----
MAX_ACTIVE_SMC_PER_DIRECTION = int(os.getenv("MAX_ACTIVE_SMC_PER_DIRECTION", "3"))
MAX_NEW_SMC_PER_CANDLE = int(os.getenv("MAX_NEW_SMC_PER_CANDLE", "2"))

# ---- RSI deadzone: RSI of the signal candle between LOW and HIGH = undecided momentum ----
RSI_DEADZONE_LOW = float(os.getenv("RSI_DEADZONE_LOW", "47"))
RSI_DEADZONE_HIGH = float(os.getenv("RSI_DEADZONE_HIGH", "50"))
# OFF     : ignore the deadzone
# BLOCK   : reject signals whose RSI is inside the deadzone
# PENALTY : allow them, but they need extra volume (RSI_DEADZONE_EXTRA_VOLUME added to the minimum)
RSI_DEADZONE_MODE = os.getenv("RSI_DEADZONE_MODE", "PENALTY").strip().upper()
RSI_DEADZONE_EXTRA_VOLUME = float(os.getenv("RSI_DEADZONE_EXTRA_VOLUME", "0.25"))
SMC_MIN_SL_ATR = 0.35
SMC_MAX_SL_ATR = 2.50
SMC_DISP_BODY_MIN = 0.55
SMC_DISP_ATR_MIN = 0.80
SMC_OB_LOOKBACK = 12
SMC_SWEEP_TOL_ATR = 0.15
SMC_CONFIRM_BARS = 1
SMC_COOLDOWN_HOURS = 4
SMC_MIN_RISK_PCT = float(os.getenv("SMC_MIN_RISK_PCT", "0.004"))   # 0 disables the cost gate
# Separate cost gate for BTC only (BTC moves little relative to its price, so the
# global 0.4% gate rejects almost every BTC signal). Negative = use SMC_MIN_RISK_PCT.
SMC_MIN_RISK_PCT_BTC = float(os.getenv("SMC_MIN_RISK_PCT_BTC", "-1"))
SMC_MAX_OPEN = int(os.getenv("SMC_MAX_OPEN", "5"))

# --- universe ---
TOP_PAIRS = int(os.getenv("TOP_PAIRS", "50"))
MIN_VOLUME_USDT = float(os.getenv("MIN_VOLUME_USDT", "3000000"))

# --- indicators ---
RSI_PERIOD = 14
ATR_PERIOD = 14
VOL_PERIOD = 20
SL_ATR_BUFFER = 0.30

# --- risk / reporting ---
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.004"))
ACCOUNT_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "1000"))
FEE_PER_SIDE = float(os.getenv("FEE_PER_SIDE", "0.0005"))          # taker 0.05%, for the fee estimate
REPORT_EVERY_HOURS = float(os.getenv("REPORT_EVERY_HOURS", "4"))



MONITOR_MAX_BARS = 1400
RR = 2.0                              # fallback when a stored trade has no "rr"

SCAN_INTERVAL = 300
RUN_FOREVER = os.getenv("RUN_FOREVER", "0").strip() == "1"
HISTORY_FILE = os.getenv("SIGNAL_HISTORY_FILE", "signals_history.json")
TIMEOUT = 15
RETRIES = 3
MIN_REQUEST_GAP = 0.09                # OKX public limit is 20 requests / 2 s per IP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("V5.2")
S = requests.Session()
S.headers.update({"User-Agent": "ALPHAQUANT-V5.2"})
TELEGRAM = bool(TOKEN and CHAT_ID)

SETUP_NAME = {"SMC_SCALP": "SMC Scalp"}

# ================= HELPERS =================


def iso(ms):
    return datetime.fromtimestamp(
        ms / 1000, tz=timezone.utc
    ).isoformat()

def now():
    return datetime.now(timezone.utc).isoformat()

_LAST_REQ = [0.0]

def api(path, params=None):
    for attempt in range(RETRIES):
        wait = MIN_REQUEST_GAP - (time.time() - _LAST_REQ[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_REQ[0] = time.time()
        try:
            r = S.get(
                OKX + path,
                params=params,
                timeout=TIMEOUT
            )
            r.raise_for_status()
            d = r.json()
            if d.get("code") == "0":
                return d
            log.warning("OKX error: %s", d)
        except Exception as e:
            log.warning(
                "Request failed %s/%s: %s",
                attempt + 1, RETRIES, e
            )
        if attempt + 1 < RETRIES:
            time.sleep(1.5)
    return None


def tg(text):
    if not TELEGRAM:
        return False
    try:
        r = S.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=TIMEOUT
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("Telegram error: %s", e)
        return False

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {"open": [], "closed": []}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("open", [])
        d.setdefault("closed", [])
        # last_scan_candle / last_smc_candle are optional keys preserved as-is
        return d
    except Exception as e:
        log.error("History load failed: %s", e)
        return {"open": [], "closed": []}

def archive_legacy(h):
    """
    Move trades of removed strategies (Breakout, Mean Rev, ...) out of open/closed.
    They are kept under h["archive"] (not deleted) but are never monitored,
    reported or counted again. Returns how many trades were moved.
    """
    moved = 0
    for key in ("open", "closed"):
        keep, drop = [], []
        for x in h.get(key, []):
            (keep if x.get("setup") == "SMC_SCALP" else drop).append(x)
        if drop:
            h[key] = keep
            h.setdefault("archive", []).extend(dict(x, archived_from=key) for x in drop)
            moved += len(drop)
    return moved

def save_history(h):
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, indent=2, ensure_ascii=False)
    os.replace(tmp, HISTORY_FILE)

# ================= MARKET DATA =================

def top_pairs():
    d = api("/api/v5/market/tickers", {"instType": "SWAP"})
    if not d:
        return []
    a = []
    for x in d.get("data", []):
        inst = x.get("instId", "")
        if not inst.endswith("-USDT-SWAP"):
            continue
        try:
            v = float(x.get("volCcy24h", 0))
        except Exception:
            continue
        if v >= MIN_VOLUME_USDT:
            a.append((inst, v))
    a.sort(key=lambda x: x[1], reverse=True)
    return [x[0] for x in a[:TOP_PAIRS]]


def instruments():
    d = api("/api/v5/public/instruments", {"instType": "SWAP"})
    out = {}
    if not d:
        return out
    for x in d.get("data", []):
        inst = x.get("instId", "")
        if inst.endswith("-USDT-SWAP"):
            try:
                out[inst] = {
                    "tick": float(x.get("tickSz", "0.00000001"))
                }
            except:
                pass
    return out

def price(inst):
    d = api("/api/v5/market/ticker", {"instId": inst})
    try:
        return float(d["data"][0]["last"])
    except:
        return None

def candles(inst, bar, needed):
    """
    OKX pagination using `after`.
    Returns confirmed candles oldest -> newest.
    """
    found = {}
    cursor = None
    max_requests = math.ceil(needed / 100) + 6

    for _ in range(max_requests):
        p = {
            "instId": inst,
            "bar": bar,
            "limit": "100"
        }
        if cursor is not None:
            p["after"] = str(cursor)

        d = api("/api/v5/market/candles", p)
        if not d:
            break

        rows = d.get("data", [])
        if not rows:
            break

        oldest = None

        for x in rows:
            try:
                ts = int(x[0])
                oldest = ts if oldest is None else min(oldest, ts)

                # x[8] == 1 means confirmed candle.
                if int(x[8]) != 1:
                    continue

                found[ts] = [
                    ts,
                    float(x[1]),  # open
                    float(x[2]),  # high
                    float(x[3]),  # low
                    float(x[4]),  # close
                    float(x[5])   # volume
                ]
            except:
                continue

        if len(found) >= needed:
            break

        if oldest is None or oldest == cursor:
            break

        cursor = oldest
        time.sleep(0.12)

    out = sorted(found.values(), key=lambda x: x[0])
    return out[-needed:]

def latest_candle_ts(bar=SMC_TF, inst=SMC_SYMBOL):
    """Open time of the newest confirmed candle (BTC as the reference clock)."""
    cs = candles(inst, bar, 3)
    return cs[-1][0] if cs else None

# ================= INDICATORS =================

def rma(v, n):
    out = [None] * len(v)
    if len(v) < n:
        return out
    x = sum(v[:n]) / n
    out[n - 1] = x
    for i in range(n, len(v)):
        x = ((x * (n - 1)) + v[i]) / n
        out[i] = x
    return out

def atr(cs, n=14):
    tr = []
    for i, c in enumerate(cs):
        if i == 0:
            tr.append(c[2] - c[3])
        else:
            pc = cs[i-1][4]
            tr.append(max(
                c[2] - c[3],
                abs(c[2] - pc),
                abs(c[3] - pc)
            ))
    return rma(tr, n)

def rsi(cs, n=14):
    closes = [x[4] for x in cs]
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i-1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    ag, al = rma(gains, n), rma(losses, n)
    out = [None] * len(cs)
    for i in range(len(cs)):
        if ag[i] is None or al[i] is None:
            continue
        if al[i] == 0:
            out[i] = 100.0
        else:
            rs = ag[i] / al[i]
            out[i] = 100 - 100 / (1 + rs)
    return out

def volume_ratio(cs, n=20):
    if len(cs) < n:
        return None
    avg = sum(x[5] for x in cs[-n:]) / n
    return cs[-1][5] / avg if avg else None

def tick_round(x, tick):
    if tick <= 0:
        return x
    return round(round(x/tick)*tick, 12)

# ================= REJECT TRACKING =================

SMC_STAGES = [
    "Data", "Struktur", "Sweep", "Displacement", "OrderBlock",
    "Retest", "Konfirmasi", "RSI", "Volume", "SL/RR", "Diblokir"
]

CUR_SETUP = "SMC_SCALP"

# (setup, stage) -> [(pair, detail)], reset at the start of every scan.
REJECTS = {}

def _p(x):
    return f"{x:.8g}"


def _safe(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _rej(inst, stage, detail="", setup=None):
    key = (setup or CUR_SETUP, stage)
    REJECTS.setdefault(key, []).append((inst, _safe(detail)))
    return None

# ================= SMC SCALP (15m) =================

def _smc_swings(cs, swing=SMC_SWING):
    """Fractal swing highs/lows. Returns lists of (index, price)."""
    highs, lows = [], []
    for i in range(swing, len(cs) - swing):
        h, l = cs[i][2], cs[i][3]
        if all(h >= cs[i - j][2] for j in range(1, swing + 1)) and \
           all(h > cs[i + j][2] for j in range(1, swing + 1)):
            highs.append((i, h))
        if all(l <= cs[i - j][3] for j in range(1, swing + 1)) and \
           all(l < cs[i + j][3] for j in range(1, swing + 1)):
            lows.append((i, l))
    return highs, lows

def _smc_structure(highs, lows):
    """
    Determine last structural bias from swing sequence.
    Returns: bias ('BULL'|'BEAR'|None), event ('BOS'|'CHOCH'|None),
             last_sh, last_sl
    """
    if len(highs) < 2 or len(lows) < 2:
        return None, None, None, None

    sh1, sh0 = highs[-2], highs[-1]
    sl1, sl0 = lows[-2], lows[-1]
    last_sh, last_sl = sh0, sl0

    # Most recent swing event determines bias.
    # Higher high + higher low = bull structure; opposite = bear.
    hh = sh0[1] > sh1[1]
    hl = sl0[1] > sl1[1]
    lh = sh0[1] < sh1[1]
    ll = sl0[1] < sl1[1]

    if hh and hl:
        return "BULL", "BOS", last_sh, last_sl
    if lh and ll:
        return "BEAR", "BOS", last_sh, last_sl
    # CHOCH: only one side of structure flips
    if hh and ll:
        # conflict – use the more recent swing
        if sh0[0] > sl0[0]:
            return "BULL", "CHOCH", last_sh, last_sl
        return "BEAR", "CHOCH", last_sh, last_sl
    if lh and hl:
        if sl0[0] > sh0[0]:
            return "BEAR", "CHOCH", last_sh, last_sl
        return "BULL", "CHOCH", last_sh, last_sl
    return None, None, last_sh, last_sl

def _find_order_block(cs, side, start_i, end_i):
    """
    Order block = last opposing candle before the impulse that caused the
    structure break / sweep recovery.
    LONG  → last bearish candle (close < open) in [start_i, end_i)
    SHORT → last bullish candle
    Returns (index, high, low) or None.
    """
    if start_i < 0:
        start_i = 0
    end_i = min(end_i, len(cs))
    for i in range(end_i - 1, start_i - 1, -1):
        o, cl = cs[i][1], cs[i][4]
        if side == "LONG" and cl < o:
            return i, cs[i][2], cs[i][3]
        if side == "SHORT" and cl > o:
            return i, cs[i][2], cs[i][3]
    return None

def _is_displacement(c, atr_v, side):
    """Strong impulse candle in the trade direction."""
    o, hi, lo, cl = c[1], c[2], c[3], c[4]
    rng = hi - lo
    if rng <= 0 or not atr_v:
        return False
    body = abs(cl - o) / rng
    atr_mult = rng / atr_v
    if body < SMC_DISP_BODY_MIN or atr_mult < SMC_DISP_ATR_MIN:
        return False
    if side == "LONG":
        return cl > o
    return cl < o

def _smc_attempt(inst, cs, meta, a, rs, highs, lows, side, sweep_idx, swept_level):
    """
    Run steps 2-6 for ONE sweep candidate.
    Returns a signal dict, or (stage, detail) describing why it failed.
    """
    # Structure is judged on swings that existed BEFORE the sweep. The sweep
    # candle's own wick later becomes a swing low/high and must not flip the bias.
    pre_h = [x for x in highs if x[0] < sweep_idx]
    pre_l = [x for x in lows if x[0] < sweep_idx]
    bias, event, _, _ = _smc_structure(pre_h, pre_l)
    if bias is None:
        return "Struktur", "bias struktur tidak jelas"

    if bias != ("BULL" if side == "LONG" else "BEAR") and event != "CHOCH":
        return "Struktur", f"sweep {side} tapi bias {bias} (bukan CHOCH)"

    # --- Displacement after the sweep ---
    disp_idx = None
    for i in range(sweep_idx, min(sweep_idx + 6, len(cs))):
        if _is_displacement(cs[i], a, side):
            disp_idx = i
            break
    if disp_idx is None:
        return "Displacement", f"{side} tidak ada displacement setelah sweep"

    # --- Order block: last opposing candle before displacement ---
    ob = _find_order_block(cs, side, max(0, disp_idx - SMC_OB_LOOKBACK), disp_idx)
    if ob is None:
        return "OrderBlock", f"{side} order block tidak ditemukan"
    ob_i, ob_hi, ob_lo = ob

    # --- Retest of the order block AFTER the displacement candle ---
    confirm_idx = None
    mid_ob = (ob_hi + ob_lo) / 2
    for i in range(max(ob_i + 1, disp_idx + 1, len(cs) - 8), len(cs)):
        c = cs[i]
        if side == "LONG" and c[4] < ob_lo:
            return "Retest", "LONG order block ditembus (close di bawah OB)"
        if side == "SHORT" and c[4] > ob_hi:
            return "Retest", "SHORT order block ditembus (close di atas OB)"

        tapped = c[3] <= ob_hi and c[2] >= ob_lo
        if not tapped:
            continue
        if side == "LONG" and c[4] > mid_ob and c[4] > c[1]:
            confirm_idx = i
            break
        if side == "SHORT" and c[4] < mid_ob and c[4] < c[1]:
            confirm_idx = i
            break

    if confirm_idx is None:
        return "Retest", f"{side} belum retest order block"

    # Confirmation must be the latest candle (entry = its close)
    if confirm_idx < len(cs) - SMC_CONFIRM_BARS:
        return (
            "Konfirmasi",
            f"{side} konfirmasi terlalu lama ({len(cs) - 1 - confirm_idx} bar lalu)"
        )

    close = cs[-1][4]

    # --- quality gates on the confirmation candle: RSI deadzone, then volume ---
    vr = volume_ratio(cs, VOL_PERIOD) or 0.0
    kind = event or "BOS"
    need_vol = MIN_VOLUME_RATIO_CHOCH if kind == "CHOCH" else MIN_VOLUME_RATIO_BOS
    in_dead = RSI_DEADZONE_LOW <= rs[-1] <= RSI_DEADZONE_HIGH
    penalised = False

    if in_dead and RSI_DEADZONE_MODE == "BLOCK":
        return (
            "RSI",
            f"{side} RSI {rs[-1]:.1f} di deadzone {RSI_DEADZONE_LOW:g}-{RSI_DEADZONE_HIGH:g} (mode BLOCK)"
        )
    if in_dead and RSI_DEADZONE_MODE == "PENALTY":
        need_vol += RSI_DEADZONE_EXTRA_VOLUME
        penalised = True

    if vr < need_vol:
        extra = f", RSI {rs[-1]:.1f} di deadzone +{RSI_DEADZONE_EXTRA_VOLUME:g}" if penalised else ""
        return (
            "Volume",
            f"{side} {kind} volume {vr:.2f}x (min {need_vol:.2f}x{extra})"
        )

    # SL: beyond the OB extreme, the swept level and the sweep wick
    if side == "LONG":
        sweep_ext = cs[sweep_idx][3]
        sl_raw = min(ob_lo, swept_level, sweep_ext) - SL_ATR_BUFFER * a
        risk = close - sl_raw
    else:
        sweep_ext = cs[sweep_idx][2]
        sl_raw = max(ob_hi, swept_level, sweep_ext) + SL_ATR_BUFFER * a
        risk = sl_raw - close

    if risk <= 0:
        return "SL/RR", f"{side} SL tidak valid"

    sl_atr = risk / a
    if not SMC_MIN_SL_ATR <= sl_atr <= SMC_MAX_SL_ATR:
        return (
            "SL/RR",
            f"{side} SL {sl_atr:.2f} ATR (perlu {SMC_MIN_SL_ATR}-{SMC_MAX_SL_ATR})"
        )

    # Cost gate: with ~0.10% round-trip taker fees, a tiny SL makes fees eat the R.
    risk_pct = risk / close
    min_risk = SMC_MIN_RISK_PCT_BTC if (inst == SMC_SYMBOL and SMC_MIN_RISK_PCT_BTC >= 0) \
        else SMC_MIN_RISK_PCT
    if risk_pct < min_risk:
        return (
            "SL/RR",
            f"{side} SL {risk_pct * 100:.2f}% (min {min_risk * 100:.2f}%, fee memakan R)"
        )

    tp_raw = close + TARGET_RR * risk if side == "LONG" else close - TARGET_RR * risk

    tick = meta.get(inst, {}).get("tick", 0.00000001)
    entry = tick_round(close, tick)
    sl = tick_round(sl_raw, tick)
    tp = tick_round(tp_raw, tick)

    note = (
        f"SMC {event or 'BOS'} {bias} | "
        f"Sweep {'low' if side == 'LONG' else 'high'} @ {swept_level:.8g} | "
        f"OB [{ob_lo:.8g}-{ob_hi:.8g}] | "
        f"Disp bar {disp_idx} | RSI {rs[-1]:.1f} | Vol {vr:.2f}x"
    )

    return {
        "signal_id": f"{inst}|{side}|{cs[-1][0]}|SMC",
        "symbol": inst,
        "direction": side,
        "timeframe": SMC_TF,
        "setup": "SMC_SCALP",
        "note": note,
        "signal_candle_time": cs[-1][0],
        "signal_candle_iso": iso(cs[-1][0]),
        "created_at": now(),
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": TARGET_RR,
        "atr": a,
        "sl_atr": sl_atr,
        "ema50": 0.0,
        "ema200": 0.0,
        "rsi": rs[-1],
        "adx": 0.0,
        "volume_ratio": vr,
        "rsi_deadzone": bool(in_dead),
        "regime": bias or "-",
        "structure": event or "-",
        "bos": event or "NONE",
        "pullback": None,
        "score": None,
        "score_reasons": [],
        "risk_usdt": ACCOUNT_EQUITY * RISK_PER_TRADE,
        "status": "OPEN",
        "exit_price": None,
        "exit_time": None,
        "result_r": None,
        "result": None,
        "monitor_tf_ms": SMC_TF_MS,
    }

def build_smc_scalp(inst, cs, meta):
    """
    15m Smart Money Concept scalp, any USDT perpetual (long; short is mirrored):
      1. Liquidity sweep of a recent swing (wick through, close back inside)
      2. Structure BEFORE the sweep agrees with the trade (BOS/CHOCH)
      3. Displacement candle after the sweep
      4. Order block = last opposing candle before the displacement
      5. Price retests the OB AFTER the displacement and prints a
         confirmation candle (must be the latest candle)
      6. Entry at close, SL beyond OB / swept level / sweep wick, TP at TARGET_RR
    Both directions are evaluated; the first valid one wins.
    """
    global CUR_SETUP
    CUR_SETUP = "SMC_SCALP"

    if len(cs) < SMC_MIN_BARS:
        return _rej(inst, "Data", f"hanya {len(cs)} candle (min {SMC_MIN_BARS})")

    at = atr(cs, ATR_PERIOD)
    rs = rsi(cs, RSI_PERIOD)
    if at[-1] is None or rs[-1] is None:
        return _rej(inst, "Data", "indikator belum siap")

    a = at[-1]
    if a <= 0:
        return _rej(inst, "Data", "ATR nol")

    highs, lows = _smc_swings(cs, SMC_SWING)
    if len(highs) < 2 or len(lows) < 2:
        return _rej(inst, "Struktur", "swing high/low kurang dari 2")

    # --- Sweep candidates (last 15 bars), newest first, both directions ---
    cands = []
    for side in ("LONG", "SHORT"):
        pool = lows if side == "LONG" else highs
        for abs_i in range(len(cs) - 1, max(0, len(cs) - 15) - 1, -1):
            c = cs[abs_i]
            for si, p in pool:
                if si >= abs_i or abs_i - si > 30:
                    continue
                if side == "LONG":
                    ok = (p - c[3]) / a >= SMC_SWEEP_TOL_ATR and c[4] > p
                else:
                    ok = (c[2] - p) / a >= SMC_SWEEP_TOL_ATR and c[4] < p
                if ok:
                    cands.append((side, abs_i, p))
                    break

    if not cands:
        return _rej(inst, "Sweep", "tidak ada liquidity sweep terkini")

    best = None
    for side, sweep_idx, swept in cands:
        res = _smc_attempt(inst, cs, meta, a, rs, highs, lows, side, sweep_idx, swept)
        if isinstance(res, dict):
            return res
        stage, detail = res
        depth = SMC_STAGES.index(stage)
        if best is None or depth > best[0]:
            best = (depth, stage, detail)

    return _rej(inst, best[1], best[2])

# ================= DEDUPE =================

def symbol_blocked(h, inst):
    """True if `inst` already has an open trade, or is cooling down after a close."""
    if any(x.get("symbol") == inst for x in h["open"]):
        return True
    cutoff = time.time() - SMC_COOLDOWN_HOURS * 3600
    for x in h["closed"]:
        if x.get("symbol") != inst:
            continue
        try:
            if datetime.fromisoformat(x["exit_time"]).timestamp() > cutoff:
                return True
        except Exception:
            pass
    return False

def blocked(h, sig):
    sid = sig["signal_id"]
    if any(x.get("signal_id") == sid for x in h["open"] + h["closed"]):
        return True
    return symbol_blocked(h, sig["symbol"])

def n_open(h):
    return len(h["open"])

# ================= MONITOR =================

def monitor(trade):
    # Entry = close of the signal candle: monitoring starts when that candle ENDS.
    start = trade.get("signal_candle_time", 0) + SMC_TF_MS
    mon_tf, mon_ms = SMC_MONITOR_TF, SMC_MONITOR_MS

    age_ms = max(0, int(time.time() * 1000) - start)
    needed = max(60, age_ms // mon_ms + 24)
    if needed > MONITOR_MAX_BARS:
        log.warning(
            "Monitor %s: trade age needs %s %s bars, capped at %s "
            "(older TP/SL hits may be missed)",
            trade["symbol"], needed, mon_tf, MONITOR_MAX_BARS
        )
        needed = MONITOR_MAX_BARS

    cs = candles(trade["symbol"], mon_tf, needed)
    if not cs:
        return None

    for c in [x for x in cs if x[0] >= start]:
        hi, lo = c[2], c[3]
        sl, tp = trade["sl"], trade["tp"]

        if trade["direction"] == "LONG":
            hit_sl, hit_tp = lo <= sl, hi >= tp
        else:
            hit_sl, hit_tp = hi >= sl, lo <= tp

        # Same monitor candle touches both: conservative SL first.
        if hit_sl and hit_tp:
            exit_price, r, result = sl, -1.0, "LOSS"
        elif hit_tp:
            exit_price, r, result = tp, float(trade.get("rr", RR)), "WIN"
        elif hit_sl:
            exit_price, r, result = sl, -1.0, "LOSS"
        else:
            continue

        t = dict(trade)
        t.update({
            "status": "CLOSED",
            "exit_price": exit_price,
            "exit_time": iso(c[0]),
            "result_r": r,
            "result": result
        })
        return t
    return None

def check_open(h):
    if not h["open"]:
        return 0

    remain, closed = [], []

    for t in h["open"]:
        try:
            result = monitor(t)
            if result:
                closed.append(result)
            else:
                remain.append(t)
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
            f"<b>{t['symbol']}</b> {t['direction']}\n"
            f"Entry: <code>{t['entry']}</code>\n"
            f"Exit: <code>{t['exit_price']}</code>\n"
            f"Result: <b>{t['result_r']:+.2f}R</b>\n"
            f"Closed: {t['exit_time']}"
        )

    return len(closed)

# ================= REPORT =================


def float_r(t, p):
    risk = abs(t["entry"]-t["sl"])
    if risk <= 0:
        return 0
    return ((p-t["entry"])/risk
            if t["direction"]=="LONG"
            else (t["entry"]-p)/risk)

def running_report(h):
    if not h["open"]:
        return

    blocks, rs = [], []

    for t in h["open"]:
        p = price(t["symbol"])
        if p is None:
            continue
        r = float_r(t, p)
        rs.append(r)
        emoji = "🟢" if r > 0 else "🔴" if r < 0 else "⚪"
        blocks.append(
            f"{emoji} <b>{t['symbol']}</b> · {t['direction']}\n"
            f"Entry <code>{t['entry']}</code> | Now <code>{p}</code>\n"
            f"SL <code>{t['sl']}</code> | TP <code>{t['tp']}</code>\n"
            f"Float <b>{r:+.2f}R</b>"
        )

    if not rs:
        return

    total = sum(rs)
    avg = total / len(rs)

    tg(
        "<b>📡 RUNNING POSITIONS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n" +
        "\n\n".join(blocks) +
        "\n\n━━━━━━━━━━━━━━━━━━\n"
        f"Open Positions : {len(h['open'])}\n"
        f"Profitable     : {sum(x > 0 for x in rs)}\n"
        f"Negative       : {sum(x < 0 for x in rs)}\n"
        f"Floating R     : <b>{total:+.2f}R</b>\n"
        f"Average Float  : {avg:+.2f}R\n"
        f"Best           : {max(rs):+.2f}R\n"
        f"Worst          : {min(rs):+.2f}R\n\n"
        f"TP Potential   : +{sum(float(x.get('rr', RR)) for x in h['open']):.2f}R\n"
        f"SL Exposure    : -{len(h['open']):.2f}R\n\n"
        "⚠️ Floating R is unrealized."
    )

def fee_r_of(x):
    """Estimated round-trip taker fee of a trade, in R."""
    try:
        entry = float(x["entry"])
        risk = abs(entry - float(x["sl"]))
        return (2 * FEE_PER_SIDE * entry / risk) if risk > 0 else 0.0
    except Exception:
        return 0.0

def stats(h):
    ts = h["closed"]
    if not ts:
        return

    wins = [x for x in ts if x.get("result") == "WIN"]
    losses = [x for x in ts if x.get("result") == "LOSS"]
    total_r = sum(float(x.get("result_r", 0)) for x in ts)
    gp = sum(max(float(x.get("result_r", 0)), 0) for x in ts)
    gl = abs(sum(min(float(x.get("result_r", 0)), 0) for x in ts))
    pf = gp / gl if gl else float("inf")

    eq = peak = dd = 0
    for x in ts:
        eq += float(x.get("result_r", 0))
        peak = max(peak, eq)
        dd = max(dd, peak - eq)

    pf_text = "∞" if math.isinf(pf) else f"{pf:.2f}"

    rrs = [float(x.get("rr", RR)) for x in ts]
    avg_rr = sum(rrs) / len(rrs)
    bep = 100 / (1 + avg_rr)
    fees = [fee_r_of(x) for x in ts]
    net_r = total_r - sum(fees)
    bep_net = 100 * (1 + sum(fees) / len(ts)) / (1 + avg_rr)

    note = ""
    if len(ts) < 30:
        note = "\n\n<i>Kurang dari 30 trade: belum bisa disimpulkan.</i>"

    tg(
        "<b>📊 V5.2 PERFORMANCE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Trades        : {len(ts)}\n"
        f"Wins          : {len(wins)}\n"
        f"Losses        : {len(losses)}\n"
        f"Win Rate      : <b>{len(wins) / len(ts) * 100:.2f}%</b>\n"
        f"Total R       : <b>{total_r:+.2f}R</b> (gross)\n"
        f"Total R net   : <b>{net_r:+.2f}R</b> (setelah fee ≈)\n"
        f"Avg R/Trade   : {total_r / len(ts):+.3f}R gross | {net_r / len(ts):+.3f}R net\n"
        f"Profit Factor : {pf_text}\n"
        f"Max Drawdown  : -{dd:.2f}R\n"
        f"Avg RR Target : 1:{avg_rr:.2f}\n"
        f"BEP WR        : {bep:.2f}% gross | <b>{bep_net:.2f}%</b> net" + note
    )

# ================= SIGNAL MESSAGE =================

def signal_message(s):
    setup = s.get("setup", "SMC_SCALP")
    risk = abs(s["entry"] - s["sl"])
    fee_r = (2 * FEE_PER_SIDE * s["entry"] / risk) if risk > 0 else 0.0
    risk_pct = (risk / s["entry"] * 100) if s["entry"] else 0.0

    head = (
        f"{s['timeframe']} | <b>{SETUP_NAME.get(setup, setup)}</b>\n"
        f"Structure: {s.get('structure', '-')} · Regime: {s.get('regime', '-')}\n"
        f"{_safe(s.get('note', ''))}\n\n"
    )

    return (
        "<b>🚨 ALPHAQUANT V5.2 SIGNAL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>{s['symbol']}</b> · <b>{s['direction']}</b>\n" +
        head +
        "<b>TRADE PLAN</b>\n"
        f"Entry: <code>{s['entry']}</code>\n"
        f"SL: <code>{s['sl']}</code>\n"
        f"TP: <code>{s['tp']}</code>\n"
        f"RR: <b>1:{float(s.get('rr', RR)):.2f}</b>\n"
        f"SL: {s['sl_atr']:.2f} ATR · {risk_pct:.2f}% harga\n"
        f"Fee ≈ {fee_r:.2f}R (taker {FEE_PER_SIDE * 100:.2f}%/sisi)\n\n"
        f"RSI: {s['rsi']:.2f} | Volume: {s['volume_ratio']:.2f}x\n"
        f"Risk: {s['risk_usdt']:.2f} USDT\n"
        "━━━━━━━━━━━━━━━━━━\n" +
        "⚠️ SIGNAL ONLY — NO AUTO EXECUTION\n"
        f"Candle: {s['signal_candle_iso']}"
    )

# ================= SCAN =================

def _tally(h, scanned, created, rows):
    """Accumulate the scan funnel in the history file until the next report."""
    d = h.get("smc_rej")
    if not d or "pairs" not in d:           # empty, or left over from V4.x (different format)
        d = {"scans": 0, "pairs": 0, "signals": 0, "stages": {}, "near": []}
    d["scans"] += 1
    d["pairs"] += scanned
    d["signals"] += created

    for (_, stage), items in rows.items():
        d["stages"][stage] = d["stages"].get(stage, 0) + len(items)
        if stage in SMC_STAGES and stage not in ("Data", "Struktur", "Sweep"):
            depth = SMC_STAGES.index(stage)
            for inst, detail in items:
                d["near"].append({"depth": depth, "text": f"{inst} → {stage}: {detail}"})

    d["near"] = sorted(d["near"], key=lambda x: -x["depth"])[:6]
    h["smc_rej"] = d

def smc_report_text(h):
    d = h.get("smc_rej")
    if not d or "pairs" not in d or not d.get("scans"):
        return ""
    parts_ = [f"{st} {d['stages'][st]}" for st in SMC_STAGES + ["Error"] if d["stages"].get(st)]
    text = (
        "<b>⚡ SMC SCALP · SCAN REPORT</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Scan         : {d['scans']} candle 15m\n"
        f"Pair dicek   : {d['pairs']}\n"
        f"Sinyal baru  : {d['signals']}\n\n"
        "<b>Gugur di filter</b> (filter pertama yang gagal)\n" +
        (" · ".join(parts_) if parts_ else "-")
    )
    if d.get("near"):
        text += "\n\n<b>Paling dekat lolos</b>\n" + "\n".join(x["text"] for x in d["near"][:4])
    return text

def scan_smc(h):
    """
    Scan the top pairs for an SMC scalp on every NEW 15m candle.
    Returns the number of new signals.
    """
    global CUR_SETUP

    ref = latest_candle_ts()
    if ref is not None and h.get("last_smc_candle") == ref:
        log.info("No new %s candle since %s - scan skipped.", SMC_TF, iso(ref))
        return 0

    if n_open(h) >= SMC_MAX_OPEN:
        log.info("SMC scan skipped: %s open scalps (cap %s).", n_open(h), SMC_MAX_OPEN)
        if ref is not None:
            h["last_smc_candle"] = ref
        return 0

    pairs = top_pairs()
    if not pairs:
        log.warning("No pairs returned by OKX; scan aborted.")
        return 0
    meta = instruments()

    log.info("SMC scan: %s pairs on %s...", len(pairs), SMC_TF)
    REJECTS.clear()
    CUR_SETUP = "SMC_SCALP"
    created, scanned = 0, 0

    def n_dir(d):
        return sum(1 for x in h["open"] if x.get("direction") == d)

    for inst in pairs:
        if n_open(h) >= SMC_MAX_OPEN:
            log.info("Open scalp cap (%s) reached - stopping the scan.", SMC_MAX_OPEN)
            break
        if MAX_NEW_SMC_PER_CANDLE > 0 and created >= MAX_NEW_SMC_PER_CANDLE:
            log.info("New-signal limit per candle (%s) reached - stopping the scan.", MAX_NEW_SMC_PER_CANDLE)
            break
        if (MAX_ACTIVE_SMC_PER_DIRECTION > 0
                and n_dir("LONG") >= MAX_ACTIVE_SMC_PER_DIRECTION
                and n_dir("SHORT") >= MAX_ACTIVE_SMC_PER_DIRECTION):
            log.info("Both directions are at the limit (%s) - stopping the scan.", MAX_ACTIVE_SMC_PER_DIRECTION)
            break

        # Skip pairs that already have a position / are cooling down BEFORE
        # spending API calls on them.
        if symbol_blocked(h, inst):
            continue

        scanned += 1
        try:
            cs = candles(inst, SMC_TF, SMC_HISTORY_BARS)
            if len(cs) < SMC_MIN_BARS:
                _rej(inst, "Data", f"hanya {len(cs)} candle")
                continue

            # Stale data (no trades in the latest bar): never signal on an old candle.
            if ref is not None and cs[-1][0] != ref:
                _rej(inst, "Data", "candle terakhir tidak sinkron")
                continue

            s = build_smc_scalp(inst, cs, meta)
            if not s:
                continue

            if blocked(h, s):
                _rej(inst, "Diblokir", f"{s['direction']} duplikat/cooldown/posisi open")
                continue

            if (MAX_ACTIVE_SMC_PER_DIRECTION > 0
                    and n_dir(s["direction"]) >= MAX_ACTIVE_SMC_PER_DIRECTION):
                _rej(inst, "Diblokir",
                     f"{s['direction']} sudah {n_dir(s['direction'])} posisi aktif "
                     f"(maks {MAX_ACTIVE_SMC_PER_DIRECTION})")
                continue

            h["open"].append(s)
            save_history(h)
            created += 1
            log.info("NEW SIGNAL %s %s rr=%s", s["symbol"], s["direction"], s["rr"])

            tg(signal_message(s))
            time.sleep(0.3)

        except Exception as e:
            log.exception("Scan error %s: %s", inst, e)
            _rej(inst, "Error", str(e)[:60])

    log.info("SMC scan complete. Pairs: %s | New signals: %s", scanned, created)
    _tally(h, scanned, created, dict(REJECTS))
    REJECTS.clear()

    if ref is not None:
        h["last_smc_candle"] = ref
    return created

# ================= MAIN =================

def main():
    global TELEGRAM

    log.info("==========================================")
    log.info("ALPHAQUANT V5.2 STARTING")
    log.info("==========================================")

    if not TOKEN or not CHAT_ID:
        TELEGRAM = False
        missing = []
        if not TOKEN:
            missing.append("TELEGRAM_TOKEN")
        if not CHAT_ID:
            missing.append("CHAT_ID")
        log.warning(
            "Telegram disabled. Missing Secret/Environment variable: %s",
            ", ".join(missing)
        )
        log.warning("GitHub: Settings > Secrets and variables > Actions")
    else:
        TELEGRAM = True
        log.info("Telegram credentials detected. Notifications ENABLED.")

    h = load_history()
    moved = archive_legacy(h)
    if moved:
        log.info("Archived %s trades of removed strategies (Breakout / Mean Rev / ...).", moved)
        save_history(h)

    log.info("Open trades: %s | Closed trades: %s", len(h["open"]), len(h["closed"]))
    log.info(
        "Config | SMC@%s RR=1:%.2f | Pairs=%s | MaxOpen=%s | MinRisk=%.2f%% (BTC %s) | "
        "Monitor=%s | History=%s | Telegram=%s | RunForever=%s",
        SMC_TF, TARGET_RR, TOP_PAIRS, SMC_MAX_OPEN, SMC_MIN_RISK_PCT * 100,
        f"{SMC_MIN_RISK_PCT_BTC * 100:.2f}%" if SMC_MIN_RISK_PCT_BTC >= 0 else "= sama",
        SMC_MONITOR_TF, HISTORY_FILE, "ON" if TELEGRAM else "OFF", RUN_FOREVER
    )

    today = datetime.now(timezone.utc).date().isoformat()
    if TELEGRAM and h.get("last_online_day") != today:
        h["last_online_day"] = today
        tg(
            "<b>🟢 ALPHAQUANT V5.2 ONLINE</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"SMC Scalp 15m · top {TOP_PAIRS} pair\n"
            "Sweep · Order Block · Displacement · Retest\n"
            f"Maks {SMC_MAX_OPEN} posisi ({MAX_ACTIVE_SMC_PER_DIRECTION} per arah, "
            f"{MAX_NEW_SMC_PER_CANDLE} baru per candle) · SL min {SMC_MIN_RISK_PCT * 100:.2f}% harga\n"
            "Signal-only"
        )

    while True:
        started = time.time()

        try:
            log.info("Starting engine cycle...")
            closed_n = check_open(h)

            # Periodic report: every REPORT_EVERY_HOURS, or right after a trade closed.
            due = (time.time() - float(h.get("last_report_ts", 0))) >= REPORT_EVERY_HOURS * 3600
            if due:
                running_report(h)
                stats(h)
                text = smc_report_text(h)
                if text:
                    tg(text)
                    h["smc_rej"] = None
                h["last_report_ts"] = time.time()
            elif closed_n:
                stats(h)

            scan_smc(h)
            save_history(h)
        except KeyboardInterrupt:
            log.info("Engine stopped.")
            break
        except Exception as e:
            log.exception("MAIN LOOP ERROR: %s", e)

        elapsed = time.time() - started
        log.info(
            "Cycle finished in %.1fs. History: %s | Open: %s | Closed: %s",
            elapsed, HISTORY_FILE, len(h["open"]), len(h["closed"])
        )

        if not RUN_FOREVER:
            log.info("RUN_ONCE mode: exiting for GitHub Actions.")
            break

        sleep_for = max(10, SCAN_INTERVAL - elapsed)
        log.info("Cycle finished. Sleeping %.1f seconds.", sleep_for)
        time.sleep(sleep_for)

if __name__ == "__main__":
    main()
