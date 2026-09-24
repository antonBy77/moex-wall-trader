"""wall-tui — терминальный дашборд: стакан, лента, свечи, стены, айсберги.

Данные: live через finam_connector (gRPC-стримы) или из NDJSON-архива коллектора.
Запуск:
    FINAM_TOKEN=... python3 wall_tui.py --symbol SBER@MISX        # live
    python3 wall_tui.py --replay data/SBER-MISX-2026-09-24.ndjson # архив
Управление: q — выход, r — пересчитать (в replay — ускорение x2)
"""
import sys, os, json, time, argparse, threading
from datetime import datetime
from collections import deque

sys.path.insert(0, "/home/user/.hermes/scripts-dev/finam-connector")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich import box

UP, DOWN, WALL_B, WALL_A, ICE = "#2a9d8f", "#e76f51", "#e9c46a", "#e9c46a", "#b388eb"
DIM = "#5a6a7a"


class MarketState:
    """Копит ленту/стакан/свечи и статус стен."""

    def __init__(self, symbol="SBER@MISX", candle_sec=60):
        self.symbol = symbol
        self.candle_sec = candle_sec
        self.tape = deque(maxlen=3000)          # (t, price, size, dir)
        self.book = {"bids": [], "asks": []}
        self.candles = deque(maxlen=60)         # (t, o,h,l,c,vol)
        self._cur = None
        self.walls = {"bid": {}, "ask": {}}     # price -> {size, hits, iceberg}
        self.signals = deque(maxlen=8)
        self.cvd = 0.0
        self.cvd_hist = deque(maxlen=200)
        self.lock = threading.Lock()
        self.tick = 0.01

    def on_trade(self, t):
        with self.lock:
            ts = datetime.fromtimestamp(t["ts"]) if isinstance(t.get("ts"), (int, float)) \
                else datetime.now()
            price, size = t["price"], t["size"]
            d = 1 if t["side"] == "buy" else -1
            self.tape.append((ts, price, size, d))
            self.cvd += size * d
            self.cvd_hist.append((ts, self.cvd))
            # свечи
            bucket = int(ts.timestamp() // self.candle_sec)
            if self._cur is None or self._cur[0] != bucket:
                if self._cur:
                    self.candles.append(self._cur)
                self._cur = [bucket, price, price, price, price, 0.0]
            c = self._cur
            c[2] = max(c[2], price); c[3] = min(c[3], price)
            c[4] = price; c[5] += size
            # касание стен
            for side, sign in (("bid", 1), ("ask", -1)):
                for p in list(self.walls[side]):
                    w = self.walls[side][p]
                    near = abs(price - p) <= 3 * self.tick
                    if near and ((side == "bid" and d == -1) or (side == "ask" and d == 1)):
                        w["hits"] += 1
                        if w["hits"] == 1:
                            self.signals.appendleft(
                                (ts, f"REJECTION {'BUY' if side=='bid' else 'SELL'} @ {p}", WALL_B))
                    # съедание
                    if (side == "bid" and d == -1 and price <= p) or \
                       (side == "ask" and d == 1 and price >= p):
                        w["size"] = max(0, w["size"] - size)
                        if w["size"] < w["size0"] * 0.3 and w["hits"] >= 3:
                            self.signals.appendleft(
                                (ts, f"BREAKOUT {'SELL' if side=='bid' else 'BUY'} @ {p}", DOWN))
                            del self.walls[side][p]
                        elif w["size"] <= 0:
                            del self.walls[side][p]

    def on_book(self, ob):
        with self.lock:
            self.book = {"bids": ob["bids"], "asks": ob["asks"]}
            # детект стен: уровень >= WALL_Kx среднего топ-10 соседей (без самого
            # уровня!) и >= WALL_MIN лотов. Пороги мягче walls.py: TUI — взгляд
            # в реальном времени, лучше показать кандидата раньше.
            WALL_K, WALL_MIN = float(os.environ.get("TUI_WALL_K", 2.0)), \
                float(os.environ.get("TUI_WALL_MIN", 1500))
            for side, levels in (("bid", ob["bids"]), ("ask", ob["asks"])):
                tops = levels[:10]
                if len(tops) < 3:
                    continue
                for p, s in tops:
                    others = [x[1] for x in tops if x[0] != p][:6]
                    avg = sum(others) / len(others) if others else 0
                    if avg and s >= max(WALL_K * avg, WALL_MIN):
                        w = self.walls[side].get(p)
                        if w:
                            # айсберг: ели >=30%, а объём вернулся к >=90% исходного
                            if w["hits"] >= 1 and s >= w["size0"] * 0.9 \
                                    and w.get("min_seen", s) < w["size0"] * 0.7:
                                w["iceberg"] = True
                            w["size"] = s
                            w["min_seen"] = min(w.get("min_seen", s), s)
                            w["last"] = time.time()
                        else:
                            self.walls[side][p] = {"size": s, "size0": s,
                                                   "min_seen": s, "hits": 0,
                                                   "iceberg": False, "last": time.time()}
                # чистка исчезнувших (терпим 30с — стена может мигать на апдейтах)
                prices = {p for p, _ in levels[:12]}
                for p in list(self.walls[side]):
                    if p not in prices and time.time() - self.walls[side][p].get("last", 0) > 30:
                        del self.walls[side][p]


def sparks(hist, width=40, reverse=False):
    """ASCII-спарклайн."""
    if len(hist) < 2:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    vals = [v for _, v in hist][-width:]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    return "".join(blocks[int((v - lo) / rng * 7)] for v in vals)


def render_book(st, height=12):
    tb = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold",
               padding=(0, 1), expand=True)
    tb.add_column("ask size", justify="right", style=DOWN)
    tb.add_column("price", justify="center", style="bold")
    tb.add_column("bid size", justify="right", style=UP)
    bids, asks = st.book["bids"], st.book["asks"]
    mx = max((max((s for _, s in bids[:height]), default=1),
              max((s for _, s in asks[:height]), default=1)))
    n = height
    for i in range(n - 1, -1, -1):
        ap, asz = asks[i] if i < len(asks) else (None, None)
        bp, bsz = bids[i] if i < len(bids) else (None, None)
        aw = "█" * int((asz or 0) / mx * 8)
        bw = "█" * int((bsz or 0) / mx * 8)
        mark_a = " ◆" if ap in st.walls["ask"] else (" ◆" if ap in st.walls["bid"] else "")
        mark_b = " ◆" if bp in st.walls["bid"] else (" ◆" if bp in st.walls["ask"] else "")
        ice_a = " ⛁" if ap in st.walls["ask"] and st.walls["ask"][ap]["iceberg"] else ""
        ice_b = " ⛁" if bp in st.walls["bid"] and st.walls["bid"][bp]["iceberg"] else ""
        tb.add_row(
            f"{asz if asz is not None else '':>8.0f} {aw}" if asz is not None else "",
            f"{ap:.2f}{mark_a}{ice_a}" if ap else "",
            f"{bsz if bsz is not None else '':>8.0f} {bw}" if bsz is not None else "",
        )
    return tb


