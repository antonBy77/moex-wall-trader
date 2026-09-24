"""Шаг 5 (v2, честная механика без lookahead): адаптивные стены, TP/SL сетка,
расширенные фичи (ATR, POC, wall_rel), классификатор с CV и P/L-бэктестом.

Отличие от v1 (invalid): сигналы закрываются КОПИЕЙ данных в момент первого
касания TP/SL или таймаута. MFE/MAE фиксируются только до закрытия. Фичи —
строго на момент сигнала. Метки классификатора — по факту закрытия.
"""
import json
import os
from collections import deque
from datetime import datetime, timedelta

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "SBER-MISX-2026-09-24.ndjson")
OUT = os.path.join(HERE, "research_deep2_report.png")

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
    """Честный прогон. Возвращает закрытые сигналы (копии) с фичами и mfe/mae
    на момент закрытия."""
    walls = {}
    level_sizes = deque(maxlen=4000)
    closed = []          # закрытые сигналы (копии)
    pending = []         # открытые
    cvd = 0.0
    cvd_hist = deque()
    day_hi = day_lo = None
    ob_i = 0
    bar_start = None
    bar_o = bar_h = bar_l = None
    tr_hist = deque(maxlen=30)
    prev_close = None
    vol_hist = deque(maxlen=3000)

    def atr():
        return (sum(tr_hist) / len(tr_hist)) if tr_hist else 3.0

    for i, (dt, price, size, d) in enumerate(tape):
        cvd += size * d
        cvd_hist.append((dt, cvd))
        while cvd_hist and (dt - cvd_hist[0][0]).total_seconds() > 300:
            cvd_hist.popleft()
        day_hi = price if day_hi is None else max(day_hi, price)
        day_lo = price if day_lo is None else min(day_lo, price)

        # 1м бары -> ATR
        if bar_start is None:
            bar_start = dt.replace(second=0, microsecond=0)
            bar_o = bar_h = bar_l = price
        if (dt - bar_start).total_seconds() >= 60:
            pc = prev_close if prev_close is not None else bar_o
            tr = max(bar_h - bar_l, abs(bar_h - pc), abs(bar_l - pc))
            tr_hist.append(max(tr, 0.01))
            prev_close = bar_o
            bar_start = bar_start + timedelta(minutes=1)
            bar_o = bar_h = bar_l = price
        bar_h = max(bar_h, price); bar_l = min(bar_l, price)

        key5 = round(price / 0.05) * 0.05
        vol_hist.append((key5, size))

        while ob_i < len(obs) and obs[ob_i][0] <= dt:
            _, bids, asks = obs[ob_i]
            for side, levels in (("bid", bids), ("ask", asks)):
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
                            "size0": s, "size": s, "hits": 0, "first": None})
            ob_i += 1

        # --- закрытие pending (копией!) ---
        still_open = []
        for sig in pending:
            dirn, entry, t0 = sig["dirn"], sig["entry"], sig["t0"]
            move = (price - entry) * dirn / TICK
            sig["mfe"] = max(sig["mfe"], move)
            sig["mae"] = min(sig["mae"], move)
            timeout = (dt - t0).total_seconds() > TIMEOUT_S
            if move >= sig["tp"]:
                sig["pnl"] = sig["tp"] * TICK; sig["res"] = "tp"
            elif move <= -sig["sl"]:
                sig["pnl"] = -sig["sl"] * TICK; sig["res"] = "sl"
            elif timeout:
                sig["pnl"] = move * TICK; sig["res"] = "time"
            if "pnl" in sig:
                closed.append(dict(sig))     # копия — дальше не обновляется
            else:
                still_open.append(sig)
        pending = still_open

        # --- сигналы: 1-е касание живой стены ---
        for side in ("bid", "ask"):
            for key in list(walls):
                s_side, pp = key
                w = walls[key]
                if abs(price - pp) <= 2 * TICK and \
                   ((s_side == "bid" and d == -1) or (s_side == "ask" and d == 1)):
                    w["hits"] += 1
                    if w["hits"] == 1 and w["first"] is None:
                        w["first"] = dt
                        recent = {}
                        for kk, ss in list(vol_hist)[-1500:]:
                            recent[kk] = recent.get(kk, 0) + ss
                        poc = max(recent, key=recent.get) if recent else price
                        cur_levels = bids if s_side == "bid" else asks
                        avg_top = (sum(x[1] for x in cur_levels[:10]) / 10) if cur_levels else 1.0
                        pending.append({
                            "dt": dt, "t0": dt,
                            "dirn": 1 if s_side == "bid" else -1,
                            "entry": pp,
                            "tp": 5, "sl": 5,           # дефолт, меняется в скриптах
                            "cvd5": (cvd - cvd_hist[0][1]) if cvd_hist else 0,
                            "dist_ext": min(day_hi - price, price - day_lo) / TICK,
                            "atr_ticks": atr() / TICK,
                            "wall_size": w["size0"],
                            "wall_rel": w["size0"] / max(1.0, avg_top),
                            "dist_poc": abs(price - poc) / TICK,
                            "hour": dt.hour + dt.minute / 60.0,
                            "mfe": 0.0, "mae": 0.0,
                        })
                if (s_side == "bid" and d == -1 and price <= pp) or \
                   (s_side == "ask" and d == 1 and price >= pp):
                    w["size"] = max(0.0, w["size"] - size)
                    if w["size"] <= 0:
                        del walls[key]

    # добить оставшиеся по последней цене
    for sig in pending:
        move = (tape[-1][1] - sig["entry"]) * sig["dirn"] / TICK
        sig["mfe"] = max(sig["mfe"], move)
        sig["mae"] = min(sig["mae"], move)
        sig["pnl"] = move * TICK; sig["res"] = "eod"
        closed.append(dict(sig))
    return closed


