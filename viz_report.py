#!/usr/bin/env python3
"""Визуализация: equity кривая + PnL распределение + таймлайн событий -> PNG."""
import json, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime

path = sys.argv[1] if len(sys.argv) > 1 else "events.jsonl"
out = sys.argv[2] if len(sys.argv) > 2 else "report.png"
evs = [json.loads(l) for l in open(path) if l.strip()]
if not evs:
    print("нет событий"); sys.exit(0)

ts = [datetime.fromisoformat(e["ts"].replace("+00:00", "+00:00")) for e in evs]

fig, axes = plt.subplots(2, 2, figsize=(14, 9))
fig.suptitle("MOEX Wall Trader — отчёт прогона (DRY_RUN)", fontsize=14, fontweight="bold")

# 1. Equity кривая по realized на закрытиях
ax = axes[0][0]
closes = [(t, e["realized"]) for t, e in zip(ts, evs) if e["type"] in ("SL", "TP")]
all_ev = [(t, e) for t, e in zip(ts, evs) if e["type"] in ("fill", "SL", "TP")]
equity, eq_t, cum = [], [], 0.0
# equity: каждая позиция открывается fill, закрывается SL/TP; на закрытии realized известен кумулятивно
# строим по realized в событиях close (уже кумулятивный)
if closes:
    eq_t = [c[0] for c in closes]
    equity = [c[1] for c in closes]
    ax.plot(eq_t, equity, "o-", color="#2a9d8f", lw=1.5, ms=4)
    ax.axhline(0, color="grey", lw=0.5, ls="--")
    ax.set_title("Equity (realized PnL, ₽)")
else:
    ax.text(0.5, 0.5, "нет закрытий", ha="center", va="center", transform=ax.transAxes)

# 2. Сторона филлов по времени
ax = axes[0][1]
ft = [t for t, e in zip(ts, evs) if e["type"] == "fill"]
fb = [1 if e["side"] == "buy" else -1 for t, e in zip(ts, evs) if e["type"] == "fill"]
ax.bar(ft, fb, width=0.001, color=["#2a9d8f" if b > 0 else "#e76f51" for b in fb])
ax.set_title("Филлы: buy(+)/sell(-)")
ax.set_yticks([-1, 1]); ax.set_yticklabels(["sell", "buy"])

# 3. Цены филлов
ax = axes[1][0]
fp = [e["price"] for e in evs if e["type"] == "fill"]
ax.plot(ft, fp, ".", ms=5, color="#264653")
ax.set_title("Цены исполнения")

# 4. Размер позиции
ax = axes[1][1]
pt = [t for t, e in zip(ts, evs) if e["type"] == "fill" and "pos" in e]
pv = [e["pos"] for t, e in zip(ts, evs) if e["type"] == "fill" and "pos" in e]
if pt:
    ax.step(pt, pv, where="post", color="#e9c46a", lw=1.5)
ax.set_title("Позиция (лотов)")

plt.tight_layout()
plt.savefig(out, dpi=120)
print(f"saved {out}")