def render_tape(st, height=12):
    tb = Table(box=box.SIMPLE, show_header=True, padding=(0, 1),
               header_style="bold dim")
    tb.add_column("время", style="dim")
    tb.add_column("цена", justify="right")
    tb.add_column("объём", justify="right")
    tb.add_column("", justify="center")
    for ts, p, s, d in list(st.tape)[-height:][::-1]:
        arrow = "▲" if d > 0 else "▼"
        col = UP if d > 0 else DOWN
        sz = f"{s:.0f}" + ("█" * min(int(s / 100), 6))
        tb.add_row(ts.strftime("%H:%M:%S"), Text(f"{p:.2f}", style=col),
                   Text(sz, style=col), Text(arrow, style=col))
    return tb


def render_candles(st, width=44):
    """ASCII-свечи с цветом (зелёные/красные тела, стены жёлтым, айсберги фиолетовым)."""
    cs = list(st.candles)
    if st._cur:
        cs.append(st._cur)
    if not cs:
        return Text("нет данных", style=DIM)
    cs = cs[-width:]
    hi = max(c[2] for c in cs); lo = min(c[3] for c in cs)
    rows_n = 11
    rng = (hi - lo) or 1
    grid = [[" "] * len(cs) for _ in range(rows_n)]
    colors = [[None] * len(cs) for _ in range(rows_n)]
    for x, c in enumerate(cs):
        o, h, l, cl = c[1], c[2], c[3], c[4]
        col = UP if cl >= o else DOWN
        y = lambda p: min(rows_n - 1, int((hi - p) / rng * (rows_n - 1)))
        for yy in range(y(h), y(l) + 1):          # тень
            grid[yy][x] = "│"; colors[yy][x] = DIM
        top, bot = (y(max(o, cl)), y(min(o, cl)))
        body_h = bot - top
        for yy in range(top, bot + 1):            # тело
            grid[yy][x] = "█" if body_h >= 1 else "▄"
            colors[yy][x] = col
    # стены поверх (жёлтые ───, айсберги фиолетовые ⛁)
    for side in ("bid", "ask"):
        for p, w in st.walls[side].items():
            if lo <= p <= hi:
                yy = int((hi - p) / rng * (rows_n - 1))
                mark = "⛁" if w["iceberg"] else "─"
                ccol = ICE if w["iceberg"] else WALL_B
                for x in range(len(cs)):
                    if colors[yy][x] in (None, DIM):
                        grid[yy][x] = mark
                        colors[yy][x] = ccol
    t = Text()
    for row_c, row_col in zip(grid, colors):
        prev, run = None, 0
        for x, (ch, cc) in enumerate(zip(row_c, row_col)):
            key = cc or DIM
            if key != prev and run:
                pass
            if prev is not None and key != prev:
                pass
            if key != prev:
                prev = key
            t.append(ch, style=key)
        t.append("\n")
    # легенда стен
    wall_labels = []
    for side in ("bid", "ask"):
        for p, w in sorted(st.walls[side].items()):
            if lo <= p <= hi:
                ice = " ⛁АЙСБЕРГ" if w["iceberg"] else ""
                arrow = "▲bid" if side == "bid" else "▼ask"
                wall_labels.append((f"{arrow} {p:.2f}  {w['size']:.0f} лотов  "
                                    f"х{w['hits']}{ice}",
                                    ICE if w["iceberg"] else WALL_B))
    for lbl, col in wall_labels[:4]:
        t.append("\n")
        t.append(lbl, style=col)
    return t


