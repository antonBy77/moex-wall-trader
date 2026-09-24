"""Гипотеза-2: hit-счётчик как фильтр ПРОБОЯ. Стену атаковали N раз (объём
проседал и восстанавливался), потом СЪЕЛИ -> пробой должен быть сильнее:
за стеной копились стопы и неудовлетворённый спрос."""
import json, sys
from datetime import datetime

PATH = "data/SBER-MISX-2026-09-24.ndjson"
TICK = 0.01
SPREAD_COST = 0.005
HORIZON_CAP = 600

events = []
with open(PATH) as f:
    for line in f:
        d = json.loads(line)
        if d.get("q") == "ob" and d.get("bid") and d.get("ask"):
            events.append(d)
events.sort(key=lambda x: x["t"])
tp = [datetime.fromisoformat(e["t"]).timestamp() for e in events]


def run(side_wall, min_hits, min_ratio=3.0, take_rub=0.10):
    pos = None
    trades = []
    walls = {}  # p -> [hits, size0, size, last_idx, eaten_idx]
    for i, e in enumerate(events):
        bid, ask = e["bid"], e["ask"]
        if not bid or not ask:
            continue
        bb, ba = bid[0][0], ask[0][0]
        mid = (bb + ba) / 2
        now = tp[i]
        lv = ask if side_wall == "ask" else bid

        if pos:
            if side_wall == "ask":  # long
                hit_tp = bid[0][0] >= pos["tp"]
                broke = mid < pos["entry"] - 3 * TICK and bb < pos["entry"] - 2 * TICK
                pnl_time = bid[0][0] - pos["entry"]
            else:  # short
                hit_tp = ask[0][0] <= pos["tp"]
                broke = mid > pos["entry"] + 3 * TICK and ba > pos["entry"] + 2 * TICK
                pnl_time = pos["entry"] - ask[0][0]
            if hit_tp:
                trades.append(take_rub)
                pos = None
            elif broke:
                pnl = (mid - pos["entry"]) if side_wall == "ask" else (pos["entry"] - mid)
                trades.append(pnl - SPREAD_COST)
                pos = None
            elif now - pos["ts"] > HORIZON_CAP:
                trades.append(pnl_time - SPREAD_COST)
                pos = None
            continue

        for idx in range(min(5, len(lv))):
            p, s = lv[idx]
            w = walls.get(p)
            if w:
                if s < w[2] * 0.9:
                    w[0] += 1
                w[2] = s
                w[3] = i
                eat = (w[1] - s) / w[1] if w[1] else 0
                if eat >= 0.3 and w[4] is None:
                    w[4] = i
            else:
                others = [x[1] for l, x in enumerate(lv) if l != idx]
                ref = sum(others) / len(others) if others else 0
                if ref and s >= max(min_ratio * ref, 3000):
                    walls[p] = [0, s, s, i, None]
        for p in list(walls.keys()):
            if i - walls[p][3] > 5:
                del walls[p]

        fresh = [p for p, w in walls.items() if w[4] == i and w[0] >= min_hits]
        for p in fresh:
            if side_wall == "ask" and ba >= p - 2 * TICK:
                pos = {"entry": bb, "tp": bb + take_rub, "ts": now}
                break
            if side_wall == "bid" and bb <= p + 2 * TICK:
                pos = {"entry": ba, "tp": ba - take_rub, "ts": now}
                break

    wins = len([t for t in trades if t > 0])
    return len(trades), (sum(trades) if trades else 0.0), (wins / len(trades) if trades else 0.0)


if __name__ == "__main__":
    print(f"{'конфиг':30s} {'n':>4s} {'итого':>8s} {'win':>5s}")
    for side, nm in (("ask", "BUY пробой ask"), ("bid", "SELL пробой bid")):
        for mh in (0, 1, 2):
            n, tot, w = run(side, mh)
            print(f"{nm} hits>={mh:<18d} {n:>4d} {tot:>+8.2f} {w:>5.0%}")
