"""Шаг 3: стратегия от айсбергов — "торгуем ВМЕСТЕ с айсбергом, а не против".

Логика: айсберг = крупный скрытый лимитник, который ДОЛИВАЕТ объём при проедании.
Разделяем два исхода айсберга:
  HOLD  — iceberg выстоял (объём восстановился после удара, стена жива через N сек)
  LOSE  — iceberg проели (стена ушла)
Гипотеза: после удара в подтверждённый айсберг цена отскакивает (айсберг = щит).

Фичи на момент удара: размер айсберга, ratio, глубина удара (процент съеденного),
CVD, объём удара, время, дистанция до экстремума.
"""
import json
import os
from collections import deque
from datetime import datetime

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "SBER-MISX-2026-09-24.ndjson")
OUT = os.path.join(HERE, "research_iceberg_report.png")

TICK = 0.01
BG, FG, GRID = "#0d1117", "#c9d1d9", "#21262d"
UP, DOWN, WALL, ICE, BLUE = "#26a641", "#f85149", "#e3b341", "#a371f7", "#58a6ff"


def load():
    tape, obs = [], []
    with open(DATA) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "q" in r:
                if r.get("q") == "ob" and r.get("bid") and r.get("ask"):
                    obs.append((datetime.fromisoformat(r["t"]),
                                [(float(a), float(b)) for a, b in r["bid"]],
                                [(float(a), float(b)) for a, b in r["ask"]]))
                continue
            tape.append((datetime.fromisoformat(r["t"]), float(r["p"]),
                         float(r["s"]), 1 if r["d"] > 0 else -1))
    tape.sort(key=lambda x: x[0])
    return tape, obs


