
#!/usr/bin/env python3
"""ALPHAQUANT V6.2 - OKX BTC-USDT-SWAP Backtest
Compatible with signal_bot.py V6.2.
Signal only. OHLCV proxies, no real exchange orders.
"""
import argparse
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests
import signal_bot as bot

M15 = bot.TF15_MS
H1 = bot.TF1H_MS
H4 = bot.TF4H_MS

URL = os.getenv("OKX_BASE_URL", bot.URL).rstrip("/")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "ALPHAQUANT-V62-backtest"
})


def fetch_history(bar, start, end):
    """Download confirmed OKX candles with pagination."""
    out = {}
    cursor = end
    last = None

    while True:
        rows = None

        for attempt in range(5):
            try:
                response = SESSION.get(
                    URL + "/api/v5/market/history-candles",
                    params={
                        "instId": bot.SYMBOL,
                        "bar": bar,
                        "limit": "100",
                        "after": str(cursor),
                    },
                    timeout=25,
                )
                response.raise_for_status()
                payload = response.json()

                if payload.get("code") != "0":
                    raise RuntimeError(str(payload))

                rows = payload.get("data", [])
                break

            except (
                requests.RequestException,
                ValueError,
                RuntimeError,
            ) as exc:
                if attempt == 4:
                    raise RuntimeError(
                        f"OKX {bar} download failed: {exc}"
                    ) from exc
                time.sleep(min(2 ** attempt, 10))

        if not rows:
            break

        oldest = min(int(x[0]) for x in rows)

        for x in rows:
            try:
                ts = int(x[0])
                if start <= ts < end and x[8] == "1":
                    out[ts] = [
                        ts,
                        *[float(x[k]) for k in (1, 2, 3, 4, 5)]
                    ]
            except (IndexError, TypeError, ValueError):
                pass

        if (
            oldest <= start
            or oldest >= cursor
            or oldest == last
        ):
            break

        last = cursor
        cursor = oldest
        time.sleep(0.11)

    return [out[k] for k in sorted(out)]


def simulate(t, bar):
    """Conservative 15m execution: stop before targets."""
    side = 1 if t["direction"] == "LONG" else -1
    ts, op, hi, lo, cl, volume = bar

    stop = t["active_stop"]
    hit_stop = lo <= stop if side == 1 else hi >= stop

    if hit_stop:
        fill = (
            min(op, stop)
            if side == 1
            else max(op, stop)
        )
        bot.close_fill(
            t, t["remaining"], fill, ts, "STOP"
        )
        return

    hit_tp1 = (
        hi >= t["tp1"]
        if side == 1
        else lo <= t["tp1"]
    )

    if not t["tp1_done"] and hit_tp1:
        bot.close_fill(t, 0.5, t["tp1"], ts, "TP1")
        t["tp1_done"] = True
        t["active_stop"] = t["entry"]

    hit_tp2 = (
        hi >= t["tp2"]
        if side == 1
        else lo <= t["tp2"]
    )

    if t["tp1_done"] and not t["tp2_done"] and hit_tp2:
        bot.close_fill(t, 0.3, t["tp2"], ts, "TP2")
        t["tp2_done"] = True

    if t["tp2_done"] and t["remaining"] > 1e-9:
        candidate = (
            hi - 1.5 * t["atr15"]
            if side == 1
            else lo + 1.5 * t["atr15"]
        )

        t["active_stop"] = (
            max(t["active_stop"], candidate)
            if side == 1
            else min(t["active_stop"], candidate)
        )

    mfe = (
        (hi - t["entry"])
        if side == 1
        else (t["entry"] - lo)
    ) / t["D"]

    t["mfe_r"] = max(t["mfe_r"], mfe)

    timeout = (
        ts >= t["signal_ts"] + M15 + 8 * M15
        and t["mfe_r"] < 0.5
        and not t["tp1_done"]
    )

    if timeout:
        bot.close_fill(
            t, t["remaining"], cl, ts, "TIMEOUT"
        )


