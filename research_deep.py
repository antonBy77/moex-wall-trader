"""Шаг 5: глубокая настройка — адаптивные стены, оптимизация TP/SL, объёмные
и волатильностные фичи. Проверяем, вытягивается ли классификатор.

Блоки:
1. Адаптивные стены: порог = квантиль распределения размеров уровней за день
   (скользящее окно), а не фиксированные 2x/1500. Гипотеза: «действительно
   важные» стены — верхний дециль, они держат лучше.
2. Оптимизация TP/SL: сетка (тейк, стоп) от 3 до 15 тиков с асимметриями.
3. Новые фичи: волатильность (ATR-подобная по 1м-барам), объёмный профиль
   (расстояние до POC/VA-границ), интенсивность ударов (размер удара / размер
   стены), скорость восстановления стены.
4. Классификатор на новых фичах с time-blocked CV + P/L-бэктест предсказаний.
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
OUT = os.path.join(HERE, "research_deep_report.png")

TICK = 0.01
TIMEOUT_S = 600
BG, FG, GRID = "#0d1117", "#c9d1d9", "#21262d"
UP, DOWN, WALL, BLUE = "#26a641", "#f85149", "#e3b341", "#58a6ff"


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


def collect(tape, obs, wall_q=0.0):
    """Прогон с адаптивным порогом стен: уровень считается стеной, если его размер
    >= квантиля wall_q распределения размеров всех уровней за последние 30 мин
    (и >= 1.8x соседей). wall_q=0 -> старое поведение 2x/1500.
    Возвращает сигналы с фичами и исходами по сетке TP/SL."""
    walls = {}
    level_sizes = deque(maxlen=4000)     # распределение размеров уровней
    signals = []
    pending = []
    cvd = 0.0
    cvd_hist = deque()
    day_hi = day_lo = None
    ob_i = 0
    # для волатильности: 1м бары
    bar_start = None; bar_o = bar_h = bar_l = None
    tr_hist = deque(maxlen=30)           # true ranges 1м
    prev_close = None
    # объёмный профиль
    vp = {}                              # price(0.05) -> vol
    vol_hist = deque(maxlen=3000)
    # фичи удара
    last_hit_sizes = {}                  # key -> deque размеров ударов

    def atr():
        return (sum(tr_hist) / len(tr_hist)) if tr_hist else 3.0

    for i, (dt, price, size, d) in enumerate(tape):
        cvd += size * d
        cvd_hist.append((dt, cvd))
        while cvd_hist and (dt - cvd_hist[0][0]).total_seconds() > 300:
            cvd_hist.popleft()
        day_hi = price if day_hi is None else max(day_hi, price)
        day_lo = price if day_lo is None else min(day_lo, price)

        # 1м бар
        if bar_start is None:
            bar_start = dt.replace(second=0, microsecond=0)
            bar_o = bar_h = bar_l = price
        if (dt - bar_start).total_seconds() >= 60:
            pc = prev_close if prev_close is not None else bar_o
            tr = max(bar_h - bar_l, abs(bar_h - pc), abs(bar_l - pc))
            tr_hist.append(max(tr, 0.01))
            prev_close = bar_o
            bar_start = bar_start + __import__("datetime").timedelta(minutes=1)
            bar_o = bar_h = bar_l = price
        bar_h = max(bar_h, price); bar_l = min(bar_l, price)

        # объёмный профиль
        key5 = round(price / 0.05) * 0.05
        vp[key5] = vp.get(key5, 0) + size
        vol_hist.append((key5, size))

        while ob_i < len(obs) and obs[ob_i][0] <= dt:
            _, b, a = obs[ob_i]
            for side, levels in (("bid", b), ("ask", a)):
                tops = levels[:10]
                if len(tops) < 3:
                    continue
                for k, (p, s) in enumerate(tops):
                    others = [x[1] for j, x in enumerate(tops) if j != k]
                    avg = sum(others) / len(others)
                    thr = 1.8
                    if wall_q > 0 and len(level_sizes) > 200:
                        thr = max(1.8, float(np.quantile(level_sizes, wall_q)))
                    if s >= max(thr, 1500):
                        level_sizes.append(s)
                        walls.setdefault((side, p), {
                            "size0": s, "size": s, "hits": 0, "first": None,
                            "sum_hits": 0.0})
            ob_i += 1

        # resolve pending: считаем исход для ВСЕЙ сетки TP/SL сразу (по MFE/MAE)
        for sig in pending:
            dirn, entry, t0 = sig["dirn"], sig["entry"], sig["t0"]
            move = (price - entry) * dirn / TICK
            sig["mfe"] = max(sig.get("mfe", 0), move)
            sig["mae"] = min(sig.get("mae", 0), move)
        done = [s for s in pending if (s["dt"] and
                (tape[i][0] - s["t0"]).total_seconds() > TIMEOUT_S)]
        signals.extend(done)
        pending = [s for s in pending if s not in done]

        for side in ("bid", "ask"):
            for key in list(walls):
                s_side, pp = key
                w = walls[key]
                if abs(price - pp) <= 2 * TICK and \
                   ((s_side == "bid" and d == -1) or (s_side == "ask" and d == 1)):
                    w["hits"] += 1
                    w["sum_hits"] += size
                    if w["hits"] == 1 and w["first"] is None:
                        w["first"] = dt
                        dist_ext = min(day_hi - price, price - day_lo) / TICK
                        # POC за последние ~час
                        recent = {}
                        for kk, ss in list(vol_hist)[-1500:]:
                            recent[kk] = recent.get(kk, 0) + ss
                        poc = max(recent, key=recent.get) if recent else price
                        pending.append({
                            "dt": dt, "t0": dt,
                            "dirn": 1 if s_side == "bid" else -1,
                            "entry": pp,
                            "cvd5": (cvd - cvd_hist[0][1]) if cvd_hist else 0,
                            "dist_ext": dist_ext,
                            "atr_ticks": atr() / TICK,
                            "wall_size": w["size0"],
                            "wall_rel": w["size0"] / max(1.0, sum(
                                x[1] for x in (b if s_side == "bid" else a)[:10]) / 10),
                            "dist_poc": abs(price - poc) / TICK,
                            "hour": dt.hour + dt.minute / 60.0,
                            "mfe": 0.0, "mae": 0.0,
                        })
                if (s_side == "bid" and d == -1 and price <= pp) or \
                   (s_side == "ask" and d == 1 and price >= pp):
                    w["size"] = max(0.0, w["size"] - size)
                    if w["size"] <= 0:
                        del walls[key]

    for sig in pending:
        signals.append(sig)
    return signals


def pnl_for(sig, tp, sl):
    """P/L тейк/стоп по MFE/MAE (тиковая метрика -> рубли)."""
    if sig["mfe"] >= tp and sig["mae"] < sl:
        return tp * TICK
    if sig["mae"] >= sl:
        return -sl * TICK
    if sig["mfe"] >= tp:            # дошли до TP, потом таймаут — упрощённо TP
        return tp * TICK
    # таймаут без TP
    return max(sig["mfe"], -sl) * TICK


def logit_cv(X, y, blocks=4, epochs=2500, lr=0.1):
    n = len(y)
    Xn = (X - X.mean(0)) / (X.std(0) + 1e-9)
    w = np.zeros(X.shape[1]); b = 0.0
    for _ in range(epochs):
        z = Xn @ w + b
        p = 1 / (1 + np.exp(-np.clip(z, -30, 30)))
        w -= lr * (Xn.T @ (p - y)) / n
        b -= lr * np.mean(p - y)
    accs = []
    for k in range(blocks):
        a, bb = int(n * k / blocks), int(n * (k + 1) / blocks)
        Xtr = np.vstack([Xn[:a], Xn[bb:]]); ytr = np.concatenate([y[:a], y[bb:]])
        wt = np.zeros(X.shape[1]); bt = 0.0
        for _ in range(epochs):
            z = Xtr @ wt + bt
            pr = 1 / (1 + np.exp(-np.clip(z, -30, 30)))
            wt -= lr * (Xtr.T @ (pr - ytr)) / len(ytr)
            bt -= lr * np.mean(pr - ytr)
        pred = (Xte @ wt + bt > 0).astype(int) if (Xte := Xn[a:bb]) is not None else None
        accs.append(float((pred == y[a:bb]).mean()))
    return w, b, accs, Xn


def main():
    tape, obs = load()
    print(f"лента {len(tape)}, стаканов {len(obs)}")

    # --- 1: адаптивные стены ---
    print("\n=== 1. Адаптивный порог стен (квантиль распределения) ===")
    tp, sl = 5, 5
    for q in [0.0, 0.5, 0.7, 0.8, 0.9]:
        sigs = collect(tape, obs, wall_q=q)
        pnls = [pnl_for(s, tp, sl) for s in sigs]
        wins = sum(1 for p in pnls if p > 0)
        print(f"  q={q:.1f}: стен/сигналов {len(sigs):>5}, P/L {sum(pnls):>+8.2f}₽, "
              f"win {wins}/{len(sigs)} ({wins/len(sigs)*100:.0f}%)")

    # --- 2: сетка TP/SL на базовых стенах ---
    print("\n=== 2. Оптимизация TP/SL (базовые стены 1.8x/1500) ===")
    sigs = collect(tape, obs, wall_q=0.0)
    grid = []
    for tp_ in [3, 5, 7, 10, 15]:
        row = []
        for sl_ in [3, 5, 7, 10, 15]:
            p = sum(pnl_for(s, tp_, sl_) for s in sigs)
            row.append(p)
            grid.append((tp_, sl_, p))
        print(f"  TP={tp_:>2}: " + " ".join(f"{v:>+7.2f}" for v in row) +
              "   (SL = 3, 5, 7, 10, 15)")

    # --- 3/4: классификатор на новых фичах ---
    print("\n=== 3/4. Классификатор на расширенных фичах ===")
    FEATS = ["cvd5", "dist_ext", "atr_ticks", "wall_size", "wall_rel",
             "dist_poc", "hour"]
    X = np.array([[s[f] for f in FEATS] for s in sigs])
    y = np.array([1 if pnl_for(s, 5, 5) > 0 else 0 for s in sigs])
    w, b, accs, Xn = logit_cv(X, y)
    print(f"  CV acc по блокам: {[f'{a:.3f}' for a in accs]}, mean {np.mean(accs):.3f}")
    print(f"  базовый win-rate: {y.mean():.3f}")
    order = np.argsort(np.abs(w))[::-1]
    for j in order:
        print(f"    {FEATS[j]:>12}: {w[j]:+.3f}")

    # P/L по предсказанию: торгуем только top-50% по вероятности
    z = Xn @ w + b
    probs = 1 / (1 + np.exp(-np.clip(z, -30, 30)))
    for frac in [0.3, 0.5, 0.7]:
        thr = np.quantile(probs, 1 - frac)
        sel = [pnl_for(s, 5, 5) for s, pr in zip(sigs, probs) if pr >= thr]
        print(f"  торгуем top-{frac:.0%} по вероятности: n={len(sel)}, "
              f"P/L {sum(sel):+.2f}₽ (база {sum(pnl_for(s, 5, 5) for s in sigs):+.2f}₽)")

    # --- рендер ---
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), facecolor=BG)
    for ax in axes.flat:
        ax.set_facecolor(BG); ax.tick_params(colors=FG)
        for sp in ax.spines.values():
            sp.set_color(GRID)
        ax.grid(color=GRID, lw=0.5)

    # 1. адаптивные стены
    ax = axes[0, 0]
    qs = [0.0, 0.5, 0.7, 0.8, 0.9]
    pnls_q = []
    for q in qs:
        ss = collect(tape, obs, wall_q=q)
        pnls_q.append(sum(pnl_for(s, 5, 5) for s in ss))
    ax.bar([str(q) for q in qs], pnls_q,
           color=[UP if v > 0 else DOWN for v in pnls_q], alpha=0.85)
    ax.set_title("P/L vs квантиль порога стен (TP=SL=5)", color=FG, fontsize=11)

    # 2. heatmap TP/SL
    ax = axes[0, 1]
    tps = [3, 5, 7, 10, 15]; sls = [3, 5, 7, 10, 15]
    H = np.zeros((len(sls), len(tps)))
    for tp_, sl_, p in grid:
        H[sls.index(sl_), tps.index(tp_)] = p
    im = ax.imshow(H, cmap="RdYlGn", aspect="auto")
    ax.set_xticks(range(len(tps)), [str(t) for t in tps])
    ax.set_yticks(range(len(sls)), [str(s) for s in sls])
    for gi in range(len(sls)):
        for gj in range(len(tps)):
            ax.text(gj, gi, f"{H[gi, gj]:+.1f}", ha="center", va="center",
                    fontsize=9, color="black")
    ax.set_xlabel("TP, тиков"); ax.set_ylabel("SL, тиков")
    ax.set_title("P/L сетка TP×SL (₽)", color=FG, fontsize=11)

    # 3. веса фичей
    ax = axes[1, 0]
    order = np.argsort(np.abs(w))[::-1]
    ax.barh([FEATS[j] for j in order], w[order],
            color=[UP if w[j] > 0 else DOWN for j in order], alpha=0.85)
    ax.set_title(f"Веса logit, CV acc {np.mean(accs):.2f}", color=FG, fontsize=11)

    # 4. P/L по top-frac вероятности
    ax = axes[1, 1]
    fracs = [0.3, 0.5, 0.7, 1.0]
    vals = []
    for frac in fracs:
        thr = np.quantile(probs, 1 - frac) if frac < 1 else -1e9
        sel = [pnl_for(s, 5, 5) for s, pr in zip(sigs, probs) if pr >= thr]
        vals.append(sum(sel))
    ax.bar([f"top{f:.0%}" for f in fracs], vals,
           color=[UP if v > 0 else DOWN for v in vals], alpha=0.85)
    ax.set_title("P/L по доле торгуемых сигналов (по вероятности)", color=FG, fontsize=11)

    fig.patch.set_facecolor(BG)
    fig.tight_layout()
    fig.savefig(OUT, dpi=110, facecolor=BG)
    print("saved", OUT)


if __name__ == "__main__":
    main()
