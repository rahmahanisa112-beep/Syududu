#!/usr/bin/env python3
"""ALPHAQUANT backtest runner (compatible with legacy signal_bot V6.1 API).

Historical data: OKX public history-candles, no API credentials.
Requires signal_bot.py exposing build_signal, simulate_trade, tick_size and iso.
Signal at 15m candle close; simulation uses next 15m bar onward.
"""
import argparse
import importlib
import json
import math
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from html import escape

import requests

M15, H1, H4 = 900_000, 3_600_000, 14_400_000
OKX_URL = os.getenv('OKX_BASE_URL', 'https://www.okx.com').rstrip('/')
SYMBOL = os.getenv('OKX_SYMBOL', 'BTC-USDT-SWAP')
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'ALPHAQUANT-backtest/6.2'})


def okx_api(path, params=None):
    last = None
    for attempt in range(5):
        try:
            resp = SESSION.get(OKX_URL + path, params=params, timeout=25)
            resp.raise_for_status()
            data = resp.json()
            if data.get('code') != '0':
                raise RuntimeError(f"OKX code={data.get('code')} msg={data.get('msg')}")
            return data
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last = exc
            print(f'OKX request attempt {attempt+1}/5: {exc}', file=sys.stderr, flush=True)
            if attempt < 4:
                time.sleep(min(2 ** attempt, 12))
    raise RuntimeError(f'OKX API unavailable at {path}: {last}')


def fetch_history(bar, start_ms, end_ms):
    """Page backward with OKX `after` cursor; keep only confirmed, closed bars."""
    out = {}
    after = str(end_ms)
    for page in range(100000):
        data = okx_api('/api/v5/market/history-candles',
                       {'instId': SYMBOL, 'bar': bar, 'limit': '100', 'after': after})
        rows = data.get('data', [])
        if not rows:
            break
        oldest = min(int(x[0]) for x in rows)
        for x in rows:
            ts = int(x[0])
            if x[8] == '1' and start_ms <= ts and ts + {'15m': M15, '1H': H1, '4H': H4}[bar] <= end_ms:
                out[ts] = [ts, *map(float, x[1:6])]
        if oldest <= start_ms:
            break
        if oldest >= int(after):
            raise RuntimeError(f'Pagination stalled for {bar}: after={after}, oldest={oldest}')
        after = str(oldest)
        if page % 100 == 0:
            print(f'{bar}: page {page+1}, candles {len(out)}', flush=True)
        time.sleep(0.12)
    return [out[k] for k in sorted(out)]


def load_bot():
    try:
        bot = importlib.import_module('signal_bot')
    except ImportError as exc:
        raise RuntimeError('File signal_bot.py harus berada di folder yang sama dengan backtest_inst_fixed.py') from exc
    required = ('build_signal', 'simulate_trade', 'tick_size', 'iso')
    missing = [name for name in required if not callable(getattr(bot, name, None))]
    if missing:
        raise RuntimeError('signal_bot.py tidak kompatibel dengan runner V6.1; fungsi hilang: '
                           + ', '.join(missing) + '. Kirim signal_bot.py untuk penyesuaian.')
    return bot


def run(bot, c15, c1h, c4h, tick, log_every=5000):
    trades, gates = [], Counter()
    open_until = 0
    warm = 260
    h1_i = h4_i = -1
    for i in range(warm, len(c15)):
        sig_ms = c15[i][0]
        if sig_ms < open_until:
            continue
        close_ms = sig_ms + M15
        while h1_i + 1 < len(c1h) and c1h[h1_i+1][0] + H1 <= close_ms:
            h1_i += 1
        while h4_i + 1 < len(c4h) and c4h[h4_i+1][0] + H4 <= close_ms:
            h4_i += 1
        h1w = c1h[max(0, h1_i-300):h1_i+1]
        h4w = c4h[max(0, h4_i-150):h4_i+1]
        if len(h1w) < 210 or len(h4w) < 12:
            gates['Data 1H/4H'] += 1
            continue
        try:
            sigs, gl, gs, _ = bot.build_signal(c15[i-warm+1:i+1], h1w, h4w, tick)
        except Exception as exc:
            raise RuntimeError(f'Signal error at {bot.iso(sig_ms)} (15m index {i}): {exc}') from exc
        if not sigs:
            gates['LONG '+(gl[0] if gl else '?')] += 1
            gates['SHORT '+(gs[0] if gs else '?')] += 1
            continue
        direction, (s, _) = next(iter(sigs.items()))
        t = {'direction': direction, 'entry': s['entry'], 'sl': s['sl'], 'D': s['D'],
             'tp1': s['tp1'], 'tp2': s['tp2'], 'atr15': s['atr15'],
             'cost_R': s['cost_R'], 'signal_candle_time': sig_ms}
        # Legacy simulator is not an intrabar execution model; fill assumptions remain approximate.
        try:
            closed, r, ex_ms, reason = bot.simulate_trade(t, c15[i+1:])
        except Exception as exc:
            raise RuntimeError(f'Simulator error at {bot.iso(sig_ms)}: {exc}') from exc
        if not closed:
            gates['Unclosed at end'] += 1
            break
        net = r - s['cost_R']
        trades.append({'time': bot.iso(sig_ms), 'dir': direction, 'regime': s['regime'],
                       'level': s['level'], 'entry': s['entry'], 'sl': s['sl'],
                       'gross_r': round(r, 4), 'cost_R': round(s['cost_R'], 4),
                       'net_r': round(net, 4), 'exit': reason, 'exit_time': bot.iso(ex_ms)})
        open_until = ex_ms + M15  # do not re-enter on an exit candle
        if log_every and i % log_every == 0:
            print(f'Progress {i}/{len(c15)}; trades {len(trades)}', flush=True)
    return trades, gates


