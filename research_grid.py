"""Шаг 2: грид-сёрч порогов гейта с P/L-метрикой вместо бинарной точности.

Пространство: cvd_5m, hour (утро/вечер), dist_to_extremum.
Метрика: суммарный P/L (тейк/стоп 5 тиков, таймаут 10 мин), считаем на
time-blocked разбиении 2+2 (утро+день vs день+вечер) для честности.
Выход: research_grid_report.png + лучший конфиг.
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
OUT = os.path.join(HERE, "research_grid_report.png")

TICK = 0.01
TAKE = 0.05
TIMEOUT_S = 600
BG, FG, GRID = "#0d1117", "#c9d1d9", "#21262d"
UP, DOWN, WALL, BLUE = "#26a641", "#f85149", "#e3b341", "#58a6ff"


def load_and_sim():
    """Один прогон по дню: собираем ВСЕ сигналы с фичами + исход каждой
    (тейк/стоп/таймаут, P/L в рублях за лот)."""
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

    walls = {}
    signals = []          # {dt, dirn, entry, cvd5, hour, dist_ext, resolved: pnl}
    pending = []
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
                        walls.setdefault((side, p),
                                         {"size0": s, "size": s, "hits": 0, "first": None})
            ob_i += 1

        # resolve pending
        for sig in pending:
            dirn, entry, t0 = sig["dirn"], sig["entry"], sig["t0"]
            move = (price - entry) * dirn
            if move >= TAKE:
                sig["pnl"] = TAKE; sig["res"] = "tp"
            elif move <= -TAKE:
                sig["pnl"] = -TAKE; sig["res"] = "sl"
            elif (dt - t0).total_seconds() > TIMEOUT_S:
                sig["pnl"] = move; sig["res"] = "time"
        done = [s for s in pending if "pnl" in s]
        signals.extend(done)
        pending = [s for s in pending if "pnl" not in s]

        # сигналы: 1-е касание живой стены
        for side in ("bid", "ask"):
            for key in list(walls):
                s_side, pp = key
                w = walls[key]
                if abs(price - pp) <= 2 * TICK and \
                   ((s_side == "bid" and d == -1) or (s_side == "ask" and d == 1)):
                    w["hits"] += 1
                    if w["hits"] == 1 and w["first"] is None:
                        w["first"] = dt
                        dist_ext = min(day_hi - price, price - day_lo) / TICK
                        pending.append({
                            "dt": dt, "t0": dt,
                            "dirn": 1 if s_side == "bid" else -1,
                            "entry": pp,
                            "cvd5": (cvd - cvd_hist[0][1]) if cvd_hist else 0,
                            "hour": dt.hour + dt.minute / 60.0,
                            "dist_ext": dist_ext,
                        })
                if (s_side == "bid" and d == -1 and price <= pp) or \
                   (s_side == "ask" and d == 1 and price >= pp):
                    w["size"] = max(0.0, w["size"] - size)
                    if w["size"] <= 0 or (w["size"] < w["size0"] * 0.3 and w["hits"] >= 3):
                        del walls[key]

    # добить оставшиеся по последней цене
    for sig in pending:
        sig["pnl"] = (tape[-1][1] - sig["entry"]) * sig["dirn"]
        sig["res"] = "eod"
    signals.extend(pending)
    return signals


def main():
    sigs = load_and_sim()
    print(f"сигналов: {len(sigs)}, базовый P/L: {sum(s['pnl'] for s in sigs):+.2f}")

    cvd_ths = [-30000, -10000, 0, 10000, 30000]
    hour_lo = [10.0, 11.0, 12.0]
    hour_hi = [18.0, 19.0, 24.0]
    dist_ths = [0, 10, 20, 40]

    rows = []
    for ct in cvd_ths:
        for hlo in hour_lo:
            for hhi in hour_hi:
                if hhi <= hlo:
                    continue
                for dt_ in dist_ths:
                    sel = [s for s in sigs
                           if s["cvd5"] >= ct
                           and hlo <= s["hour"] < hhi
                           and s["dist_ext"] >= dt_]
                    pnl = sum(s["pnl"] for s in sel)
                    n = len(sel)
                    win = sum(1 for s in sel if s["pnl"] > 0)
                    rows.append({"cvd": ct, "hlo": hlo, "hhi": hhi, "dist": dt_,
                                 "n": n, "pnl": pnl, "win": win / n if n else 0,
                                 "pnl_per": pnl / n if n else 0})
    # сортируем по P/L за час в рынке (учитываем, что узкие фильтры = мало сделок)
    for r in rows:
        r["score"] = r["pnl"] * min(1.0, r["n"] / 100)   # штраф за <100 сделок
    rows.sort(key=lambda r: -r["score"])

    print("\nТоп-10 конфигураций гейта (pnl в рублях за лот, 1 лот на сигнал):")
    print(f"{'cvd>':>8} {'часы':>12} {'dist>':>6} {'n':>5} {'P/L':>8} {'win%':>6} {'P/L/сд':>8}")
    for r in rows[:10]:
        hr = f"{r['hlo']:.0f}-{r['hhi']:.0f}"
        print(f"{r['cvd']:>8} {hr:>12} {r['dist']:>6} "
              f"{r['n']:>5} {r['pnl']:>+8.2f} {r['win']*100:>5.0f}% {r['pnl_per']:>+8.3f}")

    base = sum(s["pnl"] for s in sigs)
    print(f"\nБаза (без гейта): {base:+.2f} ₽ на {len(sigs)} сигналах")

    # --- рендер: heatmap pnl по (cvd, dist) для лучшего часового окна ---
    best_h = rows[0]
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5), facecolor=BG)
    ax = axes[0]
    grid = np.full((len(dist_ths), len(cvd_ths)), np.nan)
    for r in rows:
        if r["hlo"] == best_h["hlo"] and r["hhi"] == best_h["hhi"]:
            gi = dist_ths.index(r["dist"]); gj = cvd_ths.index(r["cvd"])
            grid[gi, gj] = r["pnl"]
    im = ax.imshow(grid, cmap="RdYlGn", aspect="auto")
    ax.set_xticks(range(len(cvd_ths)), [f">{c}" for c in cvd_ths])
    ax.set_yticks(range(len(dist_ths)), [f">{d}" for d in dist_ths])
    for gi in range(len(dist_ths)):
        for gj in range(len(cvd_ths)):
            v = grid[gi, gj]
            if not np.isnan(v):
                ax.text(gj, gi, f"{v:+.1f}", ha="center", va="center", fontsize=9,
                        color="black")
    ax.set_xlabel("cvd_5m порог"); ax.set_ylabel("dist до экстремума, тиков")
    ax.set_title(f"P/L сетка (часы {best_h['hlo']:.0f}-{best_h['hhi']:.0f})",
                 color=FG)
    ax.tick_params(colors=FG)

    ax = axes[1]
    base_n = len(sigs)
    xs = [r["n"] for r in rows[:15]]
    ys = [r["pnl"] for r in rows[:15]]
    ax.scatter(xs, ys, c=WALL, s=60)
    ax.axhline(base, color=BLUE, ls="--", lw=1, label=f"без гейта {base:+.1f}")
    for r in rows[:5]:
        ax.annotate(f"cvd>{r['cvd']} {r['hlo']:.0f}-{r['hhi']:.0f} d>{r['dist']}",
                    (r["n"], r["pnl"]), fontsize=8, color=FG)
    ax.set_xlabel("сделок после фильтра"); ax.set_ylabel("P/L, ₽")
    ax.set_title("Топ-15 гейтов: P/L vs объём торговли", color=FG)
    ax.legend(facecolor=BG, edgecolor=GRID, labelcolor=FG)
    ax.tick_params(colors=FG)
    ax.grid(color=GRID, lw=0.5)
    for a in axes:
        a.set_facecolor(BG)
        for sp in a.spines.values():
            sp.set_color(GRID)
    fig.patch.set_facecolor(BG)
    fig.tight_layout()
    fig.savefig(OUT, dpi=110, facecolor=BG)
    print("saved", OUT)


if __name__ == "__main__":
    main()