def build_ui(st):
    layout = Layout()
    layout.split_column(
        Layout(name="top", size=3),
        Layout(name="main"),
        Layout(name="bottom", size=6))
    layout["main"].split_row(
        Layout(name="book", ratio=1),
        Layout(name="mid", ratio=2))
    layout["mid"].split_column(Layout(name="candles"), Layout(name="signals", size=8))
    layout["bottom"].split_row(Layout(name="tape"), Layout(name="stats"))

    # top: цена + cvd
    with st.lock:
        last = st.tape[-1] if st.tape else None
        spread = (st.book["asks"][0][0] - st.book["bids"][0][0]) \
            if st.book["bids"] and st.book["asks"] else 0
    px_s = f"{last[1]:.2f}" if last else "—"
    d = last[3] if last else 0
    cvd_sp = sparks(list(st.cvd_hist))
    layout["top"].update(Panel(
        Text.assemble((f" {st.symbol}  ", "bold"),
                      (px_s, "bold " + (UP if d >= 0 else DOWN)),
                      (f"  spread {spread:.2f}", DIM),
                      (f"   CVD {st.cvd:+.0f} ", "bold #4cc9f0"),
                      (cvd_sp, "#4cc9f0")),
        style="on #0d1117"))

    with st.lock:
        layout["book"].update(Panel(render_book(st), title="стакан (◆ стена ⛁ айсберг)",
                                    border_style="#2a3542"))
        layout["candles"].update(Panel(render_candles(st), title=f"свечи {st.candle_sec}с",
                                       border_style="#2a3542"))
        sig_tb = Table(box=box.SIMPLE, padding=(0, 1), show_header=False)
        for ts, msg, col in list(st.signals)[:5]:
            sig_tb.add_row(Text(ts.strftime("%H:%M:%S"), style=DIM),
                           Text(msg, style=col))
        layout["signals"].update(Panel(sig_tb or Text("—", style=DIM),
                                       title="сигналы", border_style="#2a3542"))
        layout["tape"].update(Panel(render_tape(st), title="лента", border_style="#2a3542"))
        n_tr = len(st.tape)
        buys = sum(1 for *_, d in st.tape if d > 0)
        stats = Text.assemble(
            (f"сделок {n_tr}\n", ""), (f"buy/sell {buys}/{n_tr - buys}\n", DIM),
            (f"стен: bid {len(st.walls['bid'])} / ask {len(st.walls['ask'])}\n", WALL_B),
            (f"айсбергов: {sum(1 for s in st.walls.values() for w in s.values() if w['iceberg'])}", ICE))
        layout["stats"].update(Panel(stats, title="статистика", border_style="#2a3542"))
    return layout


def live_mode(st, symbol, token):
    sys.path.insert(0, "/home/user/FinamPy")
    from finam_connector import FinamConnector
    fc = FinamConnector(token=token)
    fc.stream_trades(symbol, st.on_trade)
    fc.stream_orderbook(symbol, st.on_book)
    return fc


def replay_mode(st, path, speed=20):
    def run():
        with open(path) as f:
            last_t = None
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if "q" in r:
                    if r["q"] == "ob" and r.get("bid") and r.get("ask"):
                        st.on_book({"bids": [tuple(x) for x in r["bid"]],
                                    "asks": [tuple(x) for x in r["ask"]]})
                        time.sleep(0.05 / speed)
                    continue
                ts = datetime.fromisoformat(r["t"]).timestamp()
                if last_t:
                    dt = (ts - last_t) / speed
                    if dt > 0.005:
                        time.sleep(dt)
                last_t = ts
                st.on_trade({"price": r["p"], "size": r["s"],
                             "side": "buy" if r["d"] > 0 else "sell", "ts": ts})
        # держим картинку
        while True:
            time.sleep(1)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SBER@MISX")
    ap.add_argument("--replay", default=None, help="NDJSON-файл вместо live")
    ap.add_argument("--speed", type=float, default=20, help="ускорение replay")
    ap.add_argument("--candle-sec", type=int, default=60)
    ap.add_argument("--refresh", type=float, default=0.5)
    a = ap.parse_args()

    st = MarketState(a.symbol, a.candle_sec)
    if a.replay:
        replay_mode(st, a.replay, a.speed)
        title = f"REPLAY {os.path.basename(a.replay)} x{a.speed}"
    else:
        token = os.environ.get("FINAM_TOKEN")
        if not token:
            sys.exit("FINAM_TOKEN не задан (или используй --replay)")
        live_mode(st, a.symbol, token)
        title = "LIVE"
    with Live(build_ui(st), refresh_per_second=2, screen=True) as live:
        t0 = time.time()
        try:
            while True:
                time.sleep(a.refresh)
                live.update(build_ui(st))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
