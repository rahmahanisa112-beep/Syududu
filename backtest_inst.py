#!/usr/bin/env python3
"""
Backtest engine V6.1 (BTC-USDT-SWAP). Memakai fungsi build_signal dan simulate_trade
dari signal_bot.py yang sama, jadi perilakunya identik dengan bot live.

Data: OKX history-candles (15m, 1H, 4H) diambil otomatis.
Eksekusi: sinyal pada close candle 15m, exit di candle 15m (SL didahulukan jika satu candle
menyentuh SL dan TP). Satu posisi aktif sekaligus. Biaya round-trip dipotong dari R.
"""
import os, sys, json, time, math, argparse
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signal_bot as bot

M15, H1, H4 = 900_000, 3_600_000, 14_400_000

def fetch_history(bar, start_ms, end_ms):
    """Paginasi OKX /market/history-candles dari yang terbaru ke belakang sampai start_ms."""
    out = {}
    after = str(end_ms)
    for _ in range(100000):
        d = bot.api("/api/v5/market/history-candles",
                    {"instId": bot.SYMBOL, "bar": bar, "limit": "100", "after": after})
        rows = (d or {}).get("data", [])
        if not rows:
            break
        oldest = None
        for x in rows:
            ts = int(x[0])
            oldest = ts if oldest is None else min(oldest, ts)
            if x[8] == "1":
                out[ts] = [ts, float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])]
        if oldest is None or oldest <= start_ms or str(oldest) == after:
            break
        after = str(oldest)
    return [out[k] for k in sorted(out) if start_ms <= k <= end_ms]

def run(c15, c1h, c4h, tick, log_every=0):
    trades, gates = [], Counter()
    open_until = 0
    WARM = 260
    h1_i = 0; h4_i = 0
    for i in range(WARM, len(c15)):
        sig_ms = c15[i][0]
        if sig_ms < open_until:
            continue
        window = c15[i - WARM + 1: i + 1]
        close_ms = sig_ms + M15
        # ambil candle 1H dan 4H yang sudah tutup sebelum candle sinyal
        while h1_i + 1 < len(c1h) and c1h[h1_i + 1][0] + H1 <= close_ms:
            h1_i += 1
        while h4_i + 1 < len(c4h) and c4h[h4_i + 1][0] + H4 <= close_ms:
            h4_i += 1
        h1w = c1h[max(0, h1_i - 300): h1_i + 1]
        h4w = c4h[max(0, h4_i - 150): h4_i + 1]
        if len(h1w) < 210 or len(h4w) < 12:
            gates["Data 1H/4H"] += 1
            continue
        try:
            sigs, gl, gs, _ = bot.build_signal(window, h1w, h4w, tick)
        except Exception as e:
            gates["Error"] += 1
            continue
        if not sigs:
            gates[f"LONG {gl[0]}" if gl else "LONG ?"] += 1
            gates[f"SHORT {gs[0]}" if gs else "SHORT ?"] += 1
            continue
        direction, (s, _g) = next(iter(sigs.items()))
        t = {"direction": direction, "entry": s["entry"], "sl": s["sl"], "D": s["D"],
             "tp1": s["tp1"], "tp2": s["tp2"], "atr15": s["atr15"], "cost_R": s["cost_R"],
             "signal_candle_time": sig_ms}
        future = c15[i + 1:]
        closed, r, ex_ms, reason = bot.simulate_trade(t, future)
        if not closed:
            break                                 # data habis saat posisi masih terbuka
        net = r - s["cost_R"]
        trades.append({"time": bot.iso(sig_ms), "dir": direction, "regime": s["regime"],
                       "level": s["level"], "entry": s["entry"], "sl": s["sl"],
                       "gross_r": round(r, 4), "cost_R": round(s["cost_R"], 4),
                       "net_r": round(net, 4), "exit": reason, "exit_time": bot.iso(ex_ms)})
        open_until = ex_ms
    return trades, gates

def summarize(trades, gates):
    n = len(trades)
    if n == 0:
        return f"0 trade.\nGate yang menggugurkan (LONG dan SHORT):\n" + "\n".join(f"  {k}: {v}" for k, v in gates.most_common(30))
    wins = [t for t in trades if t["net_r"] > 0]
    gp = sum(t["net_r"] for t in trades if t["net_r"] > 0)
    gl = -sum(t["net_r"] for t in trades if t["net_r"] <= 0)
    eq = pk = dd = 0.0
    for t in trades:
        eq += t["net_r"]; pk = max(pk, eq); dd = min(dd, eq - pk)
    avg = sum(t["net_r"] for t in trades) / n
    sd = (sum((t["net_r"] - avg) ** 2 for t in trades) / max(n - 1, 1)) ** 0.5
    tstat = avg / (sd / math.sqrt(n)) if sd > 0 else 0.0
    wr = len(wins) / n
    lo = wr - 1.96 * math.sqrt(wr * (1 - wr) / n); hi = wr + 1.96 * math.sqrt(wr * (1 - wr) / n)
    exits = Counter(t["exit"] for t in trades)
    lines = [
        f"Trades      : {n}",
        f"Win rate    : {wr*100:.1f}% (95% CI {max(lo,0)*100:.0f}-{min(hi,1)*100:.0f}%)",
        f"Total R neto: {sum(t['net_r'] for t in trades):+.2f}R | rata-rata {avg:+.3f}R/trade (t={tstat:.2f})",
        f"Profit factor: {'inf' if gl == 0 else f'{gp/gl:.2f}'} | Max DD {dd:.2f}R",
        f"Exit        : " + ", ".join(f"{k} {v}" for k, v in exits.most_common()),
        f"Long/Short  : {sum(1 for t in trades if t['dir']=='LONG')} / {sum(1 for t in trades if t['dir']=='SHORT')}",
        f"Target 60%  : {'TERCAPAI' if wr >= 0.60 else 'BELUM TERCAPAI'} (WR {wr*100:.1f}%)",
        "",
        "Gate yang paling sering menggugurkan:",
    ] + [f"  {k}: {v}" for k, v in gates.most_common(30)]
    return "\n".join(lines)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--end", default="")
    ap.add_argument("--out", default="bt_out")
    ap.add_argument("--telegram", action="store_true")
    a = ap.parse_args()
    end_ms = int(datetime.strptime(a.end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000) if a.end \
        else int(time.time() * 1000)
    start_ms = end_ms - a.days * 86_400_000 - 400 * H4        # buffer untuk warm-up 4H & 1H
    print("Ambil data 15m...", flush=True); c15 = fetch_history("15m", start_ms, end_ms)
    print("Ambil data 1H...", flush=True);  c1h = fetch_history("1H", start_ms, end_ms)
    print("Ambil data 4H...", flush=True);  c4h = fetch_history("4H", start_ms, end_ms)
    print(f"Candle: 15m {len(c15)} | 1H {len(c1h)} | 4H {len(c4h)}", flush=True)
    if len(c15) < 1000:
        print("Data 15m terlalu sedikit. Berhenti.", flush=True); sys.exit(1)
    tick = bot.tick_size()
    t0 = time.time()
    trades, gates = run(c15, c1h, c4h, tick)
    text = summarize(trades, gates)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(os.path.join(a.out, "trades.json"), "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False, indent=1)
    print("\n" + text)
    print(f"\nSelesai dalam {time.time()-t0:.0f} detik. Hasil di {a.out}/")
    if a.telegram and bot.TELEGRAM:
        bot.tg("<b>📈 BACKTEST V6.1 · BTC · " + str(a.days) + " hari</b>\n<pre>" + text + "</pre>")

if __name__ == "__main__":
    main()
