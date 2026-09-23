"""Backtest на истории из Redis (finam:trades / finam:dhist / finam:ob).

Идея: живая лента сделок в Redis накапливалась воркерами (finam:trades:* —
последние N сделок строкой, finam:dhist:* — дельта-история списком).
Восстанавливаем ленту, проигрываем WallTracker по реконструированным
снапшотам стакана (приближение: без полного OB-историка тестируем только
rejection-сигналы по принтам против текущего OB-снимка finam:ob:*).

Честное ограничение: полного исторического стакана в Redis нет, поэтому
backtest-режим два:
  - tape-mode: сигналы дельта-импульсов (CVD-всплески) против статичного OB
  - live-log-mode: прогон events.jsonl живых прогонов как референс
"""
import json, os, sys, time
from collections import deque

import redis

from walls import WallTracker
from config import TICK

R = redis.Redis(host="127.0.0.1", port=6380, decode_responses=True)


def load_tape(symbol: str, max_trades: int = 2000) -> list:
    raw = R.get(f"finam:trades:{symbol}")
    if not raw:
        return []
    trades = json.loads(raw)
    # из воркера: {"price","size","side"(1/2),"timestamp"}
    out = []
    for t in trades[-max_trades:]:
        side = "buy" if t.get("side") == 1 else "sell"
        ts = t.get("timestamp", "")
        out.append({"price": float(t["price"]), "size": float(t["size"]),
                    "side": side, "ts": ts})
    out.sort(key=lambda x: x["ts"])
    return out


def load_ob(symbol: str) -> dict:
    raw = R.get(f"finam:ob:{symbol}")
    if not raw:
        return {"bids": [], "asks": []}
    d = json.loads(raw)
    bids = sorted([(float(b["price"]), float(b["size"])) for b in d.get("bids", [])],
                  key=lambda x: -x[0])[:10]
    asks = sorted([(float(a["price"]), float(a["size"])) for a in d.get("asks", [])],
                  key=lambda x: x[0])[:10]
    return {"bids": bids, "asks": asks}


def load_delta_hist(symbol: str, max_len: int = 1440) -> list:
    n = R.llen(f"finam:dhist:{symbol}")
    if not n:
        return []
    raw = R.lrange(f"finam:dhist:{symbol}", max(0, n - max_len), -1)
    out = []
    for r in raw:
        try:
            d = json.loads(r)
            out.append({"t": d["t"], "net": float(d["net"])})
        except Exception:
            pass
    return out


def backtest_tape(symbol: str = "SBER@MISX", ob: dict = None) -> dict:
    """Кубик за кубик проигрываем ленту через WallTracker-подобную логику:
    дельта-импульс (CVD за 30 принтов |z|>1.5) => вход против/по импульсу,
    выход TP 1.5R / SL 1R / конец дня. Комиссия 0.035% за сторону."""
    tape = load_tape(symbol)
    ob = ob or load_ob(symbol)
    if len(tape) < 100 or not ob["bids"]:
        return {"error": f"мало данных: {len(tape)} trades"}

    lot = 10.0
    fee = 0.00035
    results = []
    pos = 0
    entry = 0.0
    window = deque(maxlen=30)
    cvds = []

    for t in tape:
        sign = 1 if t["side"] == "buy" else -1
        window.append(sign * t["size"])
        cvd = sum(window)
        sd = (sum(c * c for c in window) / len(window) - (sum(window) / len(window)) ** 2) ** 0.5 or 1.0
        z = cvd / (sd * len(window) ** 0.5)
        cvds.append(z)
        mid = (ob["bids"][0][0] + ob["asks"][0][0]) / 2
        spread = ob["asks"][0][0] - ob["bids"][0][0]

        # выход
        if pos != 0:
            pnl = (t["price"] - entry) * pos / lot
            hit_tp = pos > 0 and pnl >= 1.5 * abs((entry - (entry - 2 * spread))) or \
                     pos < 0 and pnl >= 1.5 * abs((entry + 2 * spread) - entry)
            hit_sl = pos > 0 and pnl <= -abs(spread) * 2 or pos < 0 and pnl <= -abs(spread) * 2
            if hit_tp or hit_sl:
                fee_r = t["price"] * abs(pos) * fee
                results.append({"pnl": pnl - fee_r, "exit": "TP" if hit_tp else "SL"})
                pos = 0

        # вход по импульсу (momentum: по направлению CVD)
        if pos == 0 and abs(z) > 1.5:
            pos = lot if z > 0 else -lot
            entry = t["price"]
            results.append({"pnl": -t["price"] * abs(pos) * fee / lot, "exit": "entry"})

    if pos != 0:
        last = tape[-1]["price"]
        pnl = (last - entry) * pos / lot
        results.append({"pnl": pnl - last * abs(pos) * fee / lot, "exit": "eod"})

    pnls = [r["pnl"] for r in results]
    wins = [p for p in pnls if p > 0]
    return {
        "symbol": symbol, "trades_in_tape": len(tape),
        "round_trips": len([r for r in results if r["exit"] != "entry"]),
        "total_pnl": round(sum(pnls), 2),
        "win_rate": round(len(wins) / max(1, len([r for r in results if r["exit"] in ("TP", "SL", "eod")])), 3),
        "by_exit": {k: round(sum(r["pnl"] for r in results if r["exit"] == k), 2)
                    for k in ("TP", "SL", "eod")},
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SBER@MISX,GAZP@MISX,LKOH@MISX,GMKN@MISX")
    a = ap.parse_args()
    for sym in a.symbols.split(","):
        r = backtest_tape(sym)
        print(json.dumps(r, ensure_ascii=False))
