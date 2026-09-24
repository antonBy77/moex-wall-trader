"""bookmap_tui2 — Bookmap-принципы в TUI, вариант 2: ВРЕМЕННЫЕ КОЛОНКИ.

Ключевое отличие от первой попытки (где стакан рисовали вертикальными барами
у ценовых уровней): здесь история стакана рисуется КАК В BOOKMAP — по оси
времени. Каждая колонка терминала = один OB-снапшот. В строке цены X колонка T
стоит символ, кодирующий объём лимиток на том уровне в тот момент:

    ' ' пусто · '·' <500 · ':' <2К · '+' <5К · 'x' <12К · '▓' <30К · '█' >=30К
    цвет: dim blue -> blue -> cyan -> yellow -> bold white

Рядом с ценовой шкалой: последний best bid/ask объём (DOM-полоска).
Снизу: tape с принтами. Справа: цена линии (последняя сделка) помечена '►'.

Запуск:
    python3 bookmap_tui2.py --replay data/SBER-MISX-2026-09-24.ndjson --speed 5
    FINAM_TOKEN=... python3 bookmap_tui2.py --symbol SBER@MISX
"""
import argparse
import json
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

TICK = 0.01

# пороги объёма -> (символ, цвет)
LEVELS = [
    (500,   "·", "dim blue"),
    (2000,  ":", "blue"),
    (5000,  "+", "cyan"),
    (12000, "x", "bright_cyan"),
    (30000, "▓", "yellow"),
    (1e18,  "█", "bold white"),
]


def style_for(size):
    for th, ch, col in LEVELS:
        if size < th:
            return ch, col
    return LEVELS[-1][1], LEVELS[-1][2]


class BM:
    def __init__(self, cols=100, rows=35, group=0.02):
        self.cols = deque(maxlen=cols)      # (datetime, {pkey: (size, side)})
        self.rows_n = rows
        self.g = group
        self.px = deque(maxlen=600)         # (dt, price)
        self.trades = deque(maxlen=200)     # для tape
        self.big = deque(maxlen=8)
        self.lock = threading.Lock()

    @staticmethod
    def key(p, g):
        return round(round(p / g) * g, 4)

    def on_book(self, ob):
        with self.lock:
            now = datetime.now()
            lv = {}
            bids = ob.get("bid") or ob.get("bids") or []
            asks = ob.get("ask") or ob.get("asks") or []
            for side, src in (("bid", bids), ("ask", asks)):
                sign = -1 if side == "bid" else 1
                for p, s in src[:12]:
                    k = self.key(p, self.g)
                    if k not in lv or lv[k][0] < s:
                        lv[k] = (s, sign)
            self.cols.append((now, lv))

    def on_trade(self, t):
        with self.lock:
            ts = t.get("ts")
            dt = datetime.fromtimestamp(ts) if isinstance(ts, (int, float)) else datetime.now()
            self.px.append((dt, t["price"]))
            self.trades.append((dt, t["price"], t["size"],
                                1 if t["side"] == "buy" else -1))
            if t["size"] >= 300:
                self.big.append((dt, t["price"], t["size"],
                                 1 if t["side"] == "buy" else -1))

    # ---------- рендер ----------
    def render(self):
        with self.lock:
            if not self.cols:
                return Text("ожидание стакана…", style="dim")
            last = self.px[-1][1] if self.px else None
            if last is None:
                return Text("ожидание сделок…", style="dim")

            half = self.rows_n // 2
            p_mid = self.key(last, self.g)
            p_lo = round(p_mid - half * self.g, 4)

            # цена -> строка
            row_of = {round(p_lo + r * self.g, 4): r for r in range(self.rows_n)}

            # последние сделки (для колонки справа от карты)
            recent = [(dt, p, s, d) for dt, p, s, d in self.trades
                      if (self.cols[-1][0] - dt).total_seconds() < 5]

            t = Text()
            t.append("цена    ".ljust(8), style="dim")
            t.append("[история стакана → сейчас]\n", style="dim italic")
            for r in range(self.rows_n):
                price = round(p_lo + r * self.g, 4)
                # шкала цен: подсветим центральную
                lbl = f"{price:7.2f} "
                st = "bold white" if abs(price - last) < self.g / 2 else "dim"
                t.append(lbl, style=st)
                for ci, (_, lv) in enumerate(self.cols):
                    ent = lv.get(price)
                    if ent:
                        s, sign = ent
                        ch, col = style_for(s)
                        t.append(ch, style=col)
                    else:
                        # сделка в этой колонке/строке?
                        t.append(" ")
                # правый край: поток сделок последней секунды
                marks = [x for x in recent if abs(x[1] - price) < self.g / 2]
                if marks:
                    tot_buy = sum(s for *_, s, d in marks if d > 0)
                    tot_sell = sum(s for *_, s, d in marks if d < 0)
                    if tot_buy > tot_sell:
                        t.append("►", style="bold green")
                    else:
                        t.append("◄", style="bold red")
                else:
                    t.append(" ")
                t.append("\n")

            # легенда
            leg = Text("  ")
            for th, ch, col in LEVELS:
                if th == 1e18:
                    leg.append(f"{ch}≥30К ", style=col)
                else:
                    leg.append(f"{ch}<{th//1000}К " if th >= 1000 else f"{ch}<{th} ",
                               style=col)
            leg.append("  ►покупки ◄продажи (5с)", style="dim")
            return Panel(t, title=(
                f"[bold]BOOKMAP TUI[/] · группа {self.g}₽ · колонка=снапшот · "
                f"last [bold]{last:.2f}[/]"),
                subtitle=leg, border_style="#2a3542")

    def render_tape(self):
        tt = Text()
        for dt, p, s, d in list(self.trades)[-8:][::-1]:
            col = "green" if d > 0 else "red"
            arrow = "▶" if d > 0 else "◀"
            tt.append(f"{dt.strftime('%H:%M:%S')} {p:8.2f} {s:>6.0f} {arrow}\n",
                      style=col)
        return Panel(tt, title="[dim]лента[/dim]", border_style="#2a3542")


