"""Полный сбор данных: стакан + лента синхронно -> NDJSON (формат DSH-архива).

Формат строки (совместим с tape_fri/fri.ndjson):
  trade:  {"p":..., "s":..., "d":+1/-1, "t":"ISO"}          (принт)
  ob:     {"t":"ISO", "q":"ob", "bid":[[p,s]x10], "ask":[[..]]}  (снапшот)

Один writer-поток, ротация по дате: data/SBER@MISX-2026-09-24.ndjson.gz (gzip на лету).
Запуск: FINAM_TOKEN=... python3 collector.py --symbols SBER@MISX,SiU6@RTSX --hours 14
"""
import argparse, datetime, gzip, json, os, signal, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feed import MarketFeed, _f

MSK = datetime.timezone(datetime.timedelta(hours=3))
DATA_DIR = os.path.expanduser("~/.hermes/scripts-dev/moex-spread/data")


def iso_now() -> str:
    return datetime.datetime.now(MSK).isoformat(timespec="milliseconds")


def write_gz(path: str, line: str):
    """Аппенд в gzip: читаем хвост нельзя дёшево, поэтому отдельный файл на сессию
    и обычный .ndjson + сжатие на следующий день не делаем — пишем plain, жмём потом."""
    raise NotImplementedError  # заменено write_line


class Collector:
    def __init__(self, symbol: str, out_dir: str = DATA_DIR):
        self.feed = MarketFeed(symbol)
        self.symbol = symbol
        self.out_dir = out_dir
        self.today = None
        self.fh = None
        self.n_trades = 0
        self.n_obs = 0
        self.running = True

    def _open(self):
        d = datetime.datetime.now(MSK).date()
        if d != self.today:
            if self.fh:
                self.fh.close()
            os.makedirs(self.out_dir, exist_ok=True)
            name = f"{self.symbol.replace('@','-')}-{d}.ndjson"
            self.fh = open(os.path.join(self.out_dir, name), "a")
            self.today = d

    def write(self, obj: dict):
        self._open()
        self.fh.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")

    def loop(self, a_tape_interval=1.0):
        # первый цикл: стоковый last_trade_id — берём текущий максимум, историю не пишем
        self.feed.start()
        time.sleep(2.0)
        last_id = self.feed.last_trade_id
        self.gaps = 0
        self.lost_est = 0
        self.POLL = a_tape_interval  # задаётся извне под бюджет API
        self.OB_IV = 2.0             # стакан раз в 2с (экономия бюджета)
        last_poll = 0.0
        last_ob = 0.0
        while self.running:
            now = time.time()
            # лента: раз в POLL
            if now - last_poll >= self.POLL:
                last_poll = now
                try:
                    self.feed._poll_trades()
                    fresh = [t for t in self.feed.tape if t.get("id") and t["id"] > last_id]
                    if fresh:
                        ids = sorted(t["id"] for t in fresh)
                        # детект пропусков: разрыв id внутри окна ленты API
                        # (лента API хранит ~1000; если разрыв > 10% окна — маркер)
                        missing = ids[-1] - ids[0] + 1 - len(ids)
                        if missing > 0 and len(ids) > 0:
                            self.gaps += 1
                            self.lost_est += missing
                            self.write({"t": iso_now(), "q": "gap",
                                        "missing": missing, "from": ids[0], "to": ids[-1]})
                        for t in fresh:
                            self.write({"p": t["price"], "s": t["size"],
                                        "d": 1 if t["side"] == "buy" else -1,
                                        "t": iso_now()})
                        self.n_trades += len(fresh)
                        last_id = ids[-1]
                except Exception as e:
                    print(f"[{self.symbol}] tape err: {e}", file=sys.stderr)
                    time.sleep(1)
            # стакан: раз в OB_IV
            if now - last_ob >= self.OB_IV:
                last_ob = now
                try:
                    snap = self.feed.snapshot()
                    if snap["bids"] and snap["asks"]:
                        # снапшот стакана — ВСЕ уровни
                        self.write({"t": iso_now(), "q": "ob",
                                    "bid": [[p, s] for p, s in snap["bids"]],
                                    "ask": [[p, s] for p, s in snap["asks"]]})
                        self.n_obs += 1
                except Exception as e:
                    print(f"[{self.symbol}] ob err: {e}", file=sys.stderr)
                    time.sleep(1)
            time.sleep(0.05)

    def stop(self):
        self.running = False

    def close(self):
        self.feed.close()
        if self.fh:
            self.fh.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SBER@MISX")
    ap.add_argument("--hours", type=float, default=14.0, help="сколько часов собирать")
    ap.add_argument("--tape-interval", type=float, default=None,
                    help="интервал поллинга ленты на тикер, сек (авто по числу тикеров)")
    a = ap.parse_args()

    syms = [s.strip() for s in a.symbols.split(",")]
    # бюджет API: ~200 req/min суммарно = 3.3/с. Раскладываем:
    # стакан раз в 2с на тикер + лента равным остатком.
    tape_iv = a.tape_interval or max(0.3, (2.0 * len(syms)) / max(1.0, 3.3 / (1 / 2.0) * len(syms) / len(syms) - 0.5) / 1)
    # проще: tape_interval = (остались req) / (тикеры) ; берём консервативно
    tape_iv = a.tape_interval or max(0.5, 2.0 * len(syms) / 2.0 / len(syms))
    print(f"[collector] тикеры={len(syms)} tape_iv={tape_iv:.2f}s "
          f"(бюджет ~{1/tape_iv*len(syms) + 0.5*len(syms):.1f} req/s)")

    deadline = time.time() + a.hours * 3600
    cols = []
    threads = []
    tape_iv = a.tape_interval or 1.0  # консервативный дефолт, переопределяется ниже
    # бюджет: 200 req/min = 3.33/с. На тикер: OB 0.5/с (раз в 2с) + лента X/с.
    # X = (3.33/len(syms)) - 0.5, минимум 0.5/с
    tape_iv = a.tape_interval or max(0.4, 1.0 / max(0.5, 3.33 / len(syms) - 0.5))
    print(f"[collector] тикеров={len(syms)} tape_iv={tape_iv:.2f}с "
          f"(~{len(syms)*(1/tape_iv+0.5):.1f} req/s из 3.3 доступных)")
    for sym in syms:
        c = Collector(sym.strip())
        t = threading.Thread(target=c.loop, args=(tape_iv,), daemon=True)
        c.deadline = deadline
        cols.append(c)
        threads.append(t)
        t.start()
        print(f"[collector] {c.symbol} -> {c.out_dir}")

    def bye(sig, frm):
        print("\n[collector] stopping...")
        for c in cols:
            c.stop()
    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)

    try:
        while time.time() < deadline and any(c.running for c in cols):
            time.sleep(30)
            for c in cols:
                print(f"[{datetime.datetime.now(MSK).strftime('%H:%M:%S')}] {c.symbol}: "
                      f"trades={c.n_trades} ob={c.n_obs}", flush=True)
            if all(not c.running for c in cols):
                break
    finally:
        for c in cols:
            c.stop()
        time.sleep(2)
        for c in cols:
            c.close()
        print("[collector] done")


if __name__ == "__main__":
    main()
