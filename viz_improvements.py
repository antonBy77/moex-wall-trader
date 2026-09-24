"""Отчёт по улучшениям гейта: hit-счётчик, динамический тейк, Laya-контекст."""
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# данные из тестов 24.09 (после обеда)
rows = [
    ("rejection тейк10п\n(базовый)", 52, -0.84, 44),
    ("rejection\nhits>=1", 35, -1.05, 34),
    ("rejection\nhits>=2", 19, -1.46, 21),
    ("rejection\nhits>=3", 16, -1.11, 6),
    ("rejection динамич\nтейк hits>=1", 40, -1.17, 45),
    ("rejection\nтейк5п", 42, +0.09, 55),
    ("breakout-ask\n(BUY)", 25, -0.75, 40),
    ("breakout-bid\n(SELL)", 28, -0.28, 36),
    ("breakout-bid\nhits>=2", 11, -0.07, 45),
]

fig, axes = plt.subplots(1, 2, figsize=(16, 6.5))
fig.suptitle("Улучшения гейта: hit-счётчик, динамический тейк, breakout (24.09, SBER, DRY_RUN)",
             fontsize=13, fontweight="bold")

ax = axes[0]
labels = [r[0] for r in rows]
totals = [r[2] for r in rows]
wins_ = [r[3] for r in rows]
colors = ["#2a9d8f" if v > 0 else "#e76f51" for v in totals]
bars = ax.bar(range(len(rows)), totals, color=colors)
for i, (bar, v, w) in enumerate(zip(bars, totals, wins_)):
    ax.text(bar.get_x() + bar.get_width()/2, v + (0.04 if v >= 0 else -0.09),
            f"{v:+.2f}р\nwin {w}%", ha="center", fontsize=8)
ax.set_xticks(range(len(rows)))
ax.set_xticklabels(labels, fontsize=7.5)
ax.axhline(0, color="grey", lw=0.8)
ax.set_title("PnL стратегий за день (руб, 1 лот)")
ax.grid(alpha=0.2, axis="y")

ax = axes[1]
ax.axis("off")
summary = (
    "ВЫВОДЫ ПО УЛУЧШЕНИЯМ\n"
    "─────────────────────\n"
    "1. hit-счётчик ВРЕДИТ rejection:\n"
    "   стена, отбившая 2-3 атаки, потом\n"
    "   ломается с усиленным напором\n"
    "   (44% → 21% win)\n"
    "\n"
    "2. hit-фильтр пробоя слабо помогает:\n"
    "   SELL -0.28 → -0.07 (hits≥2)\n"
    "\n"
    "3. Динамический тейк (вола-метрика)\n"
    "   не сработал — метрика грубая\n"
    "\n"
    "4. Laya на изолированном state:\n"
    "   allow 100%, не фильтрует.\n"
    "   НУЖЕН РАСШИРЕННЫЙ КОНТЕКСТ:\n"
    "   тренд дня, позиция в диапазоне,\n"
    "   плотность агрессора (нужна лента!)\n"
    "\n"
    "5. Единственный плюс — тейк 5п:\n"
    "   +0.09р, win 55%\n"
    "\n"
    "6. День SELL-смещённый: все BUY\n"
    "   в минусе. Нужен режим-фильтр\n"
    "   (trend/regime gate)\n"
    "\n"
    "СЛЕДУЮЩИЕ ШАГИ:\n"
    "• gRPC-стриминг ленты (агрессор)\n"
    "• Расширенный state для Laya\n"
    "• HMM/тренд-фильтр стороны входа"
)
ax.text(0.02, 0.98, summary, transform=ax.transAxes, fontsize=10.5,
        verticalalignment="top", fontfamily="monospace",
        bbox=dict(boxstyle="round", facecolor="#f5f5f0", edgecolor="#ccc"))

plt.tight_layout()
plt.savefig("improvements_report.png", dpi=110, bbox_inches="tight")
print("saved improvements_report.png")
