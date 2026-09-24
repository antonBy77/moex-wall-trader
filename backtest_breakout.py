"""Стратегия пробоя съеденной стены (breakout) + визуализации всех тестов.

Логика: ask-стену (продавцов) ПРОЕДАЮТ (объём упал >=30% при боевых принтах
или просто устойчивый сброс) -> вход BUY лимиткой (мейкер) на best bid,
тейк +10п sell-лимиткой (мейкер), стоп при откате под уровень стены.
Зеркально для bid-стены -> SELL.

A/B: то же для обычных уровней (ratio 1.5-3x) — контроль качества сигнала.
"""
import json, sys, bisect
from datetime import datetime

sys.path.insert(0, ".")
PATH = "data/SBER-MISX-2026-09-24.ndjson"
TICK = 0.01
SPREAD_COST = 0.005
HORIZON_CAP = 600


def load():
    events = []
    with open(PATH) as f:
        for line in f:
            d = json.loads(line)
            if d.get("q") == "ob" and d.get("bid") and d.get("ask"):
                events.append(d)
    events.sort(key=lambda x: x["t"])
    tp = [datetime.fromisoformat(e["t"]).timestamp() for e in events]
    return events, tp


def run(events, tp, side_wall="ask", min_ratio=3.0, take_rub=0.10,
        min_eat=0.3, entry_dist=2):
    """Возвращает сделки и события съедания. side_wall='ask' -> BUY."""
    walls = {}
    pos = None
    trades = []
    eaten = []
    d_sign = 1 if side_wall == "ask" else -1

    for i, e in enumerate(events):
        bid, ask = e["bid"], e["ask"]
        if not bid or not ask:
            continue
        bb, ba = bid[0][0], ask[0][0]
        mid = (bb + ba) / 2
        now = tp[i]
        lv = ask if side_wall == "ask" else bid

        # ведём позицию
        if pos:
            if side_wall == "ask":  # long
                hit_tp = bid[0][0] >= pos["tp"]
                broke = mid < pos["level"] - 2 * TICK and bb < pos["level"]
                pnl_time = bid[0][0] - pos["entry"]
            else:  # short
                hit_tp = ask[0][0] <= pos["tp"]
                broke = mid > pos["level"] + 2 * TICK and ba > pos["level"]
                pnl_time = pos["entry"] - ask[0][0]
            if hit_tp:
                trades.append({"pnl": take_rub, "exit": "TP", "i": i,
                               "ts": now, "entry": pos["entry"], "ratio": pos["ratio"]})
                pos = None
            elif broke:
                pnl = (mid - pos["entry"]) if side_wall == "ask" else (pos["entry"] - mid)
                trades.append({"pnl": pnl - SPREAD_COST, "exit": "SL", "i": i,
                               "ts": now, "entry": pos["entry"], "ratio": pos["ratio"]})
                pos = None
            elif now - pos["ts"] > HORIZON_CAP:
                trades.append({"pnl": pnl_time - SPREAD_COST, "exit": "time", "i": i,
                               "ts": now, "entry": pos["entry"], "ratio": pos["ratio"]})
                pos = None
            continue

        # обновляем стены (топ-5)
        for idx in range(min(5, len(lv))):
            p, s = lv[idx]
            others = [x[1] for l, x in enumerate(lv) if l != idx]
            ref = sum(others) / len(others) if others else 0
            ratio = s / ref if ref else 0
            if ratio >= min_ratio and s >= 3000:
                w = walls.get(p)
                if w:
                    w[1] = s
                    w[2] = i
                    eat = (w[0] - s) / w[0] if w[0] else 0
                    if eat >= min_eat and not w[3]:
                        w[3] = True
                        eaten.append({"i": i, "level": p, "ratio": ratio, "eat": eat})
                else:
                    walls[p] = [s, s, i, False]
        for p in list(walls.keys()):
            if i - walls[p][2] > 3:
                del walls[p]

        # вход: только по свежесъеденной стене этого снапшота
        fresh = [ev for ev in eaten if ev["i"] == i]
        for ev in fresh:
            level = ev["level"]
            if side_wall == "ask" and ba >= level - entry_dist * TICK:
                pos = {"entry": bb, "level": level, "tp": bb + take_rub,
                       "ts": now, "ratio": ev["ratio"], "i_open": i}
            elif side_wall == "bid" and bb <= level + entry_dist * TICK:
                pos = {"entry": ba, "level": level, "tp": ba - take_rub,
                       "ts": now, "ratio": ev["ratio"], "i_open": i}

    wins = [t for t in trades if t["pnl"] > 0]
    return {"n": len(trades), "total": sum(t["pnl"] for t in trades),
            "win": len(wins) / len(trades) if trades else 0,
            "trades": trades, "eaten": len(eaten)}


if __name__ == "__main__":
    events, tp = load()
    print(f"стаканов: {len(events)}")
    results = {}
    for side, name in (("ask", "breakout-ask->BUY"), ("bid", "breakout-bid->SELL")):
        for mr in (3.0, 5.0):
            r = run(events, tp, side, mr)
            key = f"{name} r>={mr:.0f}"
            results[key] = r
            if r["n"]:
                print(f"{key:22s} n={r['n']:3d} итого={r['total']:+.2f}р win={r['win']:.0%} "
                      f"(съедено стен: {r['eaten']})")
            else:
                print(f"{key:22s} n=0 (съедено стен: {r['eaten']})")
    # A/B: обычные уровни 1.5-3x
    r = run(events, tp, "ask", 1.6)
    results["AB-обычные(r1.6)"] = r
    print(f"{'AB-обычные(r1.6)':22s} n={r['n']:3d} итого={r['total']:+.2f}р win={r['win']:.0%}")
    json.dump({k: {kk: vv for kk, vv in v.items() if kk != "trades"} | 
               {"trades": v.get("trades", [])} for k, v in results.items()},
              open("breakout_results.json", "w"), ensure_ascii=False, indent=1)
    print("saved breakout_results.json")
