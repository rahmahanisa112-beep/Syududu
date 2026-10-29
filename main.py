"""OKX BTC/USDT live signal bot. Public data only; never places orders."""
import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT = os.getenv('TELEGRAM_CHAT_ID', '')
STATE = Path('last_signal.json')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                    handlers=[logging.StreamHandler(), logging.FileHandler('bot.log')])


def telegram(message):
    if not TOKEN or not CHAT:
        raise RuntimeError('Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID')
    url = f'https://api.telegram.org/bot{TOKEN}/sendMessage'
    for attempt in range(3):
        try:
            response = requests.post(url, json={'chat_id': CHAT, 'text': message}, timeout=20)
            if response.ok and response.json().get('ok'):
                return True
            logging.warning('Telegram HTTP %s: %s', response.status_code, response.text[:200])
            if response.status_code in (400, 401, 403, 404):
                return False
            wait = response.headers.get('Retry-After', '')
            delay = min(60, int(wait)) if wait.isdigit() else 2 ** attempt
        except (requests.RequestException, ValueError) as exc:
            logging.warning('Telegram retry %d: %s', attempt + 1, exc)
            delay = 2 ** attempt
        if attempt < 2:
            time.sleep(delay)
    return False


def candles(exchange, timeframe, count):
    """Fetch enough completed candles for EMA200 even with OKX per-call limits."""
    ms = exchange.parse_timeframe(timeframe) * 1000
    now = exchange.milliseconds()
    since = now - (count + 12) * ms
    rows_by_time = {}
    for _ in range(8):
        batch = None
        for attempt in range(3):
            try:
                batch = exchange.fetch_ohlcv('BTC/USDT', timeframe, since=since, limit=100)
                break
            except ccxt.BaseError as exc:
                logging.warning('OKX %s retry %s: %s', timeframe, attempt + 1, exc)
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        if not batch:
            break
        for row in batch:
            if row[0] + ms <= now:
                rows_by_time[row[0]] = row[:6]
        latest = max(row[0] for row in batch)
        if latest < since:
            break
        since = latest + ms
        if len(rows_by_time) >= count or since >= now:
            break
    rows = [rows_by_time[t] for t in sorted(rows_by_time)][-count:]
    if len(rows) < count - 5:
        raise RuntimeError(f'Not enough {timeframe} candles: {len(rows)}')
    df = pd.DataFrame(rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    logging.info('%s: %d closed candles, latest %s', timeframe, len(df), df.timestamp.iloc[-1])
    return df


def ema(s, period):
    return s.ewm(span=period, adjust=False, min_periods=period).mean()


def rma(s, period):
    return s.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def true_range(df):
    previous = df.close.shift(1)
    return pd.concat([(df.high - df.low), (df.high - previous).abs(),
                      (df.low - previous).abs()], axis=1).max(axis=1)


def atr(df, period=14):
    return rma(true_range(df), period)


def adx(df, period=14):
    up = df.high.diff()
    down = -df.low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr_avg = rma(true_range(df), period).replace(0, float('nan'))
    plus_di = 100 * rma(plus_dm, period) / tr_avg
    minus_di = 100 * rma(minus_dm, period) / tr_avg
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float('nan'))
    return rma(dx, period)


def rsi(close, period=14):
    delta = close.diff()
    gains = rma(delta.clip(lower=0), period)
    losses = rma((-delta).clip(lower=0), period)
    rs = gains / losses.replace(0, float('nan'))
    result = 100 - 100 / (1 + rs)
    result = result.mask((losses == 0) & (gains > 0), 100)
    return result.mask((losses == 0) & (gains == 0), 50)


