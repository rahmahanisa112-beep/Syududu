#!/usr/bin/env python3
"""
Regime-First Scalper - signal scanner
Data: OKX public API | Output: Telegram | Runner: GitHub Actions

Alur: ambil data 15m/1H/4H -> deteksi REGIME (1H) -> baru cari SINYAL (15m).
Usage: python signal_bot.py [scan|status|test]
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

# ----------------------------------------------------------------- config
INST_ID = os.getenv("INST_ID", "BTC-USDT-SWAP")
OKX_BASES = ["https://www.okx.com", "https://aws.okx.com"]
ENTRY_TF, REG_TF, DIR_TF = "15m", "1H", "4H"

TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or ""
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""

def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)

CFG = dict(
    confirm_bars=int(_f("CONFIRM_BARS", 1)),   # konfirmasi regime (candle 1H)
    adx_trend=25, adx_range=20, chaos_pct=90,
    sl_mult=_f("SL_MULT", 1.2),                # SL = x ATR(14) 15m
    rr=_f("RR", 2.0),
    risk_pct=_f("RISK_PCT", 0.5),              # risiko per trade (% equity)
    equity=_f("EQUITY", 1000),                 # hanya untuk contoh size
    max_lev=_f("MAX_LEV", 5),
    fee_rt=_f("FEE_RT", 0.08),                 # fee round trip (%)
    fee_mult=_f("FEE_MULT", 3.0),              # skip jika SL < x total fee
    time_stop=int(_f("TIME_STOP", 16)),        # candle 15m
    stale_frac=_f("STALE_FRAC", 0.5),          # skip jika harga geser > x SL dari entry
    fresh_min=int(_f("FRESH_MIN", 14)),        # sinyal max umur (menit) sejak candle close
)
SESSION_UTC = os.getenv("SESSION_UTC", "7-17")  # 14:00-23:59 WIB, jam UTC [start, end)

REGIME_NAME = {
    1: "TREND UP", -1: "TREND DOWN", 2: "RANGE",
    3: "BREAKOUT UP", -3: "BREAKOUT DOWN", 9: "CHAOS (no trade)", 0: "NETRAL",
}
MODULE_NAME = {"A": "Pullback", "B": "Mean Reversion", "C": "Breakout"}


# ----------------------------------------------------------------- data
def fetch_candles(bar, limit=300):
    """Return DataFrame ascending: ts, open, high, low, close, vol, confirm."""
    last_err = None
    for base in OKX_BASES:
        for attempt in range(3):
            try:
                r = requests.get(
                    f"{base}/api/v5/market/candles",
                    params={"instId": INST_ID, "bar": bar, "limit": limit},
                    timeout=15,
                )
                r.raise_for_status()
                j = r.json()
                if j.get("code") != "0":
                    raise RuntimeError(f"OKX error: {j}")
                rows = [x[:9] for x in j["data"]]
                df = pd.DataFrame(
                    rows,
                    columns=["ts", "open", "high", "low", "close", "vol",
                             "volCcy", "volCcyQuote", "confirm"],
                )
                for col in ["open", "high", "low", "close", "vol"]:
                    df[col] = df[col].astype(float)
                df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
                df["confirm"] = df["confirm"].astype(int)
                return df.sort_values("ts").reset_index(drop=True)
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(1.5)
    raise RuntimeError(f"Gagal ambil data OKX ({bar}): {last_err}")


# ----------------------------------------------------------------- indicators
def rma(s, n):
    return s.ewm(alpha=1 / n, adjust=False).mean()


def atr(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return rma(tr, n)


def adx(df, n=14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(df, n)
    di_p = 100 * rma(pd.Series(plus, index=df.index), n) / a
    di_m = 100 * rma(pd.Series(minus, index=df.index), n) / a
    dx = 100 * (di_p - di_m).abs() / (di_p + di_m)
    return rma(dx.fillna(0), n)


def rsi(s, n=7):
    d = s.diff()
    gain = rma(d.clip(lower=0), n)
    loss = rma((-d).clip(lower=0), n)
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


def percentrank(s, n):
    a = s.to_numpy()
    out = np.full(len(a), np.nan)
    for i in range(n, len(a)):
        w = a[i - n:i]
        if np.isnan(w).any() or np.isnan(a[i]):
            continue
        out[i] = (w <= a[i]).sum() / n * 100
    return pd.Series(out, index=s.index)


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


# ----------------------------------------------------------------- layer 1: regime
def regime_series(df):
    """0 netral | 1/-1 trend | 2 range | 3/-3 breakout | 9 chaos."""
    c = df["close"]
    e9, e21, e50 = ema(c, 9), ema(c, 21), ema(c, 50)
    basis = c.rolling(20).mean()
    dev = 2 * c.rolling(20).std(ddof=0)
    up, lo = basis + dev, basis - dev
    bbw = (up - lo) / basis
    bbp = percentrank(bbw, 200)
    atrp = percentrank(atr(df) / c, 200)
    volr = df["vol"] / df["vol"].rolling(20).mean()
    was_sq = bbp.rolling(6).min() < 25
    expand = bbw > bbw.shift(1)
    a = adx(df)

    r = pd.Series(0, index=df.index)
    r[(a < CFG["adx_range"]) & (bbp < 50)] = 2
    r[(a > CFG["adx_trend"]) & (e9 < e21) & (e21 < e50)] = -1
    r[(a > CFG["adx_trend"]) & (e9 > e21) & (e21 > e50)] = 1
    r[was_sq & expand & (volr > 1.5) & (c < lo)] = -3
    r[was_sq & expand & (volr > 1.5) & (c > up)] = 3
    r[atrp > CFG["chaos_pct"]] = 9
    return r, dict(adx=float(a.iloc[-1]), bbw_pct=float(bbp.iloc[-1]),
                   atr_pct=float(atrp.iloc[-1]))


def confirmed_regime(raw, confirm_bars):
    """Hysteresis: regime baru dipakai setelah bertahan N candle (chaos/breakout langsung)."""
    reg, cnt, prev = 0, 0, None
    for v in raw.to_numpy():
        cnt = cnt + 1 if v == prev else 1
        prev = v
        need = 1 if v in (9, 3, -3) else confirm_bars
        if cnt >= need:
            reg = int(v)
    return reg


# ----------------------------------------------------------------- layer 2: signals
def evaluate(d15, reg, dir4h):
    """Cek sinyal pada candle 15m terakhir yang sudah close. Return (signal|None, alasan)."""
    c = d15["close"]
    e9, e21 = ema(c, 9), ema(c, 21)
    tp = (d15["high"] + d15["low"] + c) / 3
    day = d15["ts"].dt.date
    vw = (tp * d15["vol"]).groupby(day).cumsum() / d15["vol"].groupby(day).cumsum()
    atr1 = atr(d15)
    basis = c.rolling(20).mean()
    dev = 2 * c.rolling(20).std(ddof=0)
    bb_up, bb_lo = basis + dev, basis - dev
    rsi7 = rsi(c, 7)
    volr = d15["vol"] / d15["vol"].rolling(20).mean()
    hi15 = d15["high"].shift(1).rolling(15).max()
    lo15 = d15["low"].shift(1).rolling(15).min()

    x = d15.iloc[-1]
    close, open_, high, low = x["close"], x["open"], x["high"], x["low"]
    v = lambda s: float(s.iloc[-1])  # noqa: E731
    vol_ok = v(volr) > 1.0

    # session (jam UTC dari open candle)
    s0, s1 = [int(t) for t in SESSION_UTC.split("-")]
    if not (s0 <= x["ts"].hour < s1):
        return None, "di luar sesi"
    if reg in (0, 9):
        return None, f"regime {REGIME_NAME[reg]}"

    sig = None  # (module, dir)
    if reg == 3 and close > v(hi15) and v(volr) > 1.5:
        sig = ("C", 1)
    elif reg == -3 and close < v(lo15) and v(volr) > 1.5:
        sig = ("C", -1)
    elif reg == 1 and dir4h == 1 and close > v(vw) and low <= v(e21) \
            and close > v(e9) and close > open_ and vol_ok:
        sig = ("A", 1)
    elif reg == -1 and dir4h == -1 and close < v(vw) and high >= v(e21) \
            and close < v(e9) and close < open_ and vol_ok:
        sig = ("A", -1)
    elif reg == 2 and low <= v(bb_lo) and v(rsi7) < 20 and close > open_:
        sig = ("B", 1)
    elif reg == 2 and high >= v(bb_up) and v(rsi7) > 80 and close < open_:
        sig = ("B", -1)
    if sig is None:
        return None, "tidak ada setup"

    mod, d = sig
    sl_dist = v(atr1) * CFG["sl_mult"]
    if sl_dist < CFG["fee_mult"] * (CFG["fee_rt"] / 100) * close:
        return None, "SL terlalu sempit vs fee"
    return dict(
        module=mod, dir=d, entry=float(close), sl_dist=sl_dist,
        sl=float(close - d * sl_dist), tp=float(close + d * sl_dist * CFG["rr"]),
        be_trigger=float(close + d * sl_dist), candle=x["ts"],
    ), "ok"


# ----------------------------------------------------------------- telegram
def tg(text):
    if not TOKEN or not CHAT_ID:
        print("[DRY-RUN] Token/chat id kosong. Pesan:\n" + text)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"},
            timeout=15,
        )
    except requests.RequestException as e:
        # jangan cetak pesan asli: URL-nya memuat token (log repo publik terbuka)
        raise RuntimeError(f"Telegram request gagal: {type(e).__name__}") from None
    if not r.ok:
        raise RuntimeError(f"Telegram gagal: HTTP {r.status_code}")


def fmt(p):
    return f"{p:,.1f}"


def signal_message(s, reg, dir4h, price_now):
    side = "LONG" if s["dir"] == 1 else "SHORT"
    icon = "🟢" if s["dir"] == 1 else "🔴"
    sl_pct = s["sl_dist"] / s["entry"] * 100
    tp_pct = sl_pct * CFG["rr"]
    risk_usd = CFG["equity"] * CFG["risk_pct"] / 100
    qty = min(risk_usd / s["sl_dist"], CFG["equity"] * CFG["max_lev"] / s["entry"])
    notional = qty * s["entry"]
    return (
        f"{icon} <b>{side} {INST_ID}</b>\n"
        f"Modul {s['module']}: {MODULE_NAME[s['module']]}\n"
        f"Regime 1H: <b>{REGIME_NAME[reg]}</b> | Arah 4H: {'naik' if dir4h == 1 else 'turun'}\n\n"
        f"Entry : <code>{fmt(s['entry'])}</code> (close 15m)\n"
        f"SL    : <code>{fmt(s['sl'])}</code> (-{sl_pct:.2f}%)\n"
        f"TP    : <code>{fmt(s['tp'])}</code> (+{tp_pct:.2f}%) RR 1:{CFG['rr']:g}\n\n"
        f"Geser SL ke BE saat harga menyentuh <code>{fmt(s['be_trigger'])}</code> (+1R)\n"
        f"Time stop: {CFG['time_stop']} candle 15m ({CFG['time_stop'] * 15 // 60} jam)\n\n"
        f"Contoh size (equity {CFG['equity']:g} USDT, risiko {CFG['risk_pct']:g}%): "
        f"{qty:.4f} BTC, notional {notional:,.0f} USDT (~{notional / CFG['equity']:.1f}x)\n"
        f"Harga sekarang: {fmt(price_now)} | Candle: {s['candle']:%Y-%m-%d %H:%M} UTC\n"
        f"<i>Sinyal bukan nasihat keuangan. Pasang SL, jangan entry jika harga sudah jauh dari level.</i>"
    )


# ----------------------------------------------------------------- main
def load_context():
    d15_all = fetch_candles(ENTRY_TF)
    time.sleep(0.3)
    d1h_all = fetch_candles(REG_TF)
    time.sleep(0.3)
    d4h_all = fetch_candles(DIR_TF)
    price_now = float(d15_all["close"].iloc[-1])  # termasuk candle berjalan
    d15 = d15_all[d15_all["confirm"] == 1].reset_index(drop=True)
    d1h = d1h_all[d1h_all["confirm"] == 1].reset_index(drop=True)
    d4h = d4h_all[d4h_all["confirm"] == 1].reset_index(drop=True)
    raw, stats = regime_series(d1h)
    reg = confirmed_regime(raw, CFG["confirm_bars"])
    dir4h = 1 if d4h["close"].iloc[-1] > ema(d4h["close"], 21).iloc[-1] else -1
    return d15, reg, int(raw.iloc[-1]), stats, dir4h, price_now


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "test":
        tg("✅ Bot terhubung. Regime-First Scalper siap memantau "
           f"{INST_ID} (entry {ENTRY_TF}, regime {REG_TF}, arah {DIR_TF}).")
        return

    d15, reg, raw_now, stats, dir4h, price_now = load_context()

    if mode == "status":
        tg(
            f"📊 <b>Status {INST_ID}</b>\n"
            f"Harga: {fmt(price_now)}\n"
            f"Regime 1H (terkonfirmasi): <b>{REGIME_NAME[reg]}</b>\n"
            f"Regime candle terakhir: {REGIME_NAME[raw_now]}\n"
            f"ADX {stats['adx']:.1f} | BB-width pctile {stats['bbw_pct']:.0f} | "
            f"ATR pctile {stats['atr_pct']:.0f}\n"
            f"Arah 4H: {'naik' if dir4h == 1 else 'turun'}"
        )
        return

    # --- scan
    last = d15.iloc[-1]
    close_time = last["ts"] + timedelta(minutes=15)
    age_min = (datetime.now(timezone.utc) - close_time).total_seconds() / 60
    print(f"[{datetime.now(timezone.utc):%F %T}Z] regime={REGIME_NAME[reg]} "
          f"dir4h={dir4h} candle={last['ts']:%F %R} umur={age_min:.1f}m")

    if age_min > CFG["fresh_min"]:
        print("Candle terakhir sudah terlalu lama (run telat) -> skip")
        return

    sig, why = evaluate(d15, reg, dir4h)
    if sig is None:
        print(f"Tidak ada sinyal: {why}")
        return
    if abs(price_now - sig["entry"]) > CFG["stale_frac"] * sig["sl_dist"]:
        print("Sinyal basi: harga sudah bergeser jauh dari entry -> skip")
        return
    tg(signal_message(sig, reg, dir4h, price_now))
    print("Sinyal terkirim.")


if __name__ == "__main__":
    main()
