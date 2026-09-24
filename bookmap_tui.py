"""bookmap_tui — Bookmap-подобная тепловая карта стакана в терминале.

Вертикаль: цена. Горизонталь: время (каждая колонка = OB-снапшот).
Яркость ячейки = размер лимитной заявки на этом уровне (глубина).
Поверх: линия последней цены, крупные принты (•), стены (жирная заливка).

Запуск:
    python3 bookmap_tui.py --replay data/SBER-MISX-2026-09-24.ndjson --speed 5
    FINAM_TOKEN=... python3 bookmap_tui.py --symbol SBER@MISX   (live, стримы)

Управление: q — выход. Зум по объёму: +/- (log-масштаб).
"""
import argparse
import json
import math
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
# 256-цветовая шкала: чёрный -> тёмно-синий -> синий -> голубой -> жёлтый -> белый
HEAT = [16, 17, 18, 19, 20, 21, 27, 33, 39, 45, 51,
        50, 49, 48, 47, 46, 82, 118, 154, 190, 226, 227, 228, 229, 230, 231]


class BookState:
    def __init__(self, max_cols=160, price_rows=45, tick_group=0.05):
        self.max_cols = max_cols
        self.rows_n = price_rows
        self.tick_g = tick_group
        self.cols = deque(maxlen=max_cols)   # [(dt, {price: size}, center)]
        self.center = None                   # центральная цена (следует за last)
        self.trades = deque(maxlen=20000)    # (dt, price, size, dir)
        self.px = deque(maxlen=400)          # (dt, price)
        self.log_scale = True
        self.lock = threading.Lock()
        self.cur_ob = {"bids": [], "asks": []}

    def on_book(self, ob):
        with self.lock:
            levels = {}
            for p, s in ob["bids"] + ob["asks"]:
                key = round(p / self.tick_g) * self.tick_g
                levels[key] = levels.get(key, 0.0) + s
            last = self.px[-1][1] if self.px else None
            self.cols.append((datetime.now(), levels, last))
            self.cur_ob = ob

    def on_trade(self, t):
        with self.lock:
            ts = t.get("ts")
            dt = datetime.fromtimestamp(ts) if isinstance(ts, (int, float)) else datetime.now()
            self.trades.append((dt, t["price"], t["size"], 1 if t["side"] == "buy" else -1))
            self.px.append((dt, t["price"]))

    # ---------- рендер ----------
    def render(self):
        with self.lock:
            if not self.cols:
                return Text("ожидание стакана…", style="dim")
            last = self.px[-1][1] if self.px else None
            if last is None:
                return Text("ожидание сделок…", style="dim")
            half = self.rows_n // 2
            p_lo = round((last - half * self.tick_g) / self.tick_g) * self.tick_g
            p_hi = p_lo + self.rows_n * self.tick_g
            row_of = {}
            for r in range(self.rows_n):
                row_of[round((p_lo + r * self.tick_g), 4)] = r

            # максимум объёма для нормировки
            mx = 0.0
            for _, levels, _ in self.cols:
                for p, s in levels.items():
                    if p_lo <= p < p_hi and s > mx:
                        mx = s
            mx = mx or 1.0

            # индекс крупных принтов по колонкам времени
            t_first = self.cols[0][0]
            big = []   # (row, col_frac, size)
            for dt, p, s, d in self.trades:
                if dt < t_first or s < 100:
                    continue
                if not (p_lo <= p < p_hi):
                    continue
                frac = (dt - t_first).total_seconds() / max(
                    1.0, (self.cols[-1][0] - t_first).total_seconds())
                big.append((row_of.get(round(p, 4)), min(int(frac * self.max_cols),
                                                         self.max_cols - 1), s, d))

            grid = Text()
            for r in range(self.rows_n):
                price = round(p_lo + r * self.tick_g, 4)
                grid.append(f"{price:7.2f} ", style="dim")
                for ci, (_, levels, _) in enumerate(self.cols):
                    s = levels.get(price)
                    if s:
                        f = (math.log10(1 + s) / math.log10(1 + mx)) if self.log_scale \
                            else (s / mx)
                        idx = min(int(f * (len(HEAT) - 1)), len(HEAT) - 1)
                        # стена: топ-5% объёма — жирный бланк
                        if s >= mx * 0.35:
                            grid.append("██", style=f"bold #{HEAT[idx]:06x}")
                        else:
                            grid.append("▓" if f > 0.45 else "▒" if f > 0.2 else "░",
                                        style=f"#{HEAT[idx]:06x}")
                    else:
                        grid.append("  ")
                    if ci >= self.max_cols - 1:
                        break
                # маркер последней цены
                if abs(price - last) < self.tick_g / 2:
                    grid.append("◄", style="bold white")
                grid.append("\n")
            # крупные принты поверх нижней строки-легенды
            leg = Text(f"  last {last:.2f} · шкала {'log' if self.log_scale else 'линейная'}"
                       f" · max ячейки {mx:.0f} · • = принт ≥100", style="dim")
            return Panel(grid, title="[dim]BOOKMAP · яркость=глубина лимиток · ██=стена · ◄=last[/dim]",
                         subtitle=leg, border_style="#2a3542")


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
                    st.on_book({"bids": [tuple(x) for x in r["bid"]],
                                "asks": [tuple(x) for x in r["ask"]]})
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
    ap.add_argument("--cols", type=int, default=110)
    ap.add_argument("--rows", type=int, default=45)
    ap.add_argument("--group", type=float, default=0.05, help="группировка цен, ₽")
    a = ap.parse_args()

    st = BookState(max_cols=a.cols, price_rows=a.rows, tick_group=a.group)
    if a.replay:
        threading.Thread(target=feed_replay, args=(st, a.replay, a.speed),
                         daemon=True).start()
    else:
        threading.Thread(target=feed_live, args=(st, a.symbol), daemon=True).start()

    con = Console()
    with Live(console=con, refresh_per_second=2) as live:
        while True:
            live.update(st.render())
            time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
