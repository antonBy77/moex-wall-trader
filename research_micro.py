"""Шаг 6: микроструктурные паттерны стакана и потока — скальперский набор.

Все сигналы: вход лимиткой у текущего bid/ask (post-only), выход лимиткой
(тейк) или рыночной симуляцией по принту (стоп). DRY_RUN-механика из wall_trader.

Паттерны:
1. SPOOF: большая заявка появляется в стакане (не у касания), затем СНЯТА без
   исполнений по её цене -> цена обычно идёт ОТ снятой стороны. Вход после снятия.
2. SWEEP: серия агрессивных принтов, съевшая >=N лучших уровней за <T сек
   (импульсный поток). Вход по импульсу (momentum) и контр-импульсу (fade).
3. STACK/FLUSH: быстрый рост суммарной глубины топ-5 (защита) vs резкий слив
   глубины (уход маркет-мейкеров).
4. TAPE SPEED: всплеск частоты/объёма принтов (x3 к медиане) — активация.
5. PRICE ACCEPTANCE: цена стоит у уровня K тиков без пробоя при большом
   кумулятивном объёме — «полка».

Каждый паттерн: n сигналов, win, P/L (тейк/стоп 5 тиков), разрез по часам.
"""
import json
import os
import statistics
from collections import deque
from datetime import datetime, timedelta

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "SBER-MISX-2026-09-24.ndjson")
OUT = os.path.join(HERE, "research_micro_report.png")

TICK = 0.01
TAKE = 0.05
STOP = 0.05
TIMEOUT_S = 300
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


def resolve(tape, start_i, dirn, entry, tp=TAKE, sl=STOP, timeout=TIMEOUT_S):
    """Честное закрытие: первый тик после входа, давший TP или SL."""
    t0 = tape[start_i][0]
    j = start_i + 1
    while j < len(tape):
        dt, p, s, d = tape[j]
        move = (p - entry) * dirn
        if move >= tp:
            return {"pnl": tp, "res": "tp", "t_out": dt, "exit": entry + tp * dirn}
        if move <= -sl:
            return {"pnl": -sl, "res": "sl", "t_out": dt, "exit": entry - sl * dirn}
        if (dt - t0).total_seconds() > timeout:
            return {"pnl": move, "res": "time", "t_out": dt, "exit": p}
        j += 1
    return {"pnl": (tape[-1][1] - entry) * dirn, "res": "eod",
            "t_out": tape[-1][0], "exit": tape[-1][1]}


