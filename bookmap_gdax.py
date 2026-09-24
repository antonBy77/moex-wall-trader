"""bookmap_gdax — TUI-клон gdax-bookmap (lian) на наших данных Finam.

Точная логика оригинала (graph_draw.go / timeslot.go):
  - TimeSlot: колонка времени; каждая строка = ценовой диапазон [Low, Heigh)
  - в слот пишется СТАКАН НА КОНЕЦ слота (StatsCopy) + MaxQuantity уровня
  - яркость ячейки: strength = row.Size / MaxSizeHisto, градиент
    colourGradientor(strength, Fg, Bg) — как в оригинале (j/k меняют MaxSizeHisto)
  - trade dots: BidTradeSize/AskTradeSize слота -> зелёный/оранжевый круг
    (t = size / (maxSizeHisto*0.8))
  - bid/ask lines: зелёная/красная ломаная best bid/ask по слотам
  - клавиши как в оригинале: up/down price steps, j/k яркость, a/d сек/слот,
    стрелки влево/вправо — ширина колонки, c — центр, p — автоскролл

Данные: replay NDJSON (collector_stream) или live (finam-connector).
Терминальный рендер: полутоновые символы + 256 цветов (градиент Fg->Bg).
"""
import argparse
import json
import os
import shutil
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

# градиент fg -> bg: индекс = round(strength * (len-1))
FG = (221, 223, 225)   # #dd dfe1 как в оригинале
BG = (21, 35, 44)      # #15232c
RED = (255, 105, 57)
GREEN = (132, 247, 102)


