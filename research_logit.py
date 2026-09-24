"""Оффлайн-исследователь: на какие исходы реально раскладываются сигналы rejection
и какие фичи их разделяют. Из DT-реплик_001.md: сначала честная статистика,
потом логистическая регрессия с кросс-валидацией по блокам времени.

Выход: research_logit_report.png + текстовая сводка.
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
OUT = os.path.join(HERE, "research_logit_report.png")

TICK = 0.01
TAKE = 0.05
TIMEOUT_S = 600
HORIZON = 300           # горизонт исхода для разметки (сек)
BG, FG, GRID = "#0d1117", "#c9d1d9", "#21262d"
UP, DOWN, WALL, BLUE = "#26a641", "#f85149", "#e3b341", "#58a6ff"

FEATS = ["cvd_5m", "trend_5m", "imb3", "wall_ratio", "wall_size",
         "dist_day_hi", "dist_day_lo", "spread", "vol_1m", "hour_frac"]


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


def max_favorable_adverse(tape, i, dirn, horizon_s=HORIZON):
    """MFE/MAE в тиках за горизонт после сигнала."""
    t0 = tape[i][0]
    mfe = mae = 0.0
    j = i + 1
    while j < len(tape) and (tape[j][0] - t0).total_seconds() <= horizon_s:
        move = (tape[j][1] - tape[i][1]) * dirn
        mfe = max(mfe, move)
        mae = min(mae, move)
        j += 1
    return mfe / TICK, abs(mae) / TICK


def main():
    tape, obs = load()
    print(f"лента {len(tape)}, стаканов {len(obs)}")
    walls = {}                     # (side, price) -> dict
    trades = []                    # фичи + исход
    cvd = 0.0
    cvd_hist = deque()
    imb_hist = deque(maxlen=80)
    vol_1m = deque(maxlen=2000)
    last_spr = 1.0
    day_hi = day_lo = None
    ob_i = 0

    for i, (dt, price, size, d) in enumerate(tape):
        cvd += size * d
        cvd_hist.append((dt, cvd))
        while cvd_hist and (dt - cvd_hist[0][0]).total_seconds() > 300:
            cvd_hist.popleft()
        vol_1m.append((dt, size))
        while vol_1m and (dt - vol_1m[0][0]).total_seconds() > 60:
            vol_1m.popleft()
        day_hi = price if day_hi is None else max(day_hi, price)
        day_lo = price if day_lo is None else min(day_lo, price)

        while ob_i < len(obs) and obs[ob_i][0] <= dt:
            _, b, a = obs[ob_i]
            # обновить стены (2x/1500)
            for side, levels in (("bid", b), ("ask", a)):
                tops = levels[:10]
                if len(tops) < 3:
                    continue
                for k, (p, s) in enumerate(tops):
                    others = [x[1] for j, x in enumerate(tops) if j != k]
                    avg = sum(others) / len(others)
                    if s >= max(2.0 * avg, 1500):
                        walls.setdefault((side, p),
                                         {"size0": s, "size": s, "hits": 0,
                                          "first": None, "ratio": s / avg})
            b3 = sum(s for _, s in b[:3]); a3 = sum(s for _, s in a[:3])
            if b3 + a3:
                imb_hist.append((b3 - a3) / (b3 + a3))
            if len(b) > 1 and len(a) > 1:
                spr = (a[0][0] - b[0][0]) / TICK
                if spr > 0:
                    last_spr = spr
            ob_i += 1

        # съедание стен + сигналы rejection (1-е касание)
        for side in ("bid", "ask"):
            for key in list(walls):
                s_side, pp = key
                w = walls[key]
                if abs(price - pp) <= 2 * TICK and \
                   ((s_side == "bid" and d == -1) or (s_side == "ask" and d == 1)):
                    w["hits"] += 1
                    if w["hits"] == 1 and w["first"] is None:
                        w["first"] = dt
                        dirn = 1 if s_side == "bid" else -1
                        cvd5 = (cvd - cvd_hist[0][1]) if cvd_hist else 0
                        # wall_ratio = размер стены к среднему соседнему объёму
                        # (берём из стакана при регистрации; восстанавливаем из wall_size,
                        #  а точнее — храним avg соседей при создании стены)
                        ratio = w.get("ratio", 2.0)
                        mfe, mae = max_favorable_adverse(tape, i, dirn)
                        trades.append({
                            "dt": dt, "side": s_side, "price": pp,
                            "cvd_5m": cvd5,
                            "trend_5m": (price - tape[max(0, i-300)][1]),
                            "imb3": imb_hist[-1] if imb_hist else 0.0,
                            "wall_ratio": ratio,
                            "wall_size": w["size0"],
                            "dist_day_hi": (day_hi - price) / TICK,
                            "dist_day_lo": (price - day_lo) / TICK,
                            "spread": last_spr,
                            "vol_1m": sum(s2 for _, s2 in vol_1m),
                            "hour_frac": dt.hour + dt.minute / 60.0,
                            "mfe": mfe, "mae": mae,
                            "win": 1 if mfe >= 5 and mae < 5 else 0,
                        })
                if (s_side == "bid" and d == -1 and price <= pp) or \
                   (s_side == "ask" and d == 1 and price >= pp):
                    w["size"] = max(0.0, w["size"] - size)
                    if w["size"] <= 0 or (w["size"] < w["size0"] * 0.3 and w["hits"] >= 3):
                        del walls[key]

    print(f"сигналов rejection: {len(trades)}")
    X = np.array([[t[f] for f in FEATS] for t in trades], dtype=float)
    y = np.array([t["win"] for t in trades])
    mfe = np.array([t["mfe"] for t in trades])
    mae = np.array([t["mae"] for t in trades])
    # нормировка
    Xn = (X - X.mean(0)) / (X.std(0) + 1e-9)
    # логистическая регрессия (свой градиентный спуск, без sklearn)
    w = np.zeros(Xn.shape[1]); b = 0.0; lr = 0.1
    for _ in range(3000):
        z = Xn @ w + b
        p = 1 / (1 + np.exp(-z))
        w -= lr * (Xn.T @ (p - y)) / len(y)
        b -= lr * np.mean(p - y)
    # time-blocked CV: 4 блока подряд
    n = len(y); accs = []
    for k in range(4):
        a, bb = int(n * k / 4), int(n * (k + 1) / 4)
        Xtr = np.vstack([Xn[:a], Xn[bb:]]); ytr = np.concatenate([y[:a], y[bb:]])
        Xte, yte = Xn[a:bb], y[a:bb]
        wt = np.zeros(Xn.shape[1]); bt = 0.0
        for _ in range(2000):
            z = Xtr @ wt + bt
            pr = 1 / (1 + np.exp(-z))
            wt -= lr * (Xtr.T @ (pr - ytr)) / len(ytr)
            bt -= lr * np.mean(pr - ytr)
        pred = (Xte @ wt + bt > 0).astype(int)
        accs.append(float((pred == yte).mean()))
    print("CV acc по блокам:", [f"{a:.3f}" for a in accs], "mean", f"{np.mean(accs):.3f}")

    # --- рендер ---
    fig = plt.figure(figsize=(15, 9), facecolor=BG)
    gs = fig.add_gridspec(2, 2, hspace=0.32, wspace=0.2)
    axes = [fig.add_subplot(gs[i]) for i in range(4)]
    for ax in axes:
        ax.set_facecolor(BG); ax.tick_params(colors=FG)
        for sp in ax.spines.values():
            sp.set_color(GRID)
        ax.grid(color=GRID, lw=0.5)

    # 1. гистограмма MFE/MAE (что вообще достижимо)
    ax = axes[0]
    bins = np.arange(0, 30, 1)
    ax.hist([mfe, mae], bins=bins, label=["MFE (макс ход в нашу сторону, тиков)"],
            color=[UP, DOWN], alpha=0.75)
    ax.legend(facecolor=BG, edgecolor=GRID, labelcolor=FG, fontsize=9)
    ax.set_title(f"Реализуемость: MFE vs MAE за {HORIZON}с (n={len(y)})",
                 color=FG, fontsize=11)
    ax.axvline(5, color=WALL, ls="--", lw=1)

    # 2. win-rate по часам
    ax = axes[1]
    hrs = [t["hour_frac"] for t in trades]
    ax.hist([h for h, w2 in zip(hrs, y) if w2], bins=np.arange(10, 24, 0.5),
            color=UP, alpha=0.7, label="win")
    ax.hist([h for h, w2 in zip(hrs, y) if not w2], bins=np.arange(10, 24, 0.5),
            color=DOWN, alpha=0.5, label="lose")
    ax.legend(facecolor=BG, edgecolor=GRID, labelcolor=FG, fontsize=9)
    ax.set_title("Исходы по часам", color=FG, fontsize=11)

    # 3. веса логистической регрессии
    ax = axes[2]
    order = np.argsort(np.abs(w))[::-1]
    ax.barh([FEATS[i] for i in order], w[order],
            color=[UP if w[i] > 0 else DOWN for i in order], alpha=0.85)
    ax.set_title(f"Веса logit (CV acc {np.mean(accs):.2f})", color=FG, fontsize=11)

    # 4. scatter: wall_ratio vs cvd_5m, цвет = исход
    ax = axes[3]
    wr = X[:, FEATS.index("wall_ratio")]
    cvd5 = X[:, FEATS.index("cvd_5m")]
    ax.scatter(wr[y == 1], cvd5[y == 1], c=UP, s=10, alpha=0.5, label="win")
    ax.scatter(wr[y == 0], cvd5[y == 0], c=DOWN, s=10, alpha=0.5, label="lose")
    ax.legend(facecolor=BG, edgecolor=GRID, labelcolor=FG, fontsize=9)
    ax.set_title("wall_ratio × cvd_5m, цвет=исход", color=FG, fontsize=11)

    fig.patch.set_facecolor(BG)
    fig.savefig(OUT, dpi=110, facecolor=BG)
    print("saved", OUT)

    # корреляции фичей с win
    print("\nКорреляция фичей с win:")
    for j, f in enumerate(FEATS):
        c = np.corrcoef(Xn[:, j], y)[0, 1]
        print(f"  {f:14s} {c:+.3f}")
    print(f"\nБазовый win-rate: {y.mean():.3f}")


if __name__ == "__main__":
    main()