def signal_for(df4, df1):
    fast = ema(df4.close, 50).iloc[-1]
    slow = ema(df4.close, 200).iloc[-1]
    trend = adx(df4).iloc[-1]
    if pd.isna(fast) or pd.isna(slow) or pd.isna(trend):
        logging.info('NO TRADE: incomplete 4H indicators')
        return None
    direction = 'LONG' if fast > slow else 'SHORT' if fast < slow else None
    logging.info('4H EMA50=%.2f EMA200=%.2f ADX=%.1f bias=%s', fast, slow, trend, direction)
    if trend <= 20 or direction is None:
        return None
    candle = df1.iloc[-1]
    e20, e50 = ema(df1.close, 20).iloc[-1], ema(df1.close, 50).iloc[-1]
    momentum = rsi(df1.close).iloc[-1]
    avg_vol = df1.volume.iloc[-21:-1].mean()
    vol_ratio = candle.volume / avg_vol if avg_vol > 0 else 0
    touch20 = candle.low <= e20 * 1.003 and candle.high >= e20 * 0.997
    touch50 = candle.low <= e50 <= candle.high
    directional = candle.close > candle.open if direction == 'LONG' else candle.close < candle.open
    ok = (touch20 or touch50) and directional and 35 <= momentum <= 65 and vol_ratio > 1
    logging.info('1H pullback=%s close_direction=%s RSI=%.1f volume=%.2fx valid=%s',
                 touch20 or touch50, directional, momentum, vol_ratio, ok)
    if not ok:
        return None
    risk = atr(df1).iloc[-1] * 1.5
    entry = float(candle.close)
    if pd.isna(risk) or risk <= 0 or entry <= risk * 2:
        return None
    side = 1 if direction == 'LONG' else -1
    return {'direction': direction, 'entry': entry, 'sl': entry - side * risk,
            'tp': entry + side * 2 * risk, 'adx': float(trend),
            'rsi': float(momentum), 'volume': float(vol_ratio),
            'trigger': 'EMA20' if touch20 else 'EMA50',
            'time': candle.timestamp.isoformat()}


def read_state():
    try:
        data = json.loads(STATE.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', action='store_true', help='Send test Telegram message')
    args = parser.parse_args()
    if args.test:
        return 0 if telegram('✅ OKX BTC Signal Bot: Telegram aktif.') else 1
    exchange = ccxt.okx({'enableRateLimit': True, 'timeout': 20000})
    try:
        df4 = candles(exchange, '4h', 260)
        df1 = candles(exchange, '1h', 100)
        s = signal_for(df4, df1)
        if not s:
            logging.info('No signal this hour')
            return 0
        now = time.time()
        day = datetime.now(timezone.utc).date().isoformat()
        history = [x for x in read_state().get('sent', []) if x.get('day') == day]
        identity = s['time'] + ':' + s['direction']
        if any(x['id'] == identity for x in history) or len(history) >= 3:
            logging.info('Duplicate or daily limit reached')
            return 0
        if history and now - max(x['at'] for x in history) < 4 * 3600:
            logging.info('Cooldown active')
            return 0
        pct = abs(s['entry'] - s['sl']) / s['entry'] * 100
        message = (f'🚨 BTC DAILY SIGNAL 🚨\n\nDirection: {s["direction"]} '
                   f'{"🟢" if s["direction"] == "LONG" else "🔴"}\n'
                   f'Entry: ${s["entry"]:,.2f}\nStop Loss: ${s["sl"]:,.2f} ({pct:.2f}% risk)\n'
                   f'Take Profit: ${s["tp"]:,.2f} (1:2 RR)\n\n'
                   f'Regime: 4H EMA50/200 + ADX {s["adx"]:.1f}\n'
                   f'Trigger: 1H {s["trigger"]} pullback + directional close\n'
                   f'RSI: {s["rsi"]:.1f} | Volume: {s["volume"]:.2f}x avg\n'
                   f'Time (candle open): {s["time"]}\nValid for: next 4 hours (unless invalidated)')
        if not telegram(message):
            return 1
        history.append({'id': identity, 'day': day, 'at': now})
        temp = STATE.with_suffix('.tmp')
        temp.write_text(json.dumps({'sent': history}, indent=2))
        temp.replace(STATE)
        logging.info('Signal sent and state saved')
        return 0
    finally:
        exchange.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        logging.exception('Bot failed')
        raise SystemExit(1)