def rgb256(r, g, b):
    return 16 + 36 * (r * 5 // 256) + 6 * (g * 5 // 256) + (b * 5 // 256)


def gradient_char(strength):
    """символ+цвет: strength 0..1 -> полутон"""
    if strength <= 0:
        return " ", None
    idx = min(int(strength * 10), 9)
    # блоки разной "плотности" — имитация заливки терминалом
    chars = ["░", "░", "▒", "▒", "▓", "▓", "▓", "█", "█", "█"]
    r = int(BG[0] + (FG[0] - BG[0]) * strength)
    g = int(BG[1] + (FG[1] - BG[1]) * strength)
    b = int(BG[2] + (FG[2] - BG[2]) * strength)
    return chars[idx], f"#{r:02x}{g:02x}{b:02x}"


class SlotRow:
    __slots__ = ("size", "max_qty", "bid", "ask")

    def __init__(self):
        self.size = 0.0
        self.max_qty = 0.0
        self.bid = 0.0
        self.ask = 0.0


class TimeSlot:
    def __init__(self, t_from):
        self.t = t_from
        self.rows = {}            # price_key -> SlotRow
        self.max_size = 0.0
        self.bid_trade = 0.0
        self.ask_trade = 0.0
        self.best_bid = 0.0
        self.best_ask = 0.0

    def add_level(self, price, size, side):
        row = self.rows.setdefault(price, SlotRow())
        row.size += size
        if side == "bid":
            row.bid += size
            if size > row.max_qty:
                row.max_qty = size
            if self.best_bid == 0 or price > self.best_bid:
                self.best_bid = price
        else:
            row.ask += size
            if size > row.max_qty:
                row.max_qty = size
            if self.best_ask == 0 or price < self.best_ask:
                self.best_ask = price
        if row.size > self.max_size:
            self.max_size = row.size


class Bookmap:
    def __init__(self, slot_sec=10, cols=90, rows=30, step=0.02):
        self.slot_sec = slot_sec
        self.cols_n = cols
        self.rows_n = rows
        self.step = step                    # PriceSteps (шаг строки цены)
        self.max_histo = 20000.0            # MaxSizeHisto (j/k)
        self.center = True                  # автоскролл по цене (p)
        self.center_px = None
        self.slots = deque(maxlen=cols)     # закрытые слоты
        self.cur = None                     # текущий TimeSlot
        self.trades = deque(maxlen=400)
        self.lock = threading.Lock()

    # ---------- ввод данных ----------
    def _slot_for(self, dt):
        key = int(dt.timestamp() // self.slot_sec) * self.slot_sec
        if self.cur is None or self.cur.t != key:
            if self.cur is not None:
                self.slots.append(self.cur)
            self.cur = TimeSlot(key)
        return self.cur

    def on_book(self, ob):
        with self.lock:
            dt = datetime.now()
            slot = self._slot_for(dt)
            bids = ob.get("bid") or ob.get("bids") or []
            asks = ob.get("ask") or ob.get("asks") or []
            for p, s in bids[:10]:
                slot.add_level(self._pk(p), float(s), "bid")
            for p, s in asks[:10]:
                slot.add_level(self._pk(p), float(s), "ask")

    def on_trade(self, t):
        with self.lock:
            ts = t.get("ts")
            dt = datetime.fromtimestamp(ts) if isinstance(ts, (int, float)) else datetime.now()
            price, size = t["price"], t["size"]
            side = "bid" if t["side"] == "buy" else "ask"
            slot = self._slot_for(dt)
            if side == "bid":
                slot.bid_trade += size
            else:
                slot.ask_trade += size
            self.trades.append((dt, price, size, 1 if side == "bid" else -1))

    def _pk(self, p):
        return round(round(p / self.step) * self.step, 4)

    # ---------- рендер ----------
    def render(self):
        with self.lock:
            slots = list(self.slots)
            if self.cur:
                slots.append(self.cur)
            if not slots:
                return Text("ожидание данных…", style="dim")
            trades = list(self.trades)

        # ценовой диапазон: центр = last trade (или best bid/ask)
        last_px = trades[-1][1] if trades else None
        if last_px is None:
            for s in reversed(slots):
                if s.best_bid:
                    last_px = (s.best_bid + s.best_ask) / 2
                    break
        if last_px is None:
            return Text("ожидание данных…", style="dim")
        if self.center:
            self.center_px = self._pk(last_px)

        mid = self.center_px if self.center_px else self._pk(last_px)
        half = self.rows_n // 2
        p_top = self._pk(mid + half * self.step)
        row_of = {}
        for r in range(self.rows_n):
            row_of[self._pk(p_top - r * self.step)] = r

        # MaxSizeHisto: авто = 60% от максимума (как в оригинале, AutoHistoSize)
        mx = max((s.max_size for s in slots), default=1.0) or 1.0
        histo = self.max_histo if self.max_histo > 0 else mx * 0.6
        mx_trade = max((max(s.bid_trade, s.ask_trade) for s in slots), default=1.0) or 1.0

        # сетка канваса: [row][col]
        grid = [[None] * len(slots) for _ in range(self.rows_n)]
        bid_y, ask_y = [], []
        for ci, s in enumerate(slots):
            for pk, row in s.rows.items():
                r = row_of.get(pk)
                if r is not None:
                    grid[r][ci] = row
            if s.best_bid:
                y = row_of.get(self._pk(s.best_bid))
                if y is not None:
                    bid_y.append((ci, y))
            if s.best_ask:
                y = row_of.get(self._pk(s.best_ask))
                if y is not None:
                    ask_y.append((ci, y))

        out = Text()
        out.append(f"{'цена':>8} ", style="dim")
        out.append("<- прошлое            сейчас ->\n", style="dim italic")
        for r in range(self.rows_n):
            price = self._pk(p_top - r * self.step)
            st = "bold white" if abs(price - self._pk(last_px)) < self.step / 2 else "dim"
            out.append(f"{price:8.2f} ", style=st)
            for ci in range(len(slots)):
                row = grid[r][ci]
                if row is None or row.size <= 0:
                    out.append(" ")
                    continue
                ch, col = gradient_char(min(row.size / histo, 1.0))
                out.append(ch, style=col)
            # trade dots последней колонки
            if slots:
                s = slots[-1]
                dot = None
                if s.bid_trade > 0 and abs(price - self._pk(s.best_bid)) < self.step / 2:
                    t = min(s.bid_trade / (mx_trade * 0.8), 1.0)
                    dot = ("●", f"bold #{GREEN[0]:02x}{GREEN[1]:02x}{GREEN[2]:02x}")
                if s.ask_trade > 0 and abs(price - self._pk(s.best_ask)) < self.step / 2:
                    t = min(s.ask_trade / (mx_trade * 0.8), 1.0)
                    dot = ("●", f"bold #{RED[0]:02x}{RED[1]:02x}{RED[2]:02x}")
                if dot:
                    out.append(dot[0], style=dot[1])
                else:
                    out.append(" ")
            # bid/ask линии
            if any(ci == len(slots) - 1 and y == r for ci, y in bid_y):
                out.append("─", style="bold #84f766")
            elif any(y == r for _, y in ask_y):
                out.append(" ")
            else:
                out.append(" ")
            out.append("\n")

        leg = Text("  ")
        leg.append("██ плотные ", style="#ddddfe")
        leg.append("▓▓ ", style="#9aa4a8")
        leg.append("▒▒ ", style="#5c6a72")
        leg.append("░░ ")
        leg.append(f"│ histo {histo:.0f} (j/k) · слот {self.slot_sec}s (a/d) · "
                   f"шаг {self.step} (up/down) · last {last_px:.2f}", style="dim")
        return Panel(out, title="[bold]gdax-bookmap clone · SBER[/]",
                     subtitle=leg, border_style="#15232c")


def feed_replay(st, path, speed):
    t0 = time.time()
    base = None
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("q") == "ob":
                if base is None:
                    base = datetime.fromisoformat(r["t"])
                try:
                    el = (datetime.fromisoformat(r["t"]) - base).total_seconds() / speed
                except Exception:
                    el = None
                while el is not None and time.time() - t0 < el:
                    time.sleep(0.02)
                st.on_book({"bid": r.get("bid") or [], "ask": r.get("ask") or []})
            elif "p" in r:
                st.on_trade({"price": r["p"], "size": r["s"],
                             "side": "buy" if r.get("d", 0) > 0 else "sell",
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
    ap.add_argument("--speed", type=float, default=10)
    ap.add_argument("--cols", type=int, default=100)
    ap.add_argument("--rows", type=int, default=32)
    ap.add_argument("--slot", type=int, default=10, help="секунд в колонке")
    ap.add_argument("--step", type=float, default=0.02, help="шаг строки цены")
    a = ap.parse_args()

    st = Bookmap(slot_sec=a.slot, cols=a.cols, rows=a.rows, step=a.step)
    if a.replay:
        threading.Thread(target=feed_replay, args=(st, a.replay, a.speed),
                         daemon=True).start()
    else:
        threading.Thread(target=feed_live, args=(st, a.symbol), daemon=True).start()

    con = Console()
    with Live(console=con, refresh_per_second=4) as live:
        while True:
            live.update(st.render())
            time.sleep(0.25)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
