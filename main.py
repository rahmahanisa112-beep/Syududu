
import os
import sys
import json
import time
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

import ccxt
import pandas as pd


# ==========================================
# CONFIG
# ==========================================

SYMBOL = "BTC/USDT"

EMA_FAST_4H = 50
EMA_SLOW_4H = 200
ADX_PERIOD = 14
ADX_THRESHOLD = 20

EMA_PULLBACK_1H = 20
EMA_TREND_1H = 50
PULLBACK_DISTANCE = 0.003

RSI_PERIOD = 14
RSI_MIN = 35
RSI_MAX = 65

VOLUME_PERIOD = 20
VOLUME_MULTIPLIER = 1.0

ATR_PERIOD = 14
ATR_MULTIPLIER = 1.5
RR_RATIO = 2.0

COOLDOWN_HOURS = 4
MAX_SIGNALS_PER_DAY = 3

STATE_FILE = Path("signal_history.json")

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN", ""
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID", ""
).strip()


# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            "bot.log",
            encoding="utf-8"
        )
    ]
)

logger = logging.getLogger("BTC_OKX_BOT")


# ==========================================
# INDICATORS
# ==========================================

def ema(series, period):
    return series.ewm(
        span=period,
        adjust=False,
        min_periods=period
    ).mean()


def rsi(series, period=14):
    delta = series.diff()

    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    avg_gain = gains.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    avg_loss = losses.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    result = 100 - (100 / (1 + rs))

    result = result.mask(
        (avg_loss == 0) & (avg_gain > 0),
        100
    )

    result = result.mask(
        (avg_gain == 0) & (avg_loss > 0),
        0
    )

    return result.mask(
        (avg_gain == 0) & (avg_loss == 0),
        50
    )


def true_range(df):
    previous_close = df["close"].shift(1)

    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)


def atr(df, period=14):
    tr = true_range(df)

    return tr.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()


def adx(df, period=14):
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = up_move.where(
        (up_move > down_move) & (up_move > 0),
        0.0
    )

    minus_dm = down_move.where(
        (down_move > up_move) & (down_move > 0),
        0.0
    )

    smoothed_atr = atr(df, period)

    plus_di = (
        100
        * plus_dm.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period
        ).mean()
        / smoothed_atr.replace(0, float("nan"))
    )

    minus_di = (
        100
        * minus_dm.ewm(
            alpha=1 / period,
            adjust=False,
            min_periods=period
        ).mean()
        / smoothed_atr.replace(0, float("nan"))
    )

    di_sum = plus_di + minus_di

    dx = (
        100
        * (plus_di - minus_di).abs()
        / di_sum.replace(0, float("nan"))
    )

    return dx.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()


# ==========================================
# TELEGRAM
# ==========================================

def telegram(message):
    import requests

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
        )
        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_BOT_TOKEN
        + "/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    for attempt in range(3):
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=15
            )

            if response.status_code == 200:
                if response.json().get("ok"):
                    logger.info("Telegram message sent")
                    return True

            logger.warning(
                "Telegram HTTP %s: %s",
                response.status_code,
                response.text[:200]
            )

            if response.status_code in (400, 401, 403, 404):
                return False

            delay = 2 ** attempt

            if response.status_code == 429:
                try:
                    delay = min(
                        60,
                        int(
                            response.json()
                            .get("parameters", {})
                            .get("retry_after", delay)
                        )
                    )
                except (ValueError, TypeError):
                    pass

        except requests.RequestException as exc:
            logger.warning(
                "Telegram attempt %d failed: %s",
                attempt + 1,
                exc
            )
            delay = 2 ** attempt

        if attempt < 2:
            time.sleep(delay)

    return False


# ==========================================
# OKX MARKET DATA
# ==========================================

