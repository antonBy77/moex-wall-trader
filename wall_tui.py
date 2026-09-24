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
        self.big_threshold = float(os.environ.get("TUI_BIG_MIN", 300))
        self.pnl_hist = deque(maxlen=120)      # (ts, allow_pnl, block_pnl) — график P/L
        self.last_signals = deque(maxlen=8)    # последние сигналы стен для статистики
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
            # снапшот кривой P/L для графика в правой панели
            self.pnl_hist.append((ts_now, self.laya_stats["allow_pnl"],
                                  self.laya_stats["block_pnl"]))

    def _ask_laya(self, sig, side, price):
        """Асинхронный вопрос гейту (если LAYA_URL задан). Вердикт ляжет в laya_verdicts.
        Дедуп: не шлём повторно по той же стене, пока прошлый запрос в полёте."""
        if not self.laya_url:
            return
        key = (side, round(price, 2))
        now = time.time()
        if getattr(self, "_laya_inflight", None) is None:
            self._laya_inflight = {}
        if now - self._laya_inflight.get(key, 0) < 30:
            return  # уже спрашивали недавно
        self._laya_inflight[key] = now
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
                    "questions": {
                        "verdict": {
                            "type": "choice",
                            "instructions": f"Order-flow wall trade gate: should we {action} at wall {price:.2f}? CVD={self.cvd:.0f}, wall_hits={state['wall_hits']}.",
                            "criteria": {
                                "allow": "wall supports the direction, proceed",
                                "block": "adverse-move probability too high, reject",
                                "escalate": "conflicting signals, skip and observe",
                            },
                        },
                    }}).encode()
                req = urllib.request.Request(
                    self.laya_url.rstrip("/") + "/v1/systemone", data=body,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    res = json.loads(r.read())
                # Jev-совместимый ответ: answers.verdict = {choice, probabilities}
                v = res.get("answers", {}).get("verdict", res.get("verdict", res))
                if isinstance(v, dict):
                    raw = str(v.get("choice", v.get("value", ""))).lower()
                    probs = v.get("probabilities", {})
                else:
                    raw, probs = str(v).lower(), {}
                if "allow" in raw:
                    verdict, allowed, col = "ALLOW LONG" if action == "buy" else "ALLOW SHORT", True, "bold " + UP
                elif "escalate" in raw:
                    verdict, allowed, col = "ESCALATE", True, "bold #ffd166"
                else:
                    verdict, allowed, col = "BLOCK", False, "bold " + DOWN
                # вероятности: если есть — берём P(allow)/P(block), иначе 1/0
                if probs:
                    pa = float(probs.get("allow", 0))
                    pb = float(probs.get("block", 0))
                    val = pa / (pa + pb) if pa + pb else 0.5
                else:
                    val = 1.0 if allowed else 0.0
                self.laya_verdicts.appendleft(
                    (datetime.now(), f"{verdict} {action} {price:.2f}",
                     val, col))
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
            bids = ob.get("bid") or ob.get("bids") or []
            asks = ob.get("ask") or ob.get("asks") or []
            self.book = {"bids": bids, "asks": asks}
            # порог стены: TUI_WALL_MODE
            #   fixed — фиксированные TUI_WALL_K / TUI_WALL_MIN (как раньше)
            #   auto  — квантиль распределения уровней стакана (TUI_WALL_Q, 0..1)
            #   ratio — множитель к среднему уровню (TUI_WALL_K трактуется как ×среднее)
            mode = os.environ.get("TUI_WALL_MODE", "fixed")
            K = float(os.environ.get("TUI_WALL_K", 2.0))
            MIN = float(os.environ.get("TUI_WALL_MIN", 1500))
            Q = float(os.environ.get("TUI_WALL_Q", 0.97))
            for side, levels in (("bid", bids), ("ask", asks)):
                tops = levels[:10]
                if len(tops) < 3:
                    continue
                sizes = [s for _, s in tops]
                if mode == "auto":
                    # квантиль по текущим уровням + абсолютный минимум MIN
                    import statistics
                    try:
                        thr = max(statistics.quantiles(sizes, n=100)[int(Q * 100) - 1], MIN)
                    except Exception:
                        thr = MIN
                    need = lambda s, avg: s >= thr
                elif mode == "ratio":
                    # множитель к СРЕДНЕМУ уровню (не к соседям) + минимум
                    avg_all = sum(sizes) / len(sizes)
                    need = lambda s, avg: s >= max(K * avg_all, MIN)
                else:  # fixed
                    need = lambda s, avg: s >= max(K * avg, MIN)
                for p, s in tops:
                    others = [x[1] for x in tops if x[0] != p][:6]
                    avg = sum(others) / len(others) if others else 0
                    if avg and need(s, avg):
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


def render_tape(st, height=16, big_only=False):
    tb = Table(box=box.SIMPLE, show_header=True, header_style="bold dim",
               padding=(0, 0), expand=True)
    tb.add_column("время", style=DIM, width=8)
    tb.add_column("цена", justify="right", width=7)
    tb.add_column("объём", justify="right", width=7)
    tb.add_column("поток", width=20)
    tail = list(st.tape_view)[-height * 4:]
    if big_only:
        tail = [r for r in tail if r[2] >= st.big_threshold]
    rows = tail[-height:][::-1]
    if not rows:
        return Text("нет принтов ≥ фильтра", style=DIM)
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
    grid: list = [[" "] * len(cs) for _ in range(rows_n)]
    colors: list = [[None] * len(cs) for _ in range(rows_n)]
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
    # метка последней цены справа
    last_p = cs[-1][4]
    if lo <= last_p <= hi:
        yy = y(last_p)
        grid[yy][len(cs) - 1] = "◄"
        colors[yy][len(cs) - 1] = "bold white"
        t_lbl = f"{last_p:.2f}"
        for x in range(max(0, len(cs) - 7), len(cs) - 1):
            if grid[yy][x] in (" ",):
                grid[yy][x] = t_lbl[x - (len(cs) - 7)] if x - (len(cs) - 7) < len(t_lbl) else " "
                colors[yy][x] = "bold white"
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
    """Кластеры объёма: бары по ценовым уровням из ленты.
    window_hours=None = весь день; иначе окно в часах.
    Диапазон цен — весь (min..max ленты), уровни агрегируются в rows_n корзин."""
    if len(st.tape) < 10:
        return Text("ожидание данных…", style=DIM)
    tape = list(st.tape)
    if window_hours:
        t_last = tape[-1][0]
        cut = t_last.timestamp() - window_hours * 3600
        tape = [r for r in tape if r[0].timestamp() >= cut] or tape[-500:]
    if not tape:
        return Text("…", style=DIM)
    lo, hi = min(p for _, p, _, _ in tape), max(p for _, p, _, _ in tape)
    lo = round(round(lo / st.tick) * st.tick, 4)
    hi = round(round(hi / st.tick) * st.tick, 4)
    n_bins = max(1, int(round((hi - lo) / st.tick)) + 1)
    # агрегация в корзины по rows_n
    bin_sz = max(st.tick, (hi - lo) / (rows_n - 1) if rows_n > 1 else st.tick)
    clusters = {}   # bin_price -> [buy, sell]
    for _, p, s, d in tape:
        k = round(lo + round((p - lo) / bin_sz) * bin_sz, 4)
        c = clusters.setdefault(k, [0.0, 0.0])
        c[0 if d > 0 else 1] += s
    mx = max(b + sl for b, sl in clusters.values()) or 1.0
    poc = max(clusters, key=lambda k: sum(clusters[k]))
    mx_w = width - 14
    t = Text()
    for p in sorted(clusters, reverse=True):
        b, sl = clusters[p]
        tot = b + sl
        nb = int(b / mx * mx_w)
        ns = int(sl / mx * mx_w)
        is_poc = p == poc
        col_b = WALL if is_poc else UP
        col_s = WALL if is_poc else DOWN
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


def render_pnl_chart(st, width=44, rows_n=10):
    """Кривая P/L: allow (зелёная) и block (красная) — в правой пустой зоне."""
    if len(st.pnl_hist) < 2 and not (st.laya_stats["allow_n"] or st.laya_stats["block_n"]):
        return Text("P/L появится после закрытия\nпервых Laya-сделок…", style=DIM)
    hist = list(st.pnl_hist)
    if len(hist) < 2:
        # ещё мало точек — покажем цифры крупно
        s = st.laya_stats
        t = Text()
        t.append(f"  ALLOW {s['allow_pnl']:+.2f}₽  ({s['allow_n']})\n",
                 style="bold " + (UP if s['allow_pnl'] >= 0 else DOWN))
        t.append(f"  BLOCK {s['block_pnl']:+.2f}₽  ({s['block_n']})",
                 style="bold " + (DOWN if s['block_pnl'] < 0 else UP))
        return t
    # два ряда значений, общий масштаб
    a_vals = [a for _, a, _ in hist][-width:]
    b_vals = [b for _, _, b in hist][-width:]
    allv = a_vals + b_vals
    hi, lo = max(allv), min(allv)
    rng = (hi - lo) or 1
    # сетка rows_n x width: рисуем обе линии точками (b поверх a)
    grid: list = [[" "] * len(a_vals) for _ in range(rows_n)]
    colors: list = [[None] * len(a_vals) for _ in range(rows_n)]
    y = lambda v: min(rows_n - 1, int((hi - v) / rng * (rows_n - 1)))
    for x, v in enumerate(a_vals):
        grid[y(v)][x] = "●"; colors[y(v)][x] = UP
    for x, v in enumerate(b_vals):
        grid[y(v)][x] = "○"; colors[y(v)][x] = DOWN
    t = Text()
    for r in range(rows_n):
        for c in range(len(a_vals)):
            t.append(grid[r][c], style=colors[r][c] or "#1b242e")
        t.append("\n")
    t.append(f"hi {hi:+.2f}  lo {lo:+.2f}  ", style=DIM)
    t.append("●ALLOW ", UP); t.append("○BLOCK", DOWN)
    return t


def render_stats(st):
    """Богатая статистика: потоки, скорость, стены, сигналы, рекорды дня."""
    tape = list(st.tape_view)
    n = len(tape)
    buys = sum(1 for *_, d in tape if d > 0)
    vol_buy = sum(s for _, _, s, d in tape if d > 0)
    vol_sell = sum(s for _, _, s, d in tape if d < 0)
    # скорость: принтов за последнюю минуту
    now = datetime.now()
    per_min = sum(1 for ts, *_ in tape if (now - ts).total_seconds() <= 60)
    max_buy = max((s for _, _, s, d in tape if d > 0), default=0)
    max_sell = max((s for _, _, s, d in tape if d < 0), default=0)
    ice_n = sum(1 for s in st.walls.values() for w in s.values() if w["iceberg"])
    t = Text()
    t.append(f"сделок {n} ({per_min}/мин)\n", TXT)
    t.append(Text.assemble(("▲", UP), (f"{buys} ", TXT), ("▼", DOWN),
                           (f"{n - buys}  ", TXT),
                           (f"V {vol_buy + vol_sell:.0f}\n", DIM)))
    t.append(Text.assemble((f"Vbuy {vol_buy:.0f} ", UP), (f"Vsell {vol_sell:.0f}\n", DOWN)))
    t.append(Text.assemble(("max⚡ ", "#ffd166"), (f"buy {max_buy:.0f} ", UP),
                           (f"sell {max_sell:.0f}\n", DOWN)))
    t.append(Text.assemble((f"стен bid {len(st.walls['bid'])} / ask {len(st.walls['ask'])}  ",
                            WALL), (f"айсбергов {ice_n}\n", ICE)))
    t.append(Text.assemble(("порог⚡ ", DIM), (f"≥{st.big_threshold:.0f} ", "#ffd166"),
                           (f"(TUI_BIG_MIN)\n", DIM)))
    # последние сигналы стен
    sigs = list(st.signals)[:3]
    for ts, msg, col, _w in sigs:
        t.append(Text.assemble((ts.strftime("%H:%M:%S ") , DIM), (msg + "\n", col)))
    return t


def build_ui(st, W=150, H=42, tape_big_only=False):
    layout = Layout()
    layout.split_column(Layout(name="top", size=3), Layout(name="body"))
    layout["body"].split_row(Layout(name="left", ratio=2), Layout(name="right", ratio=3))
    layout["left"].split_column(Layout(name="book", ratio=3),
                                Layout(name="midrow_l", size=9),
                                Layout(name="laya", size=9))
    layout["midrow_l"].split_row(Layout(name="big"), Layout(name="signals", ratio=1))
    layout["right"].split_column(
        Layout(name="toprow", ratio=1), Layout(name="midrow", ratio=1),
        Layout(name="bottom", ratio=1))
    layout["toprow"].split_row(Layout(name="cand"), Layout(name="pnl", ratio=1))
    layout["midrow"].split_row(Layout(name="mid"), Layout(name="mid2", ratio=1))
    layout["bottom"].split_row(Layout(name="tape"), Layout(name="stats", size=34))

    with st.lock:
        last = st.tape[-1] if st.tape else None
        bids, asks = st.book["bids"], st.book["asks"]
        imb = 0
        if bids and asks:
            b3 = sum(s for _, s in bids[:3]); a3 = sum(s for _, s in asks[:3])
            imb = (b3 - a3) / (b3 + a3) if b3 + a3 else 0
        imb_col = UP if imb > 0.15 else (DOWN if imb < -0.15 else DIM)
        spr = (asks[0][0] - bids[0][0]) if bids and asks else 0.0
        top = Text.assemble(
            (f" {st.symbol}  ", "bold"),
            (f"{last[1]:.2f}" if last else "—", "bold " + (UP if last and last[3] > 0 else DOWN)),
            (f"   imb(3) {imb:+.2f} ", imb_col),
            (f"   CVD {st.cvd:+.0f} ", "bold " + (UP if st.cvd >= 0 else DOWN)),
            (f"   спред {spr:.2f} " if bids and asks else "", DIM),
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
                                   title="[dim]кластеры (день) · █buy ▓sell · ◄POC[/dim]",
                                   border_style="#2a3542"))
        layout["mid2"].update(Panel(render_clusters(st, window_hours=1),
                                    title="[dim]кластеры (1ч)[/dim]",
                                    border_style="#2a3542"))
        layout["tape"].update(Panel(render_tape(st, big_only=tape_big_only),
                                    title=("[dim]лента · КРУПНЫЕ[/dim]" if tape_big_only
                                           else "[dim]лента[/dim]"),
                                    border_style="#2a3542"))

        # крупные принты (слева) + последние сигналы стен (справа от них)
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
        sg = Table(box=box.SIMPLE, show_header=False, padding=(0, 0))
        sg.add_column(width=8); sg.add_column(width=22)
        for ts, msg, col, _w in list(st.signals)[:4]:
            sg.add_row(Text(ts.strftime("%H:%M:%S"), style=DIM),
                       Text(msg, style=col))
        if not st.signals:
            sg.add_row(Text("—", style=DIM), Text("ждём касаний…", style=DIM))
        layout["signals"].update(Panel(sg, title="[dim]сигналы стен[/dim]",
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
        # правая верхняя пустая зона: кривая P/L
        layout["pnl"].update(Panel(render_pnl_chart(st),
                                   title="[dim]кривая P/L · ●allow ○block[/dim]",
                                   border_style="#2a3542"))

        # богатая статистика
        layout["stats"].update(Panel(render_stats(st),
                                     title="[dim]статистика[/dim]",
                                     border_style="#2a3542"))
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
    ap.add_argument("--tape-big", action="store_true",
                    help="лента показывает только принты >= TUI_BIG_MIN")
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
        with Live(build_ui(st, tape_big_only=a.tape_big), refresh_per_second=2.5, screen=True) as live:
            while True:
                time.sleep(a.refresh)
                live.update(build_ui(st, tape_big_only=a.tape_big))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
