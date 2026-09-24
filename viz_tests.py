"""Визуализация всех тестов дня: стены, стратегии, сделки -> один отчёт PNG."""
import json, sys, bisect
from datetime import datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

sys.path.insert(0, ".")
PATH = "data/SBER-MISX-2026-09-24.ndjson"
TICK = 0.01

# ---- данные ----
obs, trades_tape = [], []
with open(PATH) as f:
    for line in f:
        d = json.loads(line)
        if d.get("q") == "ob" and d.get("bid") and d.get("ask"):
            obs.append(d)
        elif "p" in d:
            trades_tape.append(d)
obs.sort(key=lambda x: x["t"])
def parse(t): return datetime.fromisoformat(t)
ot = [parse(e["t"]) for e in obs]
mid = [(e["bid"][0][0] + e["ask"][0][0]) / 2 for e in obs]
spread = [e["ask"][0][0] - e["bid"][0][0] for e in obs]
wall_bid = [max((s for _, s in e["bid"][:3]), default=0) for e in obs]
wall_ask = [max((s for _, s in e["ask"][:3]), default=0) for e in obs]

tt = [parse(d["t"]) for d in trades_tape]
pp = [d["p"] for d in trades_tape]
dd = [d["d"] for d in trades_tape]

fig = plt.figure(figsize=(16, 14))
gs = fig.add_gridspec(3, 2, hspace=0.35, wspace=0.25)

# 1. Цена + лента
ax = fig.add_subplot(gs[0, :])
ax.plot(ot, mid, lw=0.8, color="#264653", label="mid")
buys = [(t, p) for t, p, d in zip(tt, pp, dd) if d == 1]
sells = [(t, p) for t, p, d in zip(tt, pp, dd) if d == -1]
if buys: ax.scatter(*zip(*buys), s=3, c="#2a9d8f", alpha=0.5, label=f"buy-принты ({len(buys)})")
if sells: ax.scatter(*zip(*sells), s=3, c="#e76f51", alpha=0.5, label=f"sell-принты ({len(sells)})")
ax.set_title("SBER 24.09: цена + пойманная лента (поллинг 0.35с, покрытие частичное)")
ax.legend(loc="upper left", fontsize=8); ax.grid(alpha=0.2)
ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

# 2. Максимальный размер bid/ask стен (топ-3) vs цена
ax = fig.add_subplot(gs[1, 0])
ax2 = ax.twinx()
ax2.plot(ot, mid, lw=0.6, color="grey", alpha=0.5)
ax.bar(ot, wall_bid, width=0.0008, color="#2a9d8f", alpha=0.4, label="bid-стена (топ-3 max)")
ax.bar(ot, [-w for w in wall_ask], width=0.0008, color="#e76f51", alpha=0.4, label="ask-стена")
ax.set_title("Размер стен (лотов) и цена")
ax.legend(loc="upper left", fontsize=8); ax2.set_yticks([])
ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

# 3. Equity стратегий (из результатов)
ax = fig.add_subplot(gs[1, 1])
try:
    res = json.load(open("breakout_results.json"))
    all_trades = {}
    # перезапускаем прогон, чтобы взять сделки с таймстампами
    from backtest_breakout import run, load as bload
    ev2, tp2 = bload()
    for side, name in (("ask", "breakout-ask->BUY r3"), ("bid", "breakout-bid->SELL r3")):
        r = run(ev2, tp2, side, 3.0)
        if r["trades"]:
            ts = [datetime.fromtimestamp(t["ts"]) for t in r["trades"]]
            eq = []
            c = 0
            for t in r["trades"]:
                c += t["pnl"]; eq.append(c)
            ax.plot(ts, eq, "o-", ms=3, lw=1.2,
                    label=f"{name} ({r['n']} сделок, {r['total']:+.2f}р)")
    ax.axhline(0, color="grey", lw=0.5, ls="--")
    ax.set_title("Equity: пробой съеденной стены (день, 1 лот)")
    ax.legend(fontsize=8); ax.grid(alpha=0.2)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
except Exception as e:
    ax.text(0.5, 0.5, f"нет данных: {e}", ha="center", va="center", transform=ax.transAxes)

# 4. Устойчивость уровней по размеру (главная валидация)
ax = fig.add_subplot(gs[2, 0])
buckets = {"5x+": [0.821, 4250], "3-5x": [0.820, 2986], "1.5-3x": [0.791, 6829], "<1.5x": [0.762, 48375]}
names = list(buckets.keys())
vals = [buckets[b][0] * 100 for b in names]
ns = [buckets[b][1] for b in names]
bars = ax.bar(names, vals, color=["#e76f51", "#e9c46a", "#8ab17d", "#2a9d8f"])
for bar, v, n in zip(bars, vals, ns):
    ax.text(bar.get_x() + bar.get_width()/2, v + 0.1, f"{v:.1f}%\nn={n}",
            ha="center", fontsize=9)
ax.set_ylim(70, 86)
ax.set_title("Устойчивость уровня через 60с vs размер (edge стен)")
ax.set_ylabel("% устоявших"); ax.grid(alpha=0.2, axis="y")

# 5. Сводка стратегий
ax = fig.add_subplot(gs[2, 1])
labels = ["rejection\n(вход у стены)", "breakout ask\n(BUY)", "breakout bid\n(SELL)", "тейк 5п\n(rejection)", "тейк 10п\n(rejection)"]
totals = [-0.77, -0.18, 0.19, 0.09, -0.77]
wins_ = [45, 29, 64, 55, 45]
colors = ["#e76f51" if v < 0 else "#2a9d8f" for v in totals]
bars = ax.bar(labels, totals, color=colors)
for bar, v, w in zip(bars, totals, wins_):
    ax.text(bar.get_x() + bar.get_width()/2, v + (0.03 if v >= 0 else -0.07),
            f"{v:+.2f}р\nwin {w}%", ha="center", fontsize=9)
ax.axhline(0, color="grey", lw=0.8)
ax.set_title("Итог дня по стратегиям (руб, 1 лот, DRY_RUN)")
ax.grid(alpha=0.2, axis="y")
ax.tick_params(axis="x", labelsize=8)

fig.suptitle("MOEX Wall Trader — предварительные тесты 24.09.2026 (SBER, DRY_RUN, без комиссии)",
             fontsize=14, fontweight="bold", y=0.995)
plt.savefig("test_report.png", dpi=110, bbox_inches="tight")
print("saved test_report.png")
