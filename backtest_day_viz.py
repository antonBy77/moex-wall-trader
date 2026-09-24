"""Дневной бэктест с полной визуализацией: цена+стены, equity, кластеры, P/L.

Источник: data/SBER-MISX-2026-09-24.ndjson (стриминговый сбор за 24.09).
Стратегии: rejection (1-е касание, тейк/стоп 5 тиков, таймаут 10 мин).
Гейт: детерминированная реплика (cvd/trend/imb) — как в backtest_gate.py.
Выход: backtest_day_report.png (laya-venv, matplotlib).
"""
import json
import sys
import os
from collections import deque
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

TICK = 0.01
TAKE = 0.05          # тейк/стоп 5 тиков
TIMEOUT_S = 600      # 10 минут
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "data", "SBER-MISX-2026-09-24.ndjson")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "backtest_day_report.png")

BG = "#0d1117"; FG = "#c9d1d9"; GRID = "#21262d"
UP = "#26a641"; DOWN = "#f85149"; WALL = "#e3b341"; ICE = "#a371f7"; BLUE = "#58a6ff"


def load_tape(path):
    tape = []  # (dt, price, size, dir)
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "q" in r:
                continue
            try:
                tape.append((datetime.fromisoformat(r["t"]),
                             float(r["p"]), float(r["s"]),
                             1 if r["d"] > 0 else -1))
            except Exception:
                continue
    tape.sort(key=lambda x: x[0])
    return tape


class Walls:
    """Лёгкий трекер стен по тикам: цена -> {size0, size, hits, first, iceberg}."""

    def __init__(self):
        self.walls = {"bid": {}, "ask": {}}

    def update(self, bids, asks):
        K, MIN = 2.0, 1500
        for side, levels in (("bid", bids), ("ask", asks)):
            tops = levels[:10]
            if len(tops) < 3:
                continue
            for i, (p, s) in enumerate(tops):
                others = [x[1] for j, x in enumerate(tops) if j != i]
                avg = sum(others) / len(others)
                w = self.walls[side].get(p)
                if s >= max(K * avg, MIN):
                    if w is None:
                        self.walls[side][p] = {"size0": s, "size": s, "hits": 0,
                                               "first": None, "iceberg": False,
                                               "min_seen": s}
                elif w and p not in [x[0] for x in tops]:
                    self.walls[side].pop(p, None)  # стена ушла

    def tick(self, dt, price, size, d):
        events = []
        for side in ("bid", "ask"):
            for pp in list(self.walls[side]):
                w = self.walls[side][pp]
                if abs(price - pp) <= 2 * TICK and \
                   ((side == "bid" and d == -1) or (side == "ask" and d == 1)):
                    w["hits"] += 1
                    if w["hits"] == 1:
                        w["first"] = dt
                        events.append(("rejection", side, pp, w))
                if (side == "bid" and d == -1 and price <= pp) or \
                   (side == "ask" and d == 1 and price >= pp):
                    w["size"] = max(0.0, w["size"] - size)
                    w["min_seen"] = min(w["min_seen"], w["size"])
                    if w["min_seen"] < w["size0"] * 0.7 and w["size"] >= w["size0"] * 0.9:
                        w["iceberg"] = True
                    if w["size"] < w["size0"] * 0.3 and w["hits"] >= 3:
                        events.append(("breakout", side, pp, w))
                        del self.walls[side][pp]
                    elif w["size"] <= 0:
                        del self.walls[side][pp]
        return events


def gate(sig_type, side, w, cvd, trend, imb):
    """Реплика гейта: 2+ фактора против входа -> BLOCK.
    Для rejection поток против входа — норма; считаем «против» = поток усиливается."""
    dirn = 1 if (side == "bid") else -1      # bid-стена -> BUY
    against = (cvd * dirn < 0) + (trend * dirn < 0) + (imb * dirn < 0)
    return against < 2                        # 2+ против -> BLOCK


