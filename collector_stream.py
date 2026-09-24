"""Коллектор v2 на gRPC-стриминге: полная лента + стакан, NDJSON того же формата.

Стримы не тратят rate limit. Фолбэк: если стрим умер и не поднялся 3 раза
подряд за минуту — переключаемся на поллинг 0.35с (как v1).

Запуск:
    FINAM_TOKEN=... python3 collector_stream.py --symbols SBER@MISX --hours 10
Выход: data/SBER-MISX-<date>.ndjson
Формат строк:
    сделка:  {"p":..,"s":..,"d":+1|-1,"t":"ISO"}
    стакан:  {"t":"ISO","q":"ob","bid":[[p,s]..],"ask":[[p,s]..]}
    пропуск: {"t":"ISO","q":"gap","lost":N}   (v1-совместимо; в v2 редкость)
"""
import sys, os, time, json, threading, argparse
from datetime import datetime, timedelta

sys.path.insert(0, "/home/user/FinamPy")
from FinamPy import FinamPy
from FinamPy.grpc.marketdata_service_pb2 import (
    SubscribeLatestTradesRequest, SubscribeOrderBookRequest, OrderBookRequest)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def iso_now():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class StreamCollector:
    def __init__(self, token, symbols, outdir="data"):
        self.fp = FinamPy(token)
        self.symbols = symbols
        self.outdir = outdir
        os.makedirs(outdir, exist_ok=True)
        self.lock = threading.Lock()
        self.counts = {s: {"trades": 0, "ob": 0, "gap": 0} for s in symbols}
        self.stream_failures = {s: 0 for s in symbols}
        self.fallback_mode = {s: False for s in symbols}
        self._files = {}
        self.running = True

    def _file(self, symbol):
        """Файловый хэндл с дневной ротацией."""
        key = symbol
        today = datetime.now().strftime("%Y-%m-%d")
        fname = os.path.join(self.outdir,
                             f"{symbol.replace('@', '-')}-{today}.ndjson")
        if key not in self._files or self._files[key][1] != today:
            if key in self._files:
                try:
                    self._files[key][0].close()
                except Exception:
                    pass
            self._files[key] = (open(fname, "a", buffering=1), today)
        return self._files[key][0]

    def write(self, symbol, obj):
        with self.lock:
            self._file(symbol).write(json.dumps(obj, ensure_ascii=False) + "\n")

    # ---------- streams ----------
    def _trade_loop(self, symbol):
        def run():
            while self.running:
                try:
                    stream = self.fp.marketdata_stub.SubscribeLatestTrades(
                        request=SubscribeLatestTradesRequest(symbol=symbol),
                        metadata=(self.fp.metadata,))
                    for ev in stream:
                        self.stream_failures[symbol] = 0
                        self.fallback_mode[symbol] = False
                        for t in ev.trades:
                            price = float(t.price.value) if t.price.value else 0.0
                            size = float(t.size.value) if t.size.value else 0.0
                            if not size:
                                continue
                            ts = datetime.fromtimestamp(
                                t.timestamp.seconds).astimezone().isoformat(timespec="milliseconds")
                            self.write(symbol, {"p": price, "s": size,
                                                "d": 1 if t.side == 1 else -1, "t": ts})
                            self.counts[symbol]["trades"] += 1
                except Exception as e:
                    if not self.running:
                        return
                    self.stream_failures[symbol] += 1
                    if self.stream_failures[symbol] >= 3:
                        self.fallback_mode[symbol] = True
                        print(f"[{iso_now()}] {symbol}: стрим упал 3x ({str(e)[:60]}) -> поллинг")
                    time.sleep(2)
        th = threading.Thread(target=run, daemon=True, name=f"trades-{symbol}")
        th.start()
        return th

    def _ob_loop(self, symbol, interval=2.0):
        def run():
            last = 0.0
            while self.running:
                now = time.time()
                if now - last >= interval:
                    try:
                        ob = self.fp.call_function(self.fp.marketdata_stub.OrderBook,
                                                   OrderBookRequest(symbol=symbol))
                        f = lambda d: float(d.value) if d.value else 0.0
                        bids, asks = [], []
                        for row in ob.orderbook.rows:
                            p, b, s = f(row.price), f(row.buy_size), f(row.sell_size)
                            if b > 0:
                                bids.append((p, b))
                            if s > 0:
                                asks.append((p, s))
                        bids.sort(key=lambda x: -x[0])
                        asks.sort(key=lambda x: x[0])
                        self.write(symbol, {"t": iso_now(), "q": "ob",
                                            "bid": bids, "ask": asks})
                        self.counts[symbol]["ob"] += 1
                        last = now
                    except Exception:
                        pass
                time.sleep(0.2)
        th = threading.Thread(target=run, daemon=True, name=f"ob-{symbol}")
        th.start()
        return th

    def _fallback_poll_loop(self, symbol):
        """Фолбэк-поллинг, если стрим мёртв (v1-логика упрощённо)."""
        last_id = None
        while self.running:
            if not self.fallback_mode[symbol]:
                time.sleep(1)
                continue
            try:
                tr = self.fp.call_function(self.fp.marketdata_stub.LatestTrades,
                                           LatestTradesRequest(symbol=symbol))
                trades = list(tr.trades)
                for t in trades:
                    tid = int(getattr(t, "trade_id", 0) or 0)
                    if last_id is not None and tid <= last_id:
                        continue
                    price = float(t.price.value) if t.price.value else 0.0
                    size = float(t.size.value) if t.size.value else 0.0
                    if not size:
                        continue
                    ts = datetime.fromtimestamp(t.timestamp.seconds).astimezone().isoformat(timespec="milliseconds")
                    self.write(symbol, {"p": price, "s": size,
                                        "d": 1 if t.side == 1 else -1, "t": ts})
                    self.counts[symbol]["trades"] += 1
                    last_id = tid
            except Exception:
                pass
            time.sleep(0.35)

    def run(self, hours):
        threads = []
        for s in self.symbols:
            threads.append(self._trade_loop(s))
            threads.append(self._ob_loop(s))
            threads.append(threading.Thread(target=self._fallback_poll_loop, args=(s,),
                                            daemon=True, name=f"fb-{s}"))
            threads[-1].start()
        deadline = time.time() + hours * 3600
        try:
            while time.time() < deadline and self.running:
                time.sleep(60)
                with self.lock:
                    stat = " | ".join(
                        f"{s}: trades={self.counts[s]['trades']} ob={self.counts[s]['ob']}"
                        f"{' FB' if self.fallback_mode[s] else ''}"
                        for s in self.symbols)
                print(f"[{iso_now()}] {stat}", flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            print("[collector] стоп, файлы закрыты", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SBER@MISX")
    ap.add_argument("--hours", type=float, default=10.0)
    ap.add_argument("--ob-interval", type=float, default=2.0)
    a = ap.parse_args()

    token = os.environ.get("FINAM_TOKEN")
    if not token:
        sys.exit("FINAM_TOKEN не задан")
    symbols = [s.strip() for s in a.symbols.split(",")]
    c = StreamCollector(token, symbols)
    print(f"[collector-stream] тикеры={symbols} ob_iv={a.ob_interval}s — стрим-режим", flush=True)
    c.run(a.hours)


if __name__ == "__main__":
    main()