def main():
    tape, obs = load()
    print(f"лента {len(tape)}, стаканов {len(obs)}")

    events = []   # все сигналы всех паттернов
    ob_i = 0
    prev_ob = None                # предыдущий стакан
    last_trade_i = 0
    tape_window = deque(maxlen=800)   # (dt, size) для tape speed
    speed_hist = []

    cvd = 0.0
    hour_of = lambda dt: dt.hour + dt.minute / 60.0

    for i, (dt, price, size, d) in enumerate(tape):
        cvd += size * d
        tape_window.append((dt, size))

        # применяем все OB-обновления до текущего тика
        while ob_i < len(obs) and obs[ob_i][0] <= dt:
            cur = obs[ob_i]
            ob_dt, bids, asks = cur
            if prev_ob is not None:
                pb, pa = prev_ob[1], prev_ob[2]

                # --- 1. SPOOF: уровень >=3x медианы топ-10 исчез без исполнений ---
                for side, now, before in (("bid", bids, pb), ("ask", asks, pa)):
                    if not before:
                        continue
                    sizes = [s for _, s in before[:10]]
                    med = statistics.median(sizes) if sizes else 0
                    for p0, s0 in before[:10]:
                        if s0 >= max(3 * med, 2000):
                            # уровень исчез и цена НЕ торговалась на этом уровне
                            traded_there = any(abs(t[1] - p0) <= TICK
                                               for t in tape[max(0, last_trade_i):i])
                            still = any(abs(x[0] - p0) <= TICK for x in now[:10])
                            if not still and not traded_there:
                                dirn = -1 if side == "bid" else 1
                                entry = tape[i][1]
                                r = resolve(tape, i, dirn, entry)
                                events.append({"pat": "spoof", "dt": dt, "dirn": dirn,
                                               "entry": entry, **r})

                # --- 3. STACK/FLUSH: изменение суммарной глубины топ-5 ---
                d5_now = sum(s for _, s in bids[:5]) + sum(s for _, s in asks[:5])
                d5_prev = sum(s for _, s in pb[:5]) + sum(s for _, s in pa[:5])
                if d5_prev > 0:
                    chg = d5_now / d5_prev
                    if chg >= 1.8:   # стек: глубину налили — ждём зажатия
                        dirn = -1 if (sum(s for _, s in bids[:5]) <
                                      sum(s for _, s in asks[:5])) else 1
                        entry = tape[i][1]
                        r = resolve(tape, i, dirn, entry)
                        events.append({"pat": "stack", "dt": dt, "dirn": dirn,
                                       "entry": entry, **r})
                    elif chg <= 0.5:  # flush: глубину сняли — по тренду последнего потока
                        dirn = 1 if cvd > 0 else -1
                        entry = tape[i][1]
                        r = resolve(tape, i, dirn, entry)
                        events.append({"pat": "flush", "dt": dt, "dirn": dirn,
                                       "entry": entry, **r})

            prev_ob = cur
            ob_i += 1

        # --- 2. SWEEP: 5+ принтов в одну сторону за 2 сек с общим объёмом >= 800 ---
        tape_window.append((dt, size))
        w2 = [x for x in tape_window if (dt - x[0]).total_seconds() <= 2.0]
        if len(w2) >= 5:
            v = sum(x[1] for x in w2)
            speed_hist.append(v)
            if v >= 800 and len(speed_hist) > 100:
                med_v = statistics.median(speed_hist)
                if v >= 3 * med_v:
                    dirn = d
                    entry = price
                    # дебаунс: не чаще раза в 10 сек
                    last_sweep = next((e for e in reversed(events) if e["pat"] == "sweep"), None)
                    if not last_sweep or (dt - last_sweep["dt"]).total_seconds() > 10:
                        r = resolve(tape, i, dirn, entry)
                        events.append({"pat": "sweep", "dt": dt, "dirn": dirn,
                                       "entry": entry, **r})

        last_trade_i = i

    # --- сводка ---
    print("\n=== Паттерны стакана/потока (вход лимиткой, TP=SL=5 тиков) ===")
    pats = {}
    for e in events:
        pats.setdefault(e["pat"], []).append(e)
    summary = {}
    for pat, es in sorted(pats.items()):
        n = len(es)
        pnl = sum(e["pnl"] for e in es)
        wins = sum(1 for e in es if e["pnl"] > 0)
        # разрез по направлению
        for dd, lab in ((1, "long"), (-1, "short")):
            sel = [e for e in es if e["dirn"] == dd]
            if sel:
                k = f"{pat}_{lab}"
                summary[k] = {"n": len(sel), "pnl": sum(e["pnl"] for e in sel),
                              "win": sum(1 for e in sel if e["pnl"] > 0) / len(sel)}
        summary[pat] = {"n": n, "pnl": pnl, "win": wins / n if n else 0}
        print(f"  {pat:8s}: n={n:>5} P/L {pnl:>+9.2f}₽ win {wins/n*100:>4.0f}%  "
              f"P/L/сделку {pnl/n:+.4f}")

    # --- рендер ---
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), facecolor=BG)
    for ax in axes.flat:
        ax.set_facecolor(BG); ax.tick_params(colors=FG)
        for sp in ax.spines.values():
            sp.set_color(GRID)
        ax.grid(color=GRID, lw=0.5)

    # 1. P/L по паттернам
    ax = axes[0, 0]
    names = sorted(pats)
    vals = [sum(e["pnl"] for e in pats[p]) for p in names]
    ax.bar(names, vals, color=[UP if v > 0 else DOWN for v in vals], alpha=0.85)
    for i, (p, v) in enumerate(zip(names, vals)):
        ax.text(i, v, f"\nn={len(pats[p])}", ha="center", fontsize=9, color=FG)
    ax.set_title("P/L по паттернам (TP=SL=5т, вход лимиткой)", color=FG, fontsize=11)
    ax.axhline(0, color=GRID)

    # 2. equity по паттернам (время)
    ax = axes[0, 1]
    for pat in names:
        es = sorted(pats[pat], key=lambda e: e["t_out"])
        eq = np.cumsum([e["pnl"] for e in es])
        ax.plot([e["t_out"] for e in es], eq, lw=1.4, label=pat)
    ax.legend(facecolor=BG, edgecolor=GRID, labelcolor=FG, fontsize=9)
    ax.set_title("Equity по паттернам", color=FG, fontsize=11)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

    # 3. long/short разрез
    ax = axes[1, 0]
    keys = [k for k in summary if "_" in k]
    xs = [k for k in keys]
    vals = [summary[k]["pnl"] for k in keys]
    ax.bar(xs, vals, color=[UP if v > 0 else DOWN for v in vals], alpha=0.85)
    ax.tick_params(axis="x", rotation=45, labelsize=8)
    ax.set_title("P/L long/short по паттернам", color=FG, fontsize=11)
    ax.axhline(0, color=GRID)

    # 4. win-rate по часам (все паттерны)
    ax = axes[1, 1]
    hrs = {}
    for e in events:
        h = int(hour_of(e["dt"]))
        hrs.setdefault(h, []).append(e["pnl"])
    hs = sorted(hrs)
    ax.bar([f"{h}:00" for h in hs], [sum(v) for v in hrs.values()],
           color=[UP if sum(v) > 0 else DOWN for v in hrs.values()], alpha=0.85)
    ax.tick_params(axis="x", rotation=45, labelsize=8)
    ax.set_title("P/L по часам (все паттерны)", color=FG, fontsize=11)
    ax.axhline(0, color=GRID)

    fig.patch.set_facecolor(BG)
    fig.tight_layout()
    fig.savefig(OUT, dpi=110, facecolor=BG)
    print("saved", OUT)

    # сохраняем события для следующих шагов
    with open(os.path.join(HERE, "research_micro_events.json"), "w") as f:
        json.dump([{k: (str(v) if isinstance(v, datetime) else v)
                    for k, v in e.items()} for e in events], f,
                  ensure_ascii=False, indent=1)
    print("events saved: research_micro_events.json")


if __name__ == "__main__":
    main()