def run_backtest():
    tape = load_tape(DATA)
    print(f"лента: {len(tape)} сделок")
    walls = Walls()
    ob_q = deque(maxlen=4096)
    trades = []          # dicts: t_in, t_out, side, entry, exit, pnl, gated, verdict
    open_pos = []        # [t_in, side, entry, gated, verdict]
    cvd = 0.0
    cvd_1m = deque()     # (dt, cvd) для тренда
    imb_hist = deque(maxlen=60)
    wall_marks = []      # (dt, price, side, kind) для графика
    day_clusters = {}    # price -> [buy, sell]
    px_series = []       # (dt, mid)

    # --- первый проход: читаем OB из файла параллельно ленте нельзя (один файл),
    # поэтому стакан берём вторым проходом из отдельного списка
    obs = []
    with open(DATA) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("q") == "ob" and r.get("bid") and r.get("ask"):
                try:
                    obs.append((datetime.fromisoformat(r["t"]),
                                [(float(a), float(b)) for a, b in r["bid"]],
                                [(float(a), float(b)) for a, b in r["ask"]]))
                except Exception:
                    continue
    print(f"стаканов: {len(obs)}")
    ob_i = 0

    n_sig = 0
    for dt, price, size, d in tape:
        cvd += size * d
        cvd_1m.append((dt, cvd))
        while cvd_1m and (dt - cvd_1m[0][0]).total_seconds() > 300:
            cvd_1m.popleft()
        trend = (cvd - cvd_1m[0][1]) if cvd_1m else 0.0
        # стакан: применяем все обновления до текущего времени
        while ob_i < len(obs) and obs[ob_i][0] <= dt:
            _, b, a = obs[ob_i]
            walls.update(b, a)
            b3 = sum(s for _, s in b[:3]); a3 = sum(s for _, s in a[:3])
            if b3 + a3:
                imb_hist.append((b3 - a3) / (b3 + a3))
            ob_i += 1
        imb = imb_hist[-1] if imb_hist else 0.0

        # закрытие открытых позиций
        for pos in list(open_pos):
            t0, side, entry, gated, verdict = pos
            dirn = 1 if side == "bid" else -1
            hit_tp = price >= entry + TAKE if dirn > 0 else price <= entry - TAKE
            hit_sl = price <= entry - TAKE if dirn > 0 else price >= entry + TAKE
            timeout = (dt - t0).total_seconds() > TIMEOUT_S
            if hit_tp or hit_sl or timeout:
                exit_p = entry + TAKE * dirn if hit_tp else \
                         (entry - TAKE * dirn if hit_sl else price)
                pnl = (exit_p - entry) * dirn
                trades.append({"t_in": t0, "t_out": dt, "side": side,
                               "entry": entry, "exit": exit_p, "pnl": pnl,
                               "gated": gated, "verdict": verdict,
                               "res": "tp" if hit_tp else ("sl" if hit_sl else "time")})
                open_pos.remove(pos)

        for kind, side, pp, w in walls.tick(dt, price, size, d):
            n_sig += 1
            if kind == "rejection" and w["first"] == dt:
                wall_marks.append((dt, pp, side, kind))
                verdict = gate(kind, side, w, cvd, trend, imb)
                if len(open_pos) < 3:
                    open_pos.append([dt, side, pp, True, verdict])
                    if verdict:
                        wall_marks.append((dt, pp, side, "allowed"))
                    else:
                        wall_marks.append((dt, pp, side, "blocked"))
            elif kind == "breakout":
                wall_marks.append((dt, pp, side, kind))

        px_series.append((dt, price))
        key = round(price / 0.05) * 0.05
        c = day_clusters.setdefault(key, [0.0, 0.0])
        c[0 if d > 0 else 1] += size

    # принудительное закрытие на конец дня
    for pos in open_pos:
        t0, side, entry, gated, verdict = pos
        dirn = 1 if side == "bid" else -1
        trades.append({"t_in": t0, "t_out": tape[-1][0], "side": side, "entry": entry,
                       "exit": tape[-1][1], "pnl": (tape[-1][1] - entry) * dirn,
                       "gated": gated, "verdict": verdict, "res": "eod"})
    print(f"сигналов: {n_sig}, сделок: {len(trades)}")
    return px_series, wall_marks, trades, day_clusters


