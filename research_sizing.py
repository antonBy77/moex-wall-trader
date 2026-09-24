"""Шаг 4: position sizing вместо фильтрации — базовый лот + увеличение при
сильном CVD в сторону входа (и опционально при глубоком dist от экстремума).

Сравниваем на одних и тех же 1489 rejection-сигналах:
  A) фиксированный лот 1
  B) sizing: 1 лот базово, 3 лота если cvd_5m в сторону входа > порога
  C) sizing + усиление: 3 лота при cvd>порог И dist_ext>порог
  D) анти-сизинг (контроль): 3 лота при ПРОТИВоположном cvd (проверка, что
     связь не случайна — должно быть хуже B)
"""
import json
import os
from collections import deque
from datetime import datetime

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "SBER-MISX-2026-09-24.ndjson")
OUT = os.path.join(HERE, "research_sizing_report.png")

TICK = 0.01
TAKE = 0.05
TIMEOUT_S = 600
BG, FG, GRID = "#0d1117", "#c9d1d9", "#21262d"
UP, DOWN, WALL, BLUE = "#26a641", "#f85149", "#e3b341", "#58a6ff"


def load_and_sim():
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
    signals = []
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

        for side in ("bid", "ask"):
            for key in list(walls):
                s_side, pp = key
                w = walls[key]
                if abs(price - pp) <= 2 * TICK and \
                   ((s_side == "bid" and d == -1) or (s_side == "ask" and d == 1)):
                    w["hits"] += 1
                    if w["hits"] == 1 and w["first"] is None:
                        w["first"] = dt
                        pending.append({
                            "dt": dt, "t0": dt,
                            "dirn": 1 if s_side == "bid" else -1,
                            "entry": pp,
                            "cvd5": (cvd - cvd_hist[0][1]) if cvd_hist else 0,
                            "dist_ext": min(day_hi - price, price - day_lo) / TICK,
                        })
                if (s_side == "bid" and d == -1 and price <= pp) or \
                   (s_side == "ask" and d == 1 and price >= pp):
                    w["size"] = max(0.0, w["size"] - size)
                    if w["size"] <= 0 or (w["size"] < w["size0"] * 0.3 and w["hits"] >= 3):
                        del walls[key]

    for sig in pending:
        sig["pnl"] = (tape[-1][1] - sig["entry"]) * sig["dirn"]
        sig["res"] = "eod"
    signals.extend(pending)
    return signals


def strat_pnl(sigs, cvd_th=None, dist_th=None, anti=False, big=3):
    """PnL-ряд: лот 1 базово, big лотов если фильтр совпал.
    anti=True — инвертирует условие cvd (контроль на случайность)."""
    eq = []
    total = 0.0
    for s in sigs:
        cvd_ok = s["cvd5"] * s["dirn"] > (cvd_th or 1e18)
        if anti:
            cvd_ok = s["cvd5"] * s["dirn"] < -(cvd_th or 1e18)
        dist_ok = dist_th is None or s["dist_ext"] >= dist_th
        lots = big if (cvd_ok and dist_ok) else 1
        total += s["pnl"] * lots
        eq.append(total)
    return eq, total


def max_dd(eq):
    peak = eq[0] if eq else 0
    dd = 0
    for v in eq:
        peak = max(peak, v)
        dd = min(dd, v - peak)
    return dd


def main():
    sigs = load_and_sim()
    sigs.sort(key=lambda s: s["dt"])
    print(f"сигналов: {len(sigs)}")

    cvd_th = 20000       # 20К лотов CVD в сторону входа
    dist_th = 15         # 15+ тиков от экстремума

    variants = {
        "A: лот=1 всегда": strat_pnl(sigs),
        f"B: 3 лота при cvd>+{cvd_th}": strat_pnl(sigs, cvd_th=cvd_th),
        f"C: 3 лота cvd>+{cvd_th} и dist>={dist_th}": strat_pnl(sigs, cvd_th=cvd_th, dist_th=dist_th),
        f"D (контроль): 3 лота при cvd<-{cvd_th}": strat_pnl(sigs, cvd_th=cvd_th, anti=True),
    }
    print(f"\n{'Вариант':45s} {'P/L':>9} {'maxDD':>8} {'P/L/DD':>8}")
    results = {}
    for name, (eq, total) in variants.items():
        dd = max_dd(eq)
        results[name] = (eq, total, dd)
        print(f"{name:45s} {total:>+8.2f}₽ {dd:>+7.2f}₽ {abs(total/dd) if dd else 0:>7.2f}")

    # чувствительность к порогу cvd
    print("\nЧувствительность к порогу cvd (вариант B):")
    sens = []
    for th in [0, 10000, 20000, 40000, 80000]:
        eq, total = strat_pnl(sigs, cvd_th=th)
        n_big = sum(1 for s in sigs if s["cvd5"] * s["dirn"] > th)
        sens.append((th, total, n_big))
        print(f"  cvd>{th:>6}: P/L {total:>+8.2f}₽ (усилено {n_big} из {len(sigs)})")

    # --- рендер ---
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5), facecolor=BG)
    ax = axes[0]
    ts = [s["dt"] for s in sigs]
    colors = {"A": BLUE, "B": UP, "C": WALL, "D": DOWN}
    for (name, (eq, total, dd)), key in zip(results.items(), ["A", "B", "C", "D"]):
        ax.plot(ts, eq, color=colors[key], lw=1.5, label=f"{name}  {total:+.1f}₽")
    ax.legend(facecolor=BG, edgecolor=GRID, labelcolor=FG, fontsize=8)
    ax.set_title("Equity по стратегиям sizing'а (₽/лот)", color=FG, fontsize=11)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.tick_params(labelsize=8)
    ax.grid(color=GRID, lw=0.5)

    ax = axes[1]
    ths = [s[0] for s in sens]
    pnls = [s[1] for s in sens]
    bars = ax.bar([str(t) for t in ths], pnls,
                  color=[UP if v > 0 else DOWN for v in pnls], alpha=0.85)
    for bar, (th, total, n_big) in zip(bars, sens):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height(),
                f"\n{n_big}", ha="center", fontsize=9, color=FG)
    ax.axhline(results["A: лот=1 всегда"][1], color=BLUE, ls="--", lw=1,
               label=f"база лот=1: {results['A: лот=1 всегда'][1]:+.1f}₽")
    ax.set_xlabel("порог cvd_5m в сторону входа, лотов")
    ax.set_ylabel("P/L, ₽")
    ax.set_title("Чувствительность к порогу (подпись = усиленных сделок)", color=FG, fontsize=11)
    ax.legend(facecolor=BG, edgecolor=GRID, labelcolor=FG, fontsize=9)
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