def fetch_candles(exchange, timeframe, count):
    timeframe_ms = (
        exchange.parse_timeframe(timeframe) * 1000
    )

    now_ms = exchange.milliseconds()

    since = now_ms - (
        (count + 30) * timeframe_ms
    )

    all_rows = []

    for page in range(10):
        if len(all_rows) >= count + 10:
            break

        rows = None

        for attempt in range(3):
            try:
                rows = exchange.fetch_ohlcv(
                    SYMBOL,
                    timeframe=timeframe,
                    since=since,
                    limit=100
                )
                break

            except (
                ccxt.NetworkError,
                ccxt.ExchangeError
            ) as exc:
                logger.warning(
                    "OKX %s attempt %d: %s",
                    timeframe,
                    attempt + 1,
                    exc
                )

                if attempt == 2:
                    raise

                time.sleep(2 ** attempt)

        if not rows:
            break

        all_rows.extend(rows)

        next_since = rows[-1][0] + timeframe_ms

        if next_since <= since:
            break

        since = next_since

        if since >= now_ms:
            break

    if not all_rows:
        raise RuntimeError(
            f"No OKX data for {timeframe}"
        )

    df = pd.DataFrame(
        all_rows,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]
    )

    df = (
        df.drop_duplicates("timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    # Only completed candles.
    df = df[
        df["timestamp"] + timeframe_ms <= now_ms
    ].copy()

    df = df.tail(count).reset_index(drop=True)

    for column in [
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df = df.dropna().reset_index(drop=True)

    if len(df) < count:
        raise RuntimeError(
            f"Insufficient {timeframe} candles: "
            f"{len(df)}/{count}"
        )

    logger.info(
        "OKX %s: %d closed candles",
        timeframe,
        len(df)
    )

    return df


# ==========================================
# 4H REGIME
# ==========================================

def check_regime(df):
    fast = ema(
        df["close"],
        EMA_FAST_4H
    ).iloc[-1]

    slow = ema(
        df["close"],
        EMA_SLOW_4H
    ).iloc[-1]

    adx_value = adx(
        df,
        ADX_PERIOD
    ).iloc[-1]

    if pd.isna(fast) or pd.isna(slow) or pd.isna(adx_value):
        logger.warning("4H indicators unavailable")
        return "NO_TRADE", None

    logger.info(
        "4H EMA50=%.2f EMA200=%.2f ADX=%.2f",
        fast,
        slow,
        adx_value
    )

    if adx_value <= ADX_THRESHOLD:
        return "NO_TRADE", float(adx_value)

    if fast > slow:
        return "BULLISH", float(adx_value)

    if fast < slow:
        return "BEARISH", float(adx_value)

    return "NO_TRADE", float(adx_value)


# ==========================================
# 1H ENTRY
# ==========================================

def check_entry(df, regime, adx_value):
    if regime == "NO_TRADE":
        return None

    latest = df.iloc[-1]

    close = float(latest["close"])
    opening = float(latest["open"])
    high = float(latest["high"])
    low = float(latest["low"])

    ema20 = ema(
        df["close"],
        EMA_PULLBACK_1H
    ).iloc[-1]

    ema50 = ema(
        df["close"],
        EMA_TREND_1H
    ).iloc[-1]

    rsi_value = rsi(
        df["close"],
        RSI_PERIOD
    ).iloc[-1]

    atr_value = atr(
        df,
        ATR_PERIOD
    ).iloc[-1]

    avg_volume = df["volume"].iloc[
        -(VOLUME_PERIOD + 1):-1
    ].mean()

    if any(
        pd.isna(value)
        for value in [
            ema20,
            ema50,
            rsi_value,
            atr_value,
            avg_volume
        ]
    ):
        logger.warning("1H indicators unavailable")
        return None

    if avg_volume <= 0 or atr_value <= 0:
        return None

    volume_ratio = (
        float(latest["volume"]) / float(avg_volume)
    )

    # EMA20 within 0.3%, or actual EMA50 touch.
    near_ema20 = (
        low <= ema20 * (1 + PULLBACK_DISTANCE)
        and high >= ema20 * (1 - PULLBACK_DISTANCE)
    )

    touch_ema50 = low <= ema50 <= high

    pullback_ok = near_ema20 or touch_ema50

    if regime == "BULLISH":
        direction = "LONG"
        candle_ok = close > opening
    else:
        direction = "SHORT"
        candle_ok = close < opening

    rsi_ok = RSI_MIN <= rsi_value <= RSI_MAX

    volume_ok = volume_ratio > VOLUME_MULTIPLIER

    logger.info(
        "1H %s | pullback=%s candle=%s "
        "RSI=%.2f volume=%.2fx",
        direction,
        pullback_ok,
        candle_ok,
        rsi_value,
        volume_ratio
    )

    if not (
        pullback_ok
        and candle_ok
        and rsi_ok
        and volume_ok
    ):
        return None

    risk_distance = float(atr_value) * ATR_MULTIPLIER

    if direction == "LONG":
        sl = close - risk_distance
        tp = close + risk_distance * RR_RATIO
    else:
        sl = close + risk_distance
        tp = close - risk_distance * RR_RATIO

    if min(sl, tp) <= 0:
        return None

    candle_time = datetime.fromtimestamp(
        int(latest["timestamp"]) / 1000,
        tz=timezone.utc
    )

    return {
        "direction": direction,
        "entry": close,
        "sl": sl,
        "tp": tp,
        "sl_pct": risk_distance / close * 100,
        "regime": regime,
        "adx": adx_value,
        "rsi": float(rsi_value),
        "volume_ratio": volume_ratio,
        "trigger": (
            "EMA20 pullback"
            if near_ema20
            else "EMA50 touch"
        ),
        "candle_time": candle_time.isoformat()
    }


# ==========================================
# SIGNAL HISTORY
# ==========================================

def load_history():
    if not STATE_FILE.exists():
        return []

    try:
        data = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )

        if isinstance(data, list):
            return data

        logger.warning("Invalid history format")
        return []

    except (OSError, ValueError) as exc:
        logger.warning(
            "History read failed: %s",
            exc
        )
        return []


def save_history(history):
    temp_file = STATE_FILE.with_suffix(".tmp")

    temp_file.write_text(
        json.dumps(history[-200:], indent=2),
        encoding="utf-8"
    )

    temp_file.replace(STATE_FILE)


def can_send(signal, history):
    now = datetime.now(timezone.utc)

    today = now.date().isoformat()

    today_count = sum(
        1
        for item in history
        if item.get("sent_at", "").startswith(today)
    )

    if today_count >= MAX_SIGNALS_PER_DAY:
        logger.info("Daily signal limit reached")
        return False

    signal_id = (
        signal["candle_time"]
        + ":"
        + signal["direction"]
    )

    for item in history:
        if item.get("signal_id") == signal_id:
            logger.info("Duplicate signal blocked")
            return False

        try:
            sent_at = datetime.fromisoformat(
                item["sent_at"]
            )

            if now - sent_at < timedelta(
                hours=COOLDOWN_HOURS
            ):
                logger.info("Cooldown active")
                return False

        except (KeyError, ValueError, TypeError):
            continue

    return True


# ==========================================
# SIGNAL FORMAT
# ==========================================

def format_signal(signal):
    direction = signal["direction"]

    emoji = "🟢" if direction == "LONG" else "🔴"

    regime_text = (
        "Bullish"
        if signal["regime"] == "BULLISH"
        else "Bearish"
    )

    return (
        "🚨 BTC DAILY SIGNAL 🚨\n\n"
        f"Direction: {direction} {emoji}\n"
        f"Entry: ${signal['entry']:,.2f}\n"
        f"Stop Loss: ${signal['sl']:,.2f} "
        f"({signal['sl_pct']:.2f}% risk)\n"
        f"Take Profit: ${signal['tp']:,.2f} "
        "(1:2 RR)\n\n"
        f"Regime: {regime_text} "
        f"(4H EMA50/200, ADX {signal['adx']:.1f})\n"
        f"Trigger: {signal['trigger']} "
        "+ directional close\n"
        f"RSI: {signal['rsi']:.1f} | "
        f"Volume: {signal['volume_ratio']:.2f}x avg\n\n"
        f"Signal candle: {signal['candle_time']}\n"
        "Valid for: Next 4 hours\n\n"
        "Signal only. No automatic execution."
    )


# ==========================================
# MAIN
# ==========================================

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--test",
        action="store_true"
    )

    args = parser.parse_args()

    if args.test:
        success = telegram(
            "✅ OKX BTC Signal Bot: Telegram aktif!"
        )
        return 0 if success else 1

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error(
            "Telegram credentials missing"
        )
        return 1

    try:
        logger.info("Starting OKX BTC signal scan")

        exchange = ccxt.okx({
            "enableRateLimit": True,
            "timeout": 20000
        })

        exchange.load_markets()

        if SYMBOL not in exchange.markets:
            raise RuntimeError(
                f"Symbol unavailable: {SYMBOL}"
            )

        df_4h = fetch_candles(
            exchange,
            "4h",
            260
        )

        df_1h = fetch_candles(
            exchange,
            "1h",
            100
        )

        regime, adx_value = check_regime(df_4h)

        logger.info("Current regime: %s", regime)

        if regime == "NO_TRADE":
            logger.info("No trade: 4H regime filter")
            return 0

        signal = check_entry(
            df_1h,
            regime,
            adx_value
        )

        if signal is None:
            logger.info("No valid 1H entry")
            return 0

        history = load_history()

        if not can_send(signal, history):
            return 0

        message = format_signal(signal)

        if not telegram(message):
            logger.error("Telegram delivery failed")
            return 1

        history.append({
            "signal_id": (
                signal["candle_time"]
                + ":"
                + signal["direction"]
            ),
            "sent_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "direction": signal["direction"],
            "entry": signal["entry"]
        })

        save_history(history)

        logger.info(
            "Signal sent: %s %.2f",
            signal["direction"],
            signal["entry"]
        )

        return 0

    except Exception:
        logger.exception("Bot failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