def backtest(c15, c1, c4, tick, start_test):
    counters = Counter()
    samples = {}
    trades = []

    active = None
    equity = bot.EQUITY
    i1 = 0
    i4 = 0
    signals = 0

    for i, bar in enumerate(c15):
        close_ms = bar[0] + M15

        while (
            i1 < len(c1)
            and c1[i1][0] + H1 <= close_ms
        ):
            i1 += 1

        while (
            i4 < len(c4)
            and c4[i4][0] + H4 <= close_ms
        ):
            i4 += 1

        h1 = c1[max(0, i1 - 500):i1]
        h4 = c4[max(0, i4 - 200):i4]

        if active is not None:
            if active.get("pending"):
                active["pending"] = False

                side = (
                    1 if active["direction"] == "LONG"
                    else -1
                )

                active["entry"] = bar[1]
                active["D"] = side * (
                    active["entry"] - active["sl"]
                )

                if active["D"] <= 0:
                    counters["Invalid next-open geometry"] += 1
                    active = None
                else:
                    active["tp1"] = (
                        active["entry"] + side * active["D"]
                    )
                    active["tp2"] = (
                        active["entry"] + side * 2 * active["D"]
                    )
                    active["risk_capital"] = (
                        equity * min(bot.RISK, 0.02)
                    )

            if active is not None:
                simulate(active, bar)

                if (
                    active["remaining"] > 1e-9
                    and len(h1) >= 210
                    and len(h4) >= 20
                ):
                    reg, _ = bot.regime(h1, h4)

                    allowed = (
                        ("STRONG_UP", "WEAK_UP")
                        if active["direction"] == "LONG"
                        else ("STRONG_DN", "WEAK_DN")
                    )

                    if reg not in allowed:
                        bot.close_fill(
                            active,
                            active["remaining"],
                            bar[4],
                            bar[0],
                            "REGIME_SHIFT",
                        )

                if active["remaining"] <= 1e-9:
                    pnl = (
                        active["net_r"]
                        * active["risk_capital"]
                    )
                    equity += pnl

                    trades.append({
                        "time": datetime.fromtimestamp(
                            active["signal_ts"] / 1000,
                            timezone.utc,
                        ).isoformat(),
                        "direction": active["direction"],
                        "regime": active["regime"],
                        "entry": active["entry"],
                        "sl": active["sl"],
                        "gross_r": active["gross_r"],
                        "net_r": active["net_r"],
                        "pnl_usdt": pnl,
                        "equity_usdt": equity,
                        "exit_time": datetime.fromtimestamp(
                            bar[0] / 1000,
                            timezone.utc,
                        ).isoformat(),
                        "exit_reason": active["fills"][-1]["reason"],
                    })
                    active = None

        if (
            active is not None
            or bar[0] < start_test
            or i < 260
            or len(h1) < 210
            or len(h4) < 20
            or i == len(c15) - 1
        ):
            continue

        window = c15[max(0, i - 299):i + 1]

        for direction in ("LONG", "SHORT"):
            try:
                sig, gate, detail, gate_data = (
                    bot.gate_pipeline(
                        window, h1, h4, tick, direction
                    )
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Signal error at {bar[0]} "
                    f"{direction}: {exc}"
                ) from exc

            counters[f"{direction} {gate}"] += 1

            if sig is None:
                samples.setdefault(
                    f"{direction} {gate}", detail
                )
                continue

            signals += 1

            active = {
                **sig,
                "remaining": 1.0,
                "active_stop": sig["sl"],
                "tp1_done": False,
                "tp2_done": False,
                "gross_r": 0.0,
                "net_r": 0.0,
                "mfe_r": 0.0,
                "fills": [],
                "pending": True,
            }
            break

    return (
        trades,
        counters,
        samples,
        signals,
        active is not None,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--end", default="")
    parser.add_argument("--out", default="bt_out")
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args()

    if args.days < 1:
        parser.error("--days must be positive")

    if args.end:
        end = int(
            datetime.strptime(
                args.end, "%Y-%m-%d"
            ).replace(
                tzinfo=timezone.utc
            ).timestamp() * 1000
        )
    else:
        end = int(time.time() * 1000)

    start = end - args.days * 86_400_000
    warm = start - 90 * 86_400_000

    print(
        "ALPHAQUANT V6.2 BACKTEST",
        "| Mode:", bot.MODE,
        flush=True,
    )

    if bot.MODE == "STRICT":
        print(
            "STRICT mode: unavailable macro/order flow "
            "will reject signals.",
            flush=True,
        )

    data = []

    for tf in ("15m", "1H", "4H"):
        print("Downloading:", tf, flush=True)
        candles = fetch_history(tf, warm, end)
        print(tf, len(candles), flush=True)
        data.append(candles)

    if any(len(x) == 0 for x in data):
        raise SystemExit(
            "Missing candle data. Check OKX connection."
        )

    tick = bot.tick_size()

    trades, gates, samples, signals, unfinished = (
        backtest(*data, tick, start)
    )

    n = len(trades)
    wins = sum(x["net_r"] > 0 for x in trades)
    total = sum(x["net_r"] for x in trades)

    profit = sum(
        max(0, x["net_r"]) for x in trades
    )
    loss = -sum(
        min(0, x["net_r"]) for x in trades
    )

    equity = bot.EQUITY
    peak = equity
    max_dd = 0.0

    for t in trades:
        equity += t["pnl_usdt"]
        peak = max(peak, equity)

        if peak > 0:
            max_dd = max(
                max_dd,
                (peak - equity) / peak
            )

    lines = [
        f"Mode: {bot.MODE}",
        f"Period: {args.days} days",
        f"Completed trades: {n}",
        f"Signals: {signals}",
        f"Unfinished: {unfinished}",
        (
            f"Win rate: {wins / n:.2%}"
            if n else "Win rate: N/A"
        ),
        (
            f"Net expectancy: {total / n:+.4f} R/trade"
            if n else "Net expectancy: N/A"
        ),
        (
            f"Profit factor: {profit / loss:.3f}"
            if loss else "Profit factor: N/A"
        ),
        f"Max drawdown (closed trades): {max_dd:.2%}",
        "",
        "First failed gates:",
    ]

    for gate, count in gates.most_common():
        lines.append(
            f"  {gate}: {count} "
            f"| example: {samples.get(gate, '')}"
        )

    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)

    (output / "summary.txt").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    (output / "trades.json").write_text(
        json.dumps(trades, indent=2),
        encoding="utf-8",
    )

    (output / "gates.json").write_text(
        json.dumps({
            "counts": dict(gates),
            "examples": samples,
        }, indent=2),
        encoding="utf-8",
    )

    print("\n".join(lines), flush=True)

    if args.telegram and bot.TOKEN and bot.CHAT:
        bot.notify(
            "ALPHAQUANT V6.2 BACKTEST\n"
            + "\n".join(lines[:9])
        )


if __name__ == "__main__":
    main()