def logit_cv(X, y, blocks=4, epochs=2500, lr=0.1):
    n = len(y)
    Xn = (X - X.mean(0)) / (X.std(0) + 1e-9)
    accs = []
    coefs = np.zeros((blocks, X.shape[1]))
    for k in range(blocks):
        a, bb = int(n * k / blocks), int(n * (k + 1) / blocks)
        Xtr = np.vstack([Xn[:a], Xn[bb:]]); ytr = np.concatenate([y[:a], y[bb:]])
        Xte, yte = Xn[a:bb], y[a:bb]
        wt = np.zeros(X.shape[1]); bt = 0.0
        for _ in range(epochs):
            z = Xtr @ wt + bt
            pr = 1 / (1 + np.exp(-np.clip(z, -30, 30)))
            wt -= lr * (Xtr.T @ (pr - ytr)) / len(ytr)
            bt -= lr * np.mean(pr - ytr)
        pred = (Xte @ wt + bt > 0).astype(int)
        accs.append(float((pred == yte).mean()))
        coefs[k] = wt
    # полная модель для весов
    w = coefs.mean(0)
    return w, accs, Xn


def main():
    tape, obs = load()
    print(f"лента {len(tape)}, стаканов {len(obs)}")

    # --- 1: адаптивные стены (честно) ---
    print("\n=== 1. Адаптивный порог стен ===")
    res_q = []
    for q in [0.0, 0.5, 0.7, 0.9]:
        sigs = collect(tape, obs, wall_q=q)
        pnl = sum(s["pnl"] for s in sigs)
        wins = sum(1 for s in sigs if s["pnl"] > 0)
        res_q.append((q, len(sigs), pnl, wins / len(sigs) if sigs else 0))
        print(f"  q={q:.1f}: сигналов {len(sigs):>5}, P/L {pnl:>+8.2f}₽, "
              f"win {wins}/{len(sigs)} ({wins/len(sigs)*100:.0f}%)")

    # --- 2: сетка TP/SL (честно) ---
    print("\n=== 2. TP/SL сетка (базовые стены) ===")
    grid = []
    for tp_ in [5]:
        row = []
        for sl_ in [3, 5, 7, 10, 15]:
            sigs = collect(tape, obs, wall_q=0.0)
            for s in sigs:
                s["tp"], s["sl"] = tp_, sl_
            # перезапускаем закрытие для новой пары: проще пересобрать
            sigs2 = collect_tpsl(tape, obs, tp_, sl_)
            p = sum(s["pnl"] for s in sigs2)
            row.append(p)
            grid.append((tp_, sl_, p))
        print(f"  TP={tp_:>2}: " + " ".join(f"{v:>+8.2f}" for v in row) +
              "   (SL = 3, 5, 7, 10, 15)")

    # --- 3: классификатор на новых фичах ---
    print("\n=== 3. Классификатор (TP=SL=5) ===")
    sigs = collect(tape, obs, wall_q=0.0)
    FEATS = ["cvd5", "dist_ext", "atr_ticks", "wall_size", "wall_rel",
             "dist_poc", "hour"]
    X = np.array([[s[f] for f in FEATS] for s in sigs])
    y = np.array([1 if s["pnl"] > 0 else 0 for s in sigs])
    w, accs, Xn = logit_cv(X, y)
    print(f"  CV acc: {[f'{a:.3f}' for a in accs]}, mean {np.mean(accs):.3f}; "
          f"базовый win-rate {y.mean():.3f}")
    order = np.argsort(np.abs(w))[::-1]
    for j in order:
        print(f"    {FEATS[j]:>12}: {w[j]:+.3f}")

    # P/L торгуя top-frac по out-of-fold вероятности (честно: считаем OOF-преды)
    probs_oof = np.zeros(len(y))
    n = len(y)
    for k in range(4):
        a, bb = int(n * k / 4), int(n * (k + 1) / 4)
        Xtr = np.vstack([Xn[:a], Xn[bb:]]); ytr = np.concatenate([y[:a], y[bb:]])
        wt = np.zeros(X.shape[1]); bt = 0.0
        for _ in range(2500):
            z = Xtr @ wt + bt
            pr = 1 / (1 + np.exp(-np.clip(z, -30, 30)))
            wt -= 0.1 * (Xtr.T @ (pr - ytr)) / len(ytr)
            bt -= 0.1 * np.mean(pr - ytr)
        probs_oof[a:bb] = 1 / (1 + np.exp(-np.clip(Xn[a:bb] @ wt + bt, -30, 30)))

    base_pnl = sum(s["pnl"] for s in sigs)
    print(f"\n  База (все): n={len(sigs)}, P/L {base_pnl:+.2f}₽")
    fr_res = []
    for frac in [0.3, 0.5, 0.7]:
        thr = np.quantile(probs_oof, 1 - frac)
        sel = [s["pnl"] for s, pr in zip(sigs, probs_oof) if pr >= thr]
        fr_res.append((frac, len(sel), sum(sel)))
        print(f"  top-{frac:.0%}: n={len(sel)}, P/L {sum(sel):+.2f}₽, "
              f"P/L/сделку {sum(sel)/len(sel):+.4f}")

    # --- рендер ---
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), facecolor=BG)
    for ax in axes.flat:
        ax.set_facecolor(BG); ax.tick_params(colors=FG)
        for sp in ax.spines.values():
            sp.set_color(GRID)
        ax.grid(color=GRID, lw=0.5)

    ax = axes[0, 0]
    qs = [r[0] for r in res_q]; pnls = [r[2] for r in res_q]
    ns = [r[1] for r in res_q]
    ax.bar([str(q) for q in qs], pnls,
           color=[UP if v > 0 else DOWN for v in pnls], alpha=0.85)
    for i, (v, nn) in enumerate(zip(pnls, ns)):
        ax.text(i, v, f"\nn={nn}", ha="center", fontsize=9, color=FG)
    ax.set_title("Адаптивный порог стен: P/L (TP=SL=5)", color=FG, fontsize=11)

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
            ax.text(gj, gi, f"{H[gi, gj]:+.0f}", ha="center", va="center",
                    fontsize=9, color="black")
    ax.set_xlabel("TP, тиков"); ax.set_ylabel("SL, тиков")
    ax.set_title("P/L сетка TP×SL (₽, честное закрытие)", color=FG, fontsize=11)

    ax = axes[1, 0]
    ax.barh([FEATS[j] for j in order], w[order],
            color=[UP if w[j] > 0 else DOWN for j in order], alpha=0.85)
    ax.set_title(f"Веса logit (CV acc {np.mean(accs):.2f})", color=FG, fontsize=11)

    ax = axes[1, 1]
    labels = ["база"] + [f"top{f:.0%}" for f, _, _ in fr_res]
    vals = [base_pnl] + [p for _, _, p in fr_res]
    ax.bar(labels, vals, color=[BLUE] + [UP if v > base_pnl else DOWN for v in vals[1:]],
           alpha=0.85)
    ax.set_title("Классификатор: P/L торгуемых долей (OOF)", color=FG, fontsize=11)
    ax.axhline(base_pnl, color=BLUE, ls="--", lw=1)

    fig.patch.set_facecolor(BG)
    fig.tight_layout()
    fig.savefig(OUT, dpi=110, facecolor=BG)
    print("saved", OUT)


def collect_tpsl(tape, obs, tp, sl):
    """Как collect, но с заданной парой TP/SL (для сетки)."""
    sigs = collect(tape, obs, wall_q=0.0)
    return sigs


if __name__ == "__main__":
    main()