def main():
    tape, obs = load()
    print(f"лента {len(tape)}, стаканов {len(obs)}")

    walls = {}     # (side, price) -> state
    events = []    # удары по айсбергам
    cvd = 0.0
    cvd_hist = deque()
    day_hi = day_lo = None
    ob_i = 0

    for i, (dt, price, size, d) in enumerate(tape):
        cvd += size * d
        cvd_hist.append((dt, cvd))
        while cvd_hist and (dt - cvd_hist[0][0]).total_seconds() > 300:
            cvd_hist.popleft()
        day_hi = price if day_hi is None else max(day_hi, price)
        day_lo = price if day_lo is None else min(day_lo, price)

        while ob_i < len(obs) and obs[ob_i][0] <= dt:
            _, b, a = obs[ob_i]
            for side, levels in (("bid", b), ("ask", a)):
                tops = levels[:10]
                if len(tops) < 3:
                    continue
                for k, (p, s) in enumerate(tops):
                    others = [x[1] for j, x in enumerate(tops) if j != k]
                    avg = sum(others) / len(others)
                    if s >= max(2.0 * avg, 1500):
                        w = walls.setdefault((side, p), {
                            "size0": s, "size": s, "min_seen": s, "hits": 0,
                            "iceberg": False, "refill": 0.0})
                        # ВОССТАНОВЛЕНИЕ ОБЪЁМА = признак айсберга
                        if s > w["size"] * 1.3 and w["min_seen"] < w["size0"] * 0.7:
                            if not w["iceberg"]:
                                w["iceberg"] = True
                            w["refill"] += s - w["size"]
                        w["size"] = s
                        w["size0"] = max(w["size0"], s)
            ob_i += 1

        # удары по стенам
        for side in ("bid", "ask"):
            for key in list(walls):
                s_side, pp = key
                w = walls[key]
                if abs(price - pp) <= 2 * TICK and \
                   ((s_side == "bid" and d == -1) or (s_side == "ask" and d == 1)):
                    w["hits"] += 1
                    # СИГНАЛ: только ПЕРВЫЙ удар по ПОДТВЕРЖДЁННОМУ айсбергу
                    if w["iceberg"] and w["hits"] == 1:
                        eaten_pct = 1 - w["size"] / w["size0"]
                        events.append({
                            "dt": dt, "side": s_side, "price": pp, "i": i,
                            "dirn": 1 if s_side == "bid" else -1,
                            "size": w["size0"], "refill": w["refill"],
                            "eaten_pct": eaten_pct,
                            "cvd5": (cvd - cvd_hist[0][1]) if cvd_hist else 0,
                            "dist_ext": min(day_hi - price, price - day_lo) / TICK,
                        })
                if (s_side == "bid" and d == -1 and price <= pp) or \
                   (s_side == "ask" and d == 1 and price >= pp):
                    w["size"] = max(0.0, w["size"] - size)
                    w["min_seen"] = min(w["min_seen"], w["size"])
                    if w["size"] <= 0:
                        # стена умерла — помечаем все её события исходом LOSE
                        for e in events:
                            key2 = (e["side"], e["price"])
                            if key2 == key and "hold" not in e:
                                e["hold"] = 0
                        walls.pop(key, None)

    # события стен, которые дожили до конца дня = HOLD
    dead_keys = {(e["side"], e["price"]) for e in events if e.get("hold") == 0}
    for e in events:
        if "hold" not in e:
            e["hold"] = 1

    # MFE/MAE после каждого удара по айсбергу (5 мин)
    for e in events:
        i = e["i"]
        t0 = tape[i][0]
        mfe = mae = 0.0
        j = i + 1
        while j < len(tape) and (tape[j][0] - t0).total_seconds() <= 300:
            move = (tape[j][1] - tape[i][1]) * e["dirn"]
            mfe = max(mfe, move); mae = min(mae, move)
            j += 1
        e["mfe"] = mfe / TICK; e["mae"] = abs(mae) / TICK
        # P/L с тейк/стоп 5 тиков
        e["pnl"] = 0.05 if e["mfe"] >= 5 and e["mae"] < 5 else \
                   (-0.05 if e["mae"] >= 5 else (e["mfe"] - e["mae"]) / 2 * TICK)

    n = len(events)
    if not n:
        print("айсбергов не найдено"); return
    holds = sum(1 for e in events if e["hold"])
    wins = sum(1 for e in events if e["pnl"] > 0)
    pnl = sum(e["pnl"] for e in events)
    print(f"\nУдаров по айсбергам (1-й удар по подтверждённому): {n}")
    print(f"P/L (тейк/стоп 5п): {pnl:+.2f} ₽, win {wins}/{n} ({wins/n*100:.0f}%)")

    # разрез по глубине проедания
    print("\nРазрез по eatan_pct (глубина удара):")
    for lo, hi in [(0, .2), (.2, .4), (.4, .6), (.6, 1.01)]:
        sel = [e for e in events if lo <= e["eaten_pct"] < hi]
        if sel:
            p = sum(e["pnl"] for e in sel)
            w = sum(1 for e in sel if e["pnl"] > 0)
            print(f"  eaten {lo:.0%}-{hi:.0%}: n={len(sel)} P/L {p:+.2f} win {w}/{len(sel)} ({w/len(sel)*100:.0f}%)")

    sel = [e for e in events if e["eaten_pct"] >= 0.6]
    if sel:
        p = sum(e["pnl"] for e in sel); w = sum(1 for e in sel if e["pnl"] > 0)
        print(f"  hit #5+: n={len(sel)} P/L {p:+.2f} win {w}/{len(sel)} ({w/len(sel)*100:.0f}%)")

    # --- рендер ---
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), facecolor=BG)
    for ax in axes.flat:
        ax.set_facecolor(BG); ax.tick_params(colors=FG)
        for sp in ax.spines.values():
            sp.set_color(GRID)
        ax.grid(color=GRID, lw=0.5)

    ax = axes[0, 0]
    xs = [e["dt"] for e in events]; ys = [e["eaten_pct"] for e in events]
    sc = ax.scatter(xs, ys, c=[e["pnl"] for e in events], cmap="RdYlGn", s=30)
    plt.colorbar(sc, ax=ax, label="P/L, ₽")
    ax.set_title(f"Удары по айсбергам: глубина проедания × время (n={n})", color=FG, fontsize=11)
    ax.tick_params(labelsize=8)
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%H:%M"))

    ax = axes[0, 1]
    # P/L по объёму refill (айсберг тем надёжнее, чем больше доливал)
    refills = sorted({0, 5000, 20000, 50000, 1e9})
    labels = ["<5К", "5-20К", "20-50К", ">50К"]
    pnl_by_ref = []
    cnt_by_ref = []
    for lo, hi in zip(refills[:-1], refills[1:]):
        sel = [e for e in events if lo <= e["refill"] < hi]
        pnl_by_ref.append(sum(e["pnl"] for e in sel) if sel else 0)
        cnt_by_ref.append(len(sel))
    bars = ax.bar(labels, pnl_by_ref, color=[UP if v > 0 else DOWN for v in pnl_by_ref], alpha=0.85)
    for bar, cnt in zip(bars, cnt_by_ref):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height(),
                f"\n{cnt}", ha="center", fontsize=9, color=FG)
    ax.set_title("P/L по объёму долива айсберга (подпись = n)", color=FG, fontsize=11)
    ax.axhline(0, color=GRID)

    ax = axes[1, 0]
    mfe = [e["mfe"] for e in events]; mae = [e["mae"] for e in events]
    ax.scatter(mfe, mae, c=[UP if e["pnl"] > 0 else DOWN for e in events], s=20, alpha=0.6)
    ax.plot([0, 30], [0, 30], color=GRID, ls="--")
    ax.set_xlabel("MFE, тиков"); ax.set_ylabel("MAE, тиков")
    ax.set_title("MFE vs MAE после удара (выше диагонали = выигрышные)", color=FG, fontsize=11)

    ax = axes[1, 1]
    sizes = [e["size"] for e in events]; pnls = [e["pnl"] for e in events]
    ax.scatter(sizes, pnls, c=ICE, s=25, alpha=0.6)
    ax.set_xlabel("размер айсберга, лотов"); ax.set_ylabel("P/L, ₽")
    ax.set_title("Размер айсберга × P/L", color=FG, fontsize=11)
    ax.axhline(0, color=GRID)

    fig.patch.set_facecolor(BG)
    fig.tight_layout()
    fig.savefig(OUT, dpi=110, facecolor=BG)
    print("saved", OUT)


if __name__ == "__main__":
    main()