def feed_replay(st, path, speed):
    t0 = time.time()
    base = None
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "q" in r:
                if r["q"] == "ob" and r.get("bid") and r.get("ask"):
                    if base is None:
                        base = datetime.fromisoformat(r["t"])
                    el = (datetime.fromisoformat(r["t"]) - base).total_seconds() / speed
                    while time.time() - t0 < el:
                        time.sleep(0.02)
                    st.on_book({'bid': [tuple(x) for x in (r.get('bid') or [])],
                                'ask': [tuple(x) for x in (r.get('ask') or [])]})
            else:
                st.on_trade({"price": r["p"], "size": r["s"],
                             "side": "buy" if r["d"] > 0 else "sell",
                             "ts": time.time()})


def feed_live(st, symbol):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    + "/finam-connector")
    from finam_connector import FinamConnector
    fc = FinamConnector(token=os.environ["FINAM_TOKEN"])
    fc.stream_trades(symbol, st.on_trade)
    fc.stream_orderbook(symbol, st.on_book)
    while True:
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SBER@MISX")
    ap.add_argument("--replay")
    ap.add_argument("--speed", type=float, default=5)
    ap.add_argument("--cols", type=int, default=90)
    ap.add_argument("--rows", type=int, default=33)
    ap.add_argument("--group", type=float, default=0.02)
    a = ap.parse_args()

    st = BM(cols=a.cols, rows=a.rows, group=a.group)
    if a.replay:
        threading.Thread(target=feed_replay, args=(st, a.replay, a.speed),
                         daemon=True).start()
    else:
        threading.Thread(target=feed_live, args=(st, a.symbol), daemon=True).start()

    con = Console()
    with Live(console=con, refresh_per_second=3) as live:
        while True:
            from rich.columns import Columns
            live.update(Columns([st.render(), st.render_tape()]))
            time.sleep(0.33)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