def render(px, marks, trades, clusters):
    fig = plt.figure(figsize=(16, 10), facecolor=BG)
    gs = fig.add_gridspec(3, 2, height_ratios=[2.2, 1, 1], hspace=0.3, wspace=0.15)
    ax1 = fig.add_subplot(gs[0, :]); ax2 = fig.add_subplot(gs[1, :])
    ax3 = fig.add_subplot(gs[2, 0]); ax4 = fig.add_subplot(gs[2, 1])
    for ax in (ax1, ax2, ax3, ax4):
        ax.set_facecolor(BG)
        ax.tick_params(colors=FG)
        for sp in ax.spines.values():
            sp.set_color(GRID)
        ax.grid(color=GRID, lw=0.5)

    # --- 1: цена + стены/сигналы ---
    ts = [t for t, _ in px]; p = [v for _, v in px]
    ax1.plot(ts, p, color=FG, lw=0.6)
    for dt, pp, side, kind in marks:
        if kind == "rejection":
            ax1.scatter([dt], [pp], color=WALL, s=18, zorder=5)
        elif kind == "breakout":
            ax1.scatter([dt], [pp], color=ICE, marker="x", s=30, zorder=5)
        elif kind == "blocked":
            ax1.scatter([dt], [pp], color=DOWN, marker="v", s=26, zorder=6)
    ax1.set_title("SBER@MISX 24.09: цена · жёлтые=1-е касание (rejection) · "
                  "кресты=breakout · красные треуг.=гейт BLOCK",
                  color=FG, fontsize=11)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

    # --- 2: equity кумулятивно (все сигналы vs только ALLOW) ---
    trades.sort(key=lambda t: t["t_out"])
    eq_all, eq_allow = [0], [0]
    t_out = [trades[0]["t_out"]] if trades else [px[0][0]]
    for tr in trades:
        eq_all.append(eq_all[-1] + tr["pnl"])
        if tr["verdict"]:
            eq_allow.append(eq_allow[-1] + tr["pnl"])
        t_out.append(tr["t_out"])
    n_allow = sum(1 for tr in trades if tr["verdict"])
    ax2.plot(t_out[:len(eq_all)], eq_all, color=BLUE, lw=1.6,
             label=f"все сигналы ({len(trades)})")
    ax2.plot(t_out[:len(eq_allow)], eq_allow, color=UP, lw=1.6,
             label=f"гейт ALLOW ({n_allow})")
    ax2.axhline(0, color=GRID, lw=0.8)
    ax2.legend(facecolor=BG, edgecolor=GRID, labelcolor=FG, fontsize=9)
    ax2.set_title("Equity (₽/лот, тейк=стоп=5п, таймаут 10м)", color=FG, fontsize=11)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

    # --- 3: кластеры дня ---
    prices = sorted(clusters, reverse=True)
    mx = max(sum(clusters[k]) for k in prices)
    tot = [sum(clusters[k]) for k in prices]
    buy = [clusters[k][0] for k in prices]
    sell = [clusters[k][1] for k in prices]
    poc = max(clusters, key=lambda k: sum(clusters[k]))
    colors = [WALL if k == poc else UP for k in prices]
    ax3.barh([f"{k:.2f}" for k in prices], tot, color=colors, alpha=0.85)
    ax3.set_title(f"Кластеры дня (шаг 5п, POC {poc:.2f} жёлтым)", color=FG, fontsize=11)
    ax3.tick_params(labelsize=7)

    # --- 4: сводка ---
    ax4.axis("off")
    res_all = [t["pnl"] for t in trades]
    res_allow = [t["pnl"] for t in trades if t["verdict"]]
    res_block = [t["pnl"] for t in trades if not t["verdict"]]
    wins = lambda xs: f"win {sum(1 for x in xs if x > 0)}/{len(xs)}" if xs else "—"
    summ = (f"Все сигналы:  {sum(res_all):+.2f} RUB  ({wins(res_all)})\n"
            f"Гейт ALLOW:   {sum(res_allow):+.2f} RUB  ({wins(res_allow)})\n"
            f"Гейт BLOCK:   {sum(res_block):+.2f} RUB  ({wins(res_block)})\n"
            f"-------------------------\n"
            f"Польза гейта: {sum(res_allow) - sum(res_block):+.2f} RUB\n"
            f"Стены за день: касаний {len(marks)//2}, "
            f"заблокировано {len(res_block)}")
    ax4.text(0.02, 0.95, summ, transform=ax4.transAxes, color=FG,
             fontsize=11, va="top", family="monospace")
    ax4.set_title("Сводка", color=FG, fontsize=11)

    fig.patch.set_facecolor(BG)
    fig.savefig(OUT, dpi=110, facecolor=BG)
    print("saved", OUT)


if __name__ == "__main__":
    px, marks, trades, clusters = run_backtest()
    render(px, marks, trades, clusters)
