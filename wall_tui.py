"""wall-tui v2 — плотный терминальный дашборд: стакан с барами по каждому уровню,
лента с историей, тейп-давление, кластеры объёма, heatmap потока.

Данные: live (gRPC-стримы) или replay NDJSON.
Запуск:
    FINAM_TOKEN=... python3 wall_tui.py --symbol SBER@MISX
    python3 wall_tui.py --replay data/SBER-MISX-2026-09-24.ndjson --speed 30
Клавиши: q/Ctrl+C — выход.
"""
import sys, os, time, json, argparse, threading
from datetime import datetime
from collections import deque

sys.path.insert(0, "/home/user/.hermes/scripts-dev/finam-connector")

from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich import box

UP, DOWN, WALL, ICE = "#2dd4a3", "#ff6b5e", "#e9c46a", "#b388eb"
BLUE, DIM, TXT = "#4cc9f0", "#5a6a7a", "#e6edf3"

BLOCKS = " ▁▂▃▄▅▆▇█"


class MarketState:
    def __init__(self, symbol="SBER@MISX", candle_sec=30):
        self.symbol = symbol
        self.candle_sec = candle_sec
        self.tick = 0.01
        self.tape = deque(maxlen=60000)        # (ts, p, s, d) — день SBER ~50-80К
        self.tape_view = deque(maxlen=120)     # короткий хвост для панели «лента»
        self.book = {"bids": [], "asks": []}
        self.candles = deque(maxlen=70)        # (bucket, o,h,l,c,vol, buyvol)
        self._cur = None
        self.walls = {"bid": {}, "ask": {}}
        self.signals = deque(maxlen=10)
        self.cvd = 0.0
        self.cvd_hist = deque(maxlen=60)
        self.big_prints = deque(maxlen=12)     # принты >= big_threshold
        self.big_threshold = 300
        self.lock = threading.Lock()
        self.laya_verdicts = deque(maxlen=10)   # (ts, verdict, val, style)
        self.laya_url = os.environ.get("LAYA_URL")
        self.laya_stats = {"allow_pnl": 0.0, "allow_n": 0, "allow_win": 0,
                           "block_pnl": 0.0, "block_n": 0, "block_win": 0}
        self.laya_pending = []   # [(open_ts, action, price)] — ждут исхода 5п тейк/стоп
        self.laya_trade = 0.05   # тейк/стоп 5 тиков, как в бэктесте
        self._laya_thread = None

    def _settle_laya(self, ts_now, price):
        """Проверяет открытые Laya-входы: тейк +5 тиков / стоп -5 тиков."""
        for item in list(self.laya_pending):
            ts0, action, entry, allowed = item
            dt = (ts_now - ts0).total_seconds()
            dirn = 1 if action == "buy" else -1
            hit_tp = (price >= entry + self.laya_trade) if dirn > 0 else (price <= entry - self.laya_trade)
            hit_sl = (price <= entry - self.laya_trade) if dirn > 0 else (price >= entry + self.laya_trade)
            timeout = dt > 600
            if not (hit_tp or hit_sl or timeout):
                continue
            pnl = ((price - entry) * dirn) if timeout else \
                  (self.laya_trade if hit_tp else -self.laya_trade)
            result = "tp" if hit_tp else ("sl" if hit_sl else "time")
            key = "allow" if allowed else "block"
            self.laya_stats[key + "_pnl"] += pnl
            self.laya_stats[key + "_n"] += 1
            if pnl > 0:
                self.laya_stats[key + "_win"] += 1
            self.laya_pending.remove(item)

    def _ask_laya(self, sig, side, price):
        """Асинхронный вопрос гейту (если LAYA_URL задан). Вердикт ляжет в laya_verdicts."""
        if not self.laya_url:
            return
        action = "buy" if "BUY" in sig[1] else "sell"
        state = {
            "symbol": self.symbol,
            "signal": sig[1],
            "wall_side": side, "wall_price": price,
            "wall_size": sig[3].get("size", 0),
            "wall_hits": sig[3].get("hits", 0),
            "cvd": self.cvd,
        }

        def run():
            try:
                import urllib.request
                body = json.dumps({
                    "state": {"document": json.dumps(state)},
                    "questions": {"trade": {
                        "type": "rating",
                        "instructions": f"Should we {action} at wall {price}?"}}}).encode()
                req = urllib.request.Request(
                    self.laya_url.rstrip("/") + "/v1/systemone", data=body,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    res = json.loads(r.read())
                # Jev-совместимый ответ: rating/trade -> value
                v = res.get("trade", res)
                val = float(v.get("value", 0.5))
                verdict = "ALLOW" if val >= 0.5 else "BLOCK"
                allowed = val >= 0.5
                self.laya_verdicts.appendleft(
                    (datetime.now(), f"{verdict} {action} {price:.2f}",
                     val, "bold " + (UP if allowed else DOWN)))
                self.laya_pending.append((datetime.now(), action, price, allowed))
            except Exception:
                self.laya_verdicts.appendleft(
                    (datetime.now(), f"Laya offline ({action} {price:.2f})", -1, DIM))

        threading.Thread(target=run, daemon=True).start()

    # ---------- поток данных ----------
    def on_trade(self, t):
        with self.lock:
            ts = datetime.fromtimestamp(t["ts"]) if isinstance(t.get("ts"), (int, float)) \
                else datetime.now()
            p, s = t["price"], t["size"]
            d = 1 if t["side"] == "buy" else -1
            self.tape.append((ts, p, s, d))
            self.tape_view.append((ts, p, s, d))
            if self.laya_url and self.laya_pending:
                self._settle_laya(ts, p)
            self.cvd += s * d
            self.cvd_hist.append((ts, self.cvd))
            if s >= self.big_threshold:
                self.big_prints.append((ts, p, s, d))
            bucket = int(ts.timestamp() // self.candle_sec)
            if self._cur is None or self._cur[0] != bucket:
                if self._cur:
                    self.candles.append(self._cur)
                self._cur = [bucket, p, p, p, p, 0.0, 0.0]
            c = self._cur
            c[2] = max(c[2], p); c[3] = min(c[3], p); c[4] = p
            c[5] += s
            c[6] += s if d > 0 else 0
            # касание/съедание стен
            for side in ("bid", "ask"):
                for pp in list(self.walls[side]):
                    w = self.walls[side][pp]
                    if abs(p - pp) <= 2 * self.tick and \
                       ((side == "bid" and d == -1) or (side == "ask" and d == 1)):
                        w["hits"] += 1
                        if w["hits"] == 1:
                            sig = (ts, f"REJECTION {'BUY' if side=='bid' else 'SELL'} @ {pp:.2f}",
                                   WALL, w)
                            self.signals.appendleft(sig)
                            self._ask_laya(sig, side, pp)
                    if (side == "bid" and d == -1 and p <= pp) or \
                       (side == "ask" and d == 1 and p >= pp):
                        w["size"] = max(0, w["size"] - s)
                        w["min_seen"] = min(w["min_seen"], w["size"])
                        if w["size"] < w["size0"] * 0.3 and w["hits"] >= 3:
                            sig = (ts, f"BREAKOUT {'SELL' if side=='bid' else 'BUY'} @ {pp:.2f}",
                                   DOWN, w)
                            self.signals.appendleft(sig)
                            self._ask_laya(sig, side, pp)
                            del self.walls[side][pp]
                        elif w["size"] <= 0:
                            del self.walls[side][pp]

    def on_book(self, ob):
        with self.lock:
            self.book = {"bids": ob["bids"], "asks": ob["asks"]}
            K = float(os.environ.get("TUI_WALL_K", 2.0))
            MIN = float(os.environ.get("TUI_WALL_MIN", 1500))
            for side, levels in (("bid", ob["bids"]), ("ask", ob["asks"])):
                tops = levels[:10]
                if len(tops) < 3:
                    continue
                for p, s in tops:
                    others = [x[1] for x in tops if x[0] != p][:6]
                    avg = sum(others) / len(others) if others else 0
                    if avg and s >= max(K * avg, MIN):
                        w = self.walls[side].get(p)
                        if w:
                            if w["hits"] >= 1 and s >= w["size0"] * 0.9 \
                                    and w["min_seen"] < w["size0"] * 0.7:
                                w["iceberg"] = True
                            w["size"] = s
                            w["min_seen"] = min(w["min_seen"], s)
                            w["last"] = time.time()
                        else:
                            self.walls[side][p] = {"size": s, "size0": s, "min_seen": s,
                                                   "hits": 0, "iceberg": False,
                                                   "last": time.time()}
                prices = {p for p, _ in levels[:12]}
                for p in list(self.walls[side]):
                    if p not in prices and time.time() - self.walls[side][p].get("last", 0) > 30:
                        del self.walls[side][p]


# ================= рендеры =================

def bar(sz, mx, width=9, col=DIM):
    n = int(sz / mx * width) if mx else 0
    return Text("█" * n, style=col) if n else Text("·", style="#24303c")


def render_book(st, height=14):
    tb = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold dim",
               padding=(0, 0), expand=True)
    tb.add_column("bid", justify="right", width=9)
    tb.add_column("b-бар", width=9)
    tb.add_column("цена", justify="center", width=9)
    tb.add_column("a-бар", width=9)
    tb.add_column("ask", justify="left", width=9)
    bids, asks = st.book["bids"], st.book["asks"]
    mx = max(max((s for _, s in bids[:height]), default=1),
             max((s for _, s in asks[:height]), default=1))
    n = min(height, max(len(bids), len(asks), 1))
    for i in range(n - 1, -1, -1):
        ap, asz = asks[i] if i < len(asks) else (None, None)
        bp, bsz = bids[i] if i < len(bids) else (None, None)
        wa, wb = st.walls["ask"].get(ap), st.walls["bid"].get(bp)
        ptxt = Text()
        if ap is not None:
            ptxt.append(f"{ap:.2f}", style=WALL if wa else TXT)
        if ap is not None and bp is not None:
            ptxt.append(" ")
        if bp is not None:
            ptxt.append(f"{bp:.2f}", style=WALL if wb else TXT)
        a_col = ICE if wa and wa["iceberg"] else (WALL if wa else DOWN)
        b_col = ICE if wb and wb["iceberg"] else (WALL if wb else UP)
        mark_a = "⛁" if wa and wa["iceberg"] else ("◆" if wa else "")
        mark_b = "⛁" if wb and wb["iceberg"] else ("◆" if wb else "")
        tb.add_row(
            Text(f"{bsz:.0f}", style=b_col) if bsz is not None else Text(""),
            bar(bsz or 0, mx, col=b_col) if bsz is not None else Text(""),
            ptxt,
            bar(asz or 0, mx, col=a_col) if asz is not None else Text(""),
            Text.assemble((f"{asz:.0f}", a_col), (" " + mark_a, a_col))
            if asz is not None else Text(""),
        )
    mid = (bids[0][0] + asks[0][0]) / 2 if bids and asks else 0
    spr = asks[0][0] - bids[0][0] if bids and asks else 0
    tb.add_row(Text(f"mid {mid:.2f}  spr {spr:.2f}", style=DIM))
    return tb


def render_tape(st, height=16):
    tb = Table(box=box.SIMPLE, show_header=True, header_style="bold dim",
               padding=(0, 0), expand=True)
    tb.add_column("время", style=DIM, width=8)
    tb.add_column("цена", justify="right", width=7)
    tb.add_column("объём", justify="right", width=7)
    tb.add_column("поток", width=20)
    rows = list(st.tape_view)[-height:][::-1]
    mx = max((s for _, _, s, _ in rows), default=1)
    for ts, p, s, d in rows:
        col = UP if d > 0 else DOWN
        n = max(1, int(s / mx * 16))
        flow = Text(("◀" if d < 0 else "▶") + ("█" * n), style=col)
        big = " ⚡" if s >= st.big_threshold else ""
        tb.add_row(ts.strftime("%H:%M:%S"),
                   Text(f"{p:.2f}", style="bold " + col),
                   Text(f"{s:.0f}", style=col),
                   Text.assemble(flow, (big, "bold #ffd166")))
    return tb


def render_candles(st, width=46, rows_n=13):
    cs = list(st.candles)
    if st._cur:
        cs.append(st._cur)
    if not cs:
        return Text("ожидание данных…", style=DIM)
    cs = cs[-width:]
    hi = max(c[2] for c in cs); lo = min(c[3] for c in cs)
    rng = (hi - lo) or 1
    grid = [[" "] * len(cs) for _ in range(rows_n)]
    colors = [[None] * len(cs) for _ in range(rows_n)]
    y = lambda p: min(rows_n - 1, int((hi - p) / rng * (rows_n - 1)))
    for x, c in enumerate(cs):
        o, h, l, cl = c[1], c[2], c[3], c[4]
        col = UP if cl >= o else DOWN
        for yy in range(y(h), y(l) + 1):
            grid[yy][x] = "│"; colors[yy][x] = "#31404e"
        top, bot = y(max(o, cl)), y(min(o, cl))
        for yy in range(top, bot + 1):
            grid[yy][x] = "█" if bot > top else "▄"
            colors[yy][x] = col
    for side in ("bid", "ask"):
        for p, w in st.walls[side].items():
            if lo <= p <= hi:
                yy = y(p)
                mk = "⛁" if w["iceberg"] else "─"
                cc = ICE if w["iceberg"] else WALL
                for x in range(len(cs)):
                    if colors[yy][x] in (None, "#31404e"):
                        grid[yy][x] = mk; colors[yy][x] = cc
    t = Text()
    for row_c, row_cl in zip(grid, colors):
        for ch, cc in zip(row_c, row_cl):
            t.append(ch, style=cc or "#1b242e")
        t.append("\n")
    walls_near = [(p, w) for side in ("bid", "ask")
                  for p, w in sorted(st.walls[side].items()) if lo <= p <= hi]
    for p, w in walls_near[:4]:
        ice = " ⛁АЙСБЕРГ" if w["iceberg"] else ""
        side = "bid" if any(p == x for x in st.walls["bid"]) else "ask"
        col = ICE if w["iceberg"] else WALL
        t.append(Text(f"\n{'▲' if side=='bid' else '▼'} {p:.2f} {w['size']:.0f} "
                      f"х{w['hits']}{ice}", style=col))
    return t


def render_clusters(st, rows_n=15, width=50, window_hours=None):
    """Кластеры объёма: бары по ценовым уровням из ленты. window_hours=None = весь день."""
    if len(st.tape) < 10:
        return Text("ожидание данных…", style=DIM)
    tape = list(st.tape)
    if window_hours:
        t_last = tape[-1][0]
        cut = t_last.timestamp() - window_hours * 3600
        tape = [r for r in tape if r[0].timestamp() >= cut] or tape[-500:]
    # группировка по тикам
    clusters = {}   # price -> [buy, sell]
    for _, p, s, d in tape:
        key = round(p / st.tick) * st.tick
        c = clusters.setdefault(key, [0.0, 0.0])
        c[0 if d > 0 else 1] += s
    if not clusters:
        return Text("…", style=DIM)
    mx = max(b + sl for b, sl in clusters.values())
    poc = max(clusters, key=lambda k: sum(clusters[k]))
    lo, hi = min(clusters), max(clusters)
    # один ряд на тик (если уровней много — шагаем)
    step = max(1, int((hi - lo) / st.tick / rows_n) + 1)
    prices = sorted(clusters, reverse=True)
    shown = prices[::step][:rows_n]
    mx_w = width - 14
    t = Text()
    for p in shown:
        b, sl = clusters[p]
        tot = b + sl
        nb = int(b / mx * mx_w)
        ns = int(sl / mx * mx_w)
        is_poc = p == poc
        col_b = WALL if is_poc else UP
        col_s = WALL if is_poc else DOWN
        pct = tot / mx
        t.append(Text(f"{p:8.2f} ", style=WALL if is_poc else DIM))
        t.append(Text("█" * nb, style=col_b))
        t.append(Text("▓" * ns, style=col_s))
        if is_poc:
            t.append(Text(" ◄POC", style=WALL))
        t.append(Text(f" {tot:>7.0f}\n", style=DIM))
    return t


def render_cvd(st, width=46, rows_n=5):
    if len(st.cvd_hist) < 3:
        return Text("…", style=DIM)
    vals = [v for _, v in list(st.cvd_hist)[-width:]]
    hi, lo = max(vals), min(vals)
    rng = (hi - lo) or 1
    t = Text()
    for v in vals:
        n = int((v - lo) / rng * (rows_n * len(BLOCKS) - 1)) + 1
        idx = min(n, len(BLOCKS) - 1)
        t.append(BLOCKS[idx], style=BLUE)
    zero_y = int((0 - lo) / rng * (rows_n - 1)) if lo < 0 < hi else None
    return t


def build_ui(st, W=150, H=42):
    layout = Layout()
    layout.split_column(Layout(name="top", size=3), Layout(name="body"))
    layout["body"].split_row(Layout(name="left", ratio=2), Layout(name="right", ratio=3))
    layout["left"].split_column(Layout(name="book", ratio=3), Layout(name="big", size=9),
                                Layout(name="laya", size=9))
    layout["right"].split_column(Layout(name="cand"), Layout(name="mid", ratio=1),
                                 Layout(name="bottom", ratio=1))
    layout["bottom"].split_row(Layout(name="tape"), Layout(name="stats", size=30))

    with st.lock:
        last = st.tape[-1] if st.tape else None
        bids, asks = st.book["bids"], st.book["asks"]
        imb = 0
        if bids and asks:
            b3 = sum(s for _, s in bids[:3]); a3 = sum(s for _, s in asks[:3])
            imb = (b3 - a3) / (b3 + a3) if b3 + a3 else 0
        imb_col = UP if imb > 0.15 else (DOWN if imb < -0.15 else DIM)
        top = Text.assemble(
            (f" {st.symbol}  ", "bold"),
            (f"{last[1]:.2f}" if last else "—", "bold " + (UP if last and last[3] > 0 else DOWN)),
            (f"   imb(3) {imb:+.2f} ", imb_col),
            (f"   CVD {st.cvd:+.0f} ", "bold " + (UP if st.cvd >= 0 else DOWN)),
            (" ", ""),
        )
        # спарклайн CVD отдельной строкой нельзя — рисуем рядом
        layout["top"].update(Panel(top, style="on #0d1117"))

        layout["book"].update(Panel(render_book(st),
                                    title="[dim]стакан · ◆стена ⛁айсберг[/dim]",
                                    border_style="#2a3542"))
        layout["cand"].update(Panel(render_candles(st),
                                    title=f"[dim]свечи {st.candle_sec}с · стены/айсберги[/dim]",
                                    border_style="#2a3542"))
        layout["mid"].update(Panel(render_clusters(st, window_hours=None),
                                   title="[dim]кластеры объёма (день) · █buy ▓sell · ◄POC[/dim]",
                                   border_style="#2a3542"))
        layout["tape"].update(Panel(render_tape(st), title="[dim]лента[/dim]",
                                    border_style="#2a3542"))

        # крупные принты
        bt = Table(box=box.SIMPLE, show_header=False, padding=(0, 0))
        bt.add_column(width=8); bt.add_column(width=7); bt.add_column(width=8)
        for ts, p, s, d in list(st.big_prints)[-4:][::-1]:
            col = UP if d > 0 else DOWN
            bt.add_row(Text(ts.strftime("%H:%M:%S"), style=DIM),
                       Text(f"{p:.2f}", style=col),
                       Text(f"{s:.0f}⚡", style="bold " + col))
        layout["big"].update(Panel(bt or Text("—", style=DIM),
                                   title=f"[dim]крупные ≥{st.big_threshold:.0f}[/dim]",
                                   border_style="#2a3542"))

        # Laya-вердикты + P/L A/B
        lt = Table(box=box.SIMPLE, show_header=False, padding=(0, 0))
        lt.add_column(width=8); lt.add_column(width=22)
        for ts, msg, val, col in list(st.laya_verdicts)[:3]:
            vt = f"{val:.2f}" if val >= 0 else "—"
            lt.add_row(Text(ts.strftime("%H:%M:%S"), style=DIM),
                       Text.assemble((f"{msg}", col), (f" {vt}", DIM)))
        if not st.laya_verdicts:
            lt.add_row(Text("—", style=DIM),
                       Text("LAYA_URL не задан" if not st.laya_url else "ждём сигналы…",
                            style=DIM))
        s = st.laya_stats
        if s["allow_n"] or s["block_n"]:
            a_pnl, b_pnl = s["allow_pnl"], s["block_pnl"]
            lt.add_row(Text("P/L:", style="bold"))
            lt.add_row(Text.assemble(
                ("ALLOW ", UP), (f"{a_pnl:+.2f}₽ ", "bold " + (UP if a_pnl >= 0 else DOWN)),
                (f"({s['allow_n']} / win {s['allow_win']}/{s['allow_n']})" if s['allow_n'] else "(0)", DIM)))
            lt.add_row(Text.assemble(
                ("BLOCK ", DOWN), (f"{b_pnl:+.2f}₽ ", "bold " + (UP if b_pnl >= 0 else DOWN)),
                (f"({s['block_n']} / win {s['block_win']}/{s['block_n']})" if s['block_n'] else "(0)", DIM)))
            edge = a_pnl - b_pnl
            lt.add_row(Text.assemble(
                ("гейт ", DIM),
                (f"{'+' if edge >= 0 else ''}{edge:.2f}₽", "bold " + (UP if edge >= 0 else DOWN)),
                (" (allow-block)", DIM)))
        layout["laya"].update(Panel(lt, title="[dim]Laya-гейт · P/L allow vs block[/dim]",
                                    border_style="#2a3542"))

        # статистика: cvd-спарклайн, дисбаланс по 10с-корзинам, сигналы счётом
        n = len(st.tape_view)
        buys = sum(1 for *_, d in st.tape_view if d > 0)
        stat = Text.assemble(
            (f"сделок {n}\n", TXT), (f"▲{buys} ▼{n - buys}\n", DIM),
            (f"стен bid {len(st.walls['bid'])} / ask {len(st.walls['ask'])}\n", WALL),
            (f"айсбергов {sum(1 for s in st.walls.values() for w in s.values() if w['iceberg'])}\n", ICE))
        sigs = list(st.signals)[:6]
        sig_tb = Table(box=box.SIMPLE, show_header=False, padding=(0, 0))
        sig_tb.add_column(width=8); sig_tb.add_column(width=24)
        for ts, msg, col, w in sigs:
            sig_tb.add_row(Text(ts.strftime("%H:%M:%S"), style=DIM),
                           Text(msg, style=col))
        stat.append("\n")
        layout["stats"].update(Panel(Text.assemble(stat),
                                     title="[dim]статистика[/dim]",
                                     border_style="#2a3542"))
        # сигналы кладём в ту же колонку: переразметим bottom
    return layout


def live_mode(st, symbol, token):
    from finam_connector import FinamConnector
    fc = FinamConnector(token=token)
    fc.stream_trades(symbol, st.on_trade)
    fc.stream_orderbook(symbol, st.on_book)
    return fc


def replay_mode(st, path, speed=30):
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
                    continue
                try:
                    ts = datetime.fromisoformat(r["t"]).timestamp()
                except Exception:
                    continue
                if last_t:
                    dt = (ts - last_t) / speed
                    if dt > 0.004:
                        time.sleep(dt)
                last_t = ts
                st.on_trade({"price": r["p"], "size": r["s"],
                             "side": "buy" if r["d"] > 0 else "sell", "ts": ts})
        while True:
            time.sleep(1)
    threading.Thread(target=run, daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SBER@MISX")
    ap.add_argument("--replay", default=None)
    ap.add_argument("--speed", type=float, default=30)
    ap.add_argument("--candle-sec", type=int, default=30)
    ap.add_argument("--refresh", type=float, default=0.4)
    a = ap.parse_args()
    st = MarketState(a.symbol, a.candle_sec)
    if a.replay:
        replay_mode(st, a.replay, a.speed)
    else:
        token = os.environ.get("FINAM_TOKEN")
        if not token:
            sys.exit("FINAM_TOKEN не задан (или --replay файл)")
        live_mode(st, a.symbol, token)
    try:
        with Live(build_ui(st), refresh_per_second=2.5, screen=True) as live:
            while True:
                time.sleep(a.refresh)
                live.update(build_ui(st))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
