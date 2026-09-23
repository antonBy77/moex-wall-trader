"""Бэктест v2: полный WallTracker (с лентой) на SiM6@RTSX — единственный тикер,
где в архиве есть и стаканы (~15с), и сделки с side (4599 шт, май-июнь 2026).

Здесь доступны все три типа сигналов: rejection (нужны принты в стену),
breakout (нужны принты + сброс объёма), pull. Проверяем каждый тип отдельно
против фактического движения mid и против baseline.
"""
import json, sqlite3, sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")
from walls import WallTracker
from backtest_history import _levels, baseline

DB = "/home/user/.openclaw/workspace/scripts/arena/arena_data.db"
SYMBOL = "SiM6@RTSX"
HORIZON = 20          # снапшотов (~5 мин при 15с)
FEE_BPS = 1.0         # фьючерс 0.001%? фактически 0.01 bp, округлим до 1bp круг


def parse_ts_any(s):
    """ISO-Z / ISO+03:00 / наносекунды -> unix."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        pass
    try:
        # обрезаем дробную часть до микросекунд
        if "." in s:
            head, tail = s.split(".", 1)
            frac = "".join(c for c in tail if c.isdigit())[:6]
            for suf in ("Z", "+00:00", "+03:00"):
                tail2 = tail
                if suf and tail2.endswith(suf):
                    tail2 = tail2[: -len(suf)]
                    head_tail_tz = suf
                    break
            else:
                head_tail_tz = ""
            s2 = f"{head}.{frac}{head_tail_tz}"
            return datetime.fromisoformat(s2.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None
    return None


def load(db):
    obs = []
    for tux, bids, asks in db.execute(
            "SELECT ts_unix, bids, asks FROM ob_snapshots WHERE symbol=? ORDER BY ts_unix",
            (SYMBOL,)):
        b, a = _levels(bids), _levels(asks)
        if b and a:
            obs.append({"t": tux, "bids": b, "asks": a})
    trades = []
    for ts, price, size, side in db.execute(
            "SELECT ts, price, size, side FROM trades WHERE symbol=? ORDER BY ts", (SYMBOL,)):
        t = parse_ts_any(ts)
        if t:
            trades.append({"t": t, "price": float(price), "size": float(size),
                           "side": "buy" if side == 1 else "sell"})
    return obs, trades


def run():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    obs, trades = load(db)
    print(f"SiM6@RTSX: {len(obs)} OB-снапшотов, {len(trades)} сделок, "
          f"{obs[0]['t']}..{obs[-1]['t']}")

    tr = WallTracker(k=3.0, min_size=200, hold_min_updates=2, pull_stay_bps=3.0)
    ti = 0
    signals = []
    for i, o in enumerate(obs):
        # лента с прошлого снапшота
        tape = []
        while ti < len(trades) and trades[ti]["t"] <= o["t"]:
            tape.append(trades[ti])
            ti += 1
        mid = (o["bids"][0][0] + o["asks"][0][0]) / 2
        rep = tr.update({"mid": mid, "spread": o["asks"][0][0] - o["bids"][0][0],
                         "imbalance": 0, "bids": o["bids"], "asks": o["asks"],
                         "tape": tape}, tape)
        for s in rep["signals"]:
            s["i"] = i
            signals.append(s)

    results = []
    for s in signals:
        i = s["i"]
        if i + HORIZON >= len(obs):
            continue
        m0 = (obs[i]["bids"][0][0] + obs[i]["asks"][0][0]) / 2
        m1 = (obs[i + HORIZON]["bids"][0][0] + obs[i + HORIZON]["asks"][0][0]) / 2
        move = (m1 / m0 - 1) * 1e4
        d = 1 if s["action"] == "buy" else -1
        results.append({"kind": s["kind"], "pnl": round(d * move - FEE_BPS, 1)})

    print(f"\nСигналов: {len(signals)}, оценено: {len(results)} (горизонт {HORIZON} снапшотов)")
    print(f"{'kind':10s} {'n':>4s} {'avg_bps':>8s} {'win':>5s}")
    for k in ("rejection", "breakout", "pull"):
        rs = [r["pnl"] for r in results if r["kind"] == k]
        if rs:
            w = len([x for x in rs if x > 0])
            print(f"{k:10s} {len(rs):>4d} {sum(rs)/len(rs):>+8.1f} {w/len(rs):>5.0%}")
    allp = [r["pnl"] for r in results]
    if allp:
        print(f"{'ВСЕГО':10s} {len(allp):>4d} {sum(allp)/len(allp):>+8.1f} "
              f"{len([x for x in allp if x>0])/len(allp):>5.0%}")
    print(f"baseline (random): {baseline(db, SYMBOL, horizon=HORIZON)} bps")


if __name__ == "__main__":
    run()