def summarize(trades, gates):
    n = len(trades)
    if n == 0:
        return '0 trade.\nGate yang menggugurkan (LONG dan SHORT):\n' + '\n'.join(
            f'  {k}: {v}' for k, v in gates.most_common(30))
    wins = sum(t['net_r'] > 0 for t in trades)
    gp = sum(max(t['net_r'], 0) for t in trades)
    gl = -sum(min(t['net_r'], 0) for t in trades)
    eq = peak = dd = 0.0
    for t in trades:
        eq += t['net_r']; peak = max(peak, eq); dd = min(dd, eq-peak)
    avg = sum(t['net_r'] for t in trades) / n
    sd = (sum((t['net_r']-avg)**2 for t in trades)/max(n-1,1))**0.5
    tstat = avg/(sd/math.sqrt(n)) if sd else 0.0
    wr = wins/n
    # Wilson 95% interval, better behaved for small samples
    z = 1.96; den = 1 + z*z/n
    center = (wr + z*z/(2*n))/den
    half = z*math.sqrt(wr*(1-wr)/n + z*z/(4*n*n))/den
    exits = Counter(t['exit'] for t in trades)
    lines = [f'Trades: {n}', f'Win rate: {wr*100:.1f}% (95% Wilson CI {(center-half)*100:.1f}-{(center+half)*100:.1f}%)',
             f'Total R neto: {sum(t["net_r"] for t in trades):+.2f}R | mean {avg:+.3f}R/trade (t={tstat:.2f})',
             f'Profit factor: {"inf" if gl == 0 else f"{gp/gl:.2f}"} | Max DD {dd:.2f}R (not percent)',
             'Exit: ' + ', '.join(f'{k} {v}' for k,v in exits.most_common()),
             f'Long/Short: {sum(t["dir"]=="LONG" for t in trades)} / {sum(t["dir"]=="SHORT" for t in trades)}',
             'Gate yang paling sering menggugurkan:']
    return '\n'.join(lines + [f'  {k}: {v}' for k,v in gates.most_common(30)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=730)
    ap.add_argument('--end', default='')
    ap.add_argument('--out', default='bt_out')
    ap.add_argument('--telegram', action='store_true')
    args = ap.parse_args()
    bot = load_bot()
    end_ms = int(datetime.strptime(args.end, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()*1000) if args.end else int(time.time()*1000)
    start_ms = end_ms - args.days*86_400_000 - 400*H4
    print('Ambil data 15m...', flush=True); c15 = fetch_history('15m', start_ms, end_ms)
    print('Ambil data 1H...', flush=True); c1h = fetch_history('1H', start_ms, end_ms)
    print('Ambil data 4H...', flush=True); c4h = fetch_history('4H', start_ms, end_ms)
    print(f'Candle: 15m {len(c15)} | 1H {len(c1h)} | 4H {len(c4h)}', flush=True)
    if len(c15) < 1000 or len(c1h) < 210 or len(c4h) < 12:
        raise RuntimeError('Data historis tidak cukup untuk warmup indikator.')
    tick = bot.tick_size()
    trades, gates = run(bot, c15, c1h, c4h, tick)
    result = summarize(trades, gates)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, 'summary.txt'), 'w', encoding='utf-8') as f:
        f.write(result+'\n')
    with open(os.path.join(args.out, 'trades.json'), 'w', encoding='utf-8') as f:
        json.dump(trades, f, ensure_ascii=False, indent=2)
    print('\n'+result, flush=True)
    if args.telegram and getattr(bot, 'TELEGRAM', False) and callable(getattr(bot, 'tg', None)):
        bot.tg('<b>BACKTEST BTC · '+str(args.days)+' hari</b>\n<pre>'+escape(result[:3500])+'</pre>')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'BACKTEST FAILED: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
