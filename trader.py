"""Trader loop: quote -> sim-fill accounting. DRY_RUN ONLY (no real orders).

Логика как в jev-trader, но адаптирована:
- не каждый "блок", а раз в LOOP_SEC: read book -> decide -> place (virtual)
  post-only limit на 1 tick внутрь касания, отменяя предыдущую.
- fill: чужой принт на ленте пробил нашу цену => исполнены (post-only честно:
  наша лимитка на bid не может исполниться от buy-принта, только sell-принт
  с price <= наша; для ask наоборот).
- позиция нетто, cap MAX_POS, PnL по сделкам + виртуальный спред-доход.
"""
import json, os, time
from datetime import datetime, timezone

from config import LOOP_SEC, MAX_POS, LOG_PATH, MODEL, SYMBOL, TICK
from feed import MarketFeed
from model import get_model


class Trader:
    def __init__(self, symbol: str = SYMBOL):
        self.feed = MarketFeed(symbol)
        self.model = get_model(MODEL)
        self.pos = 0            # +long lots / -short
        self.entry = 0.0        # средняя цена входа
        self.realized = 0.0
        self.spread_earned = 0.0
        self.resting = None     # {"side","price","size","block"}
        self.fills = 0
        self.quotes = 0
        self.blocks = 0
        self.last_event = None
        self.events = []

    # --- fills: сканируем новые принты против нашей лимитки ---
    def _check_fills(self, tape_before: int):
        if not self.resting:
            return
        tape = self.feed.snapshot()["tape"]
        for t in tape[tape_before:]:
            r = self.resting
            hit = (r["side"] == "buy" and t["side"] == "sell" and t["price"] <= r["price"]) or \
                  (r["side"] == "sell" and t["side"] == "buy" and t["price"] >= r["price"])
            if hit:
                self._fill(t["price"], r["size"], t)
                break

    def _fill(self, price: float, size: int, trade: dict):
        r = self.resting
        side = r["side"]
        # нетто-позиция: buy-филл закрывает short и открывает long
        if side == "buy":
            if self.pos < 0:
                closed = min(size, -self.pos)
                self.realized += (self.entry - price) * closed
                self.spread_earned += (self.entry - price) * closed
                self.pos += closed
                size -= closed
            self.pos += size
            if self.pos > 0 and size > 0:
                self.entry = price if self.pos == size else (self.entry * (self.pos - size) + price * size) / self.pos
        else:
            if self.pos > 0:
                closed = min(size, self.pos)
                self.realized += (price - self.entry) * closed
                self.spread_earned += (price - self.entry) * closed
                self.pos -= closed
                size -= closed
            self.pos -= size
            if self.pos < 0 and size > 0:
                self.entry = price if -self.pos == size else (self.entry * (-self.pos - size) + price * size) / -self.pos
        self.fills += 1
        self.resting = None
        self.last_event = {"type": "fill", "side": side, "price": price,
                           "size": size, "trade": trade.get("id"),
                           "pos": self.pos, "realized": round(self.realized, 2)}
        self._log(self.last_event)

    # --- quote: post-only лимитка на tick внутрь касания ---
    def _quote(self, decision: dict, snap: dict):
        if not snap["bids"] or not snap["asks"]:
            return
        best_bid, best_ask = snap["bids"][0][0], snap["asks"][0][0]
        allowed_buy = self.pos + (1 if self.resting and self.resting["side"] == "buy" else 0) < MAX_POS
        allowed_sell = self.pos - (1 if self.resting and self.resting["side"] == "sell" else 0) > -MAX_POS

        action = decision["action"]
        if action == "buy" and allowed_buy:
            side, price = "buy", round(best_bid + TICK, 2)
        elif action == "sell" and allowed_sell:
            side, price = "sell", round(best_ask - TICK, 2)
        else:
            # capped: ставим на сокращающую сторону или ничего
            if self.pos > 0 and allowed_sell:
                side, price = "sell", round(best_ask - TICK, 2)
            elif self.pos < 0 and allowed_buy:
                side, price = "buy", round(best_bid + TICK, 2)
            else:
                self.resting = None
                return
        self.resting = {"side": side, "price": price, "size": 1,
                        "block": self.blocks, "ts": time.time()}
        self.quotes += 1
        self.last_event = {"type": "quote", "side": side, "price": price,
                           "action": action, "score": round(decision["score"], 2),
                           "p_buy": round(decision["probabilities"]["buy"], 2)}

    def _log(self, ev: dict):
        ev["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.events.append(ev)
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def run(self, max_blocks: int = None):
        self.feed.start()
        time.sleep(2.5)
        print(f"[trader] model={self.model.name} symbol={self.feed.symbol} DRY_RUN")
        try:
            while max_blocks is None or self.blocks < max_blocks:
                time.sleep(LOOP_SEC)
                self.blocks += 1
                tape_before = max(0, len(self.feed.snapshot()["tape"]) - 3)
                self._check_fills(tape_before)
                snap = self.feed.snapshot()
                t0 = time.time()
                decision = self.model.decide(snap)
                decision["latency_ms"] = (time.time() - t0) * 1000
                self._quote(decision, snap)
                if self.blocks % 15 == 0:
                    self._status(decision, snap)
        except KeyboardInterrupt:
            pass
        finally:
            self.feed.close()
            self._final()

    def _status(self, d, snap):
        pos_s = f"{self.pos:+d}" if self.pos else " 0"
        print(f"[{self.blocks:4d}] mid={snap['mid']:.2f} spr={snap['spread']:.2f} "
              f"{d['action']:4s} score={d['score']:+.2f} quote="
              f"{self.resting['side'][:1].upper() if self.resting else '-'}"
              f"{self.resting['price'] if self.resting else ''} pos={pos_s} "
              f"fills={self.fills} pnl={self.realized:+.2f}")

    def _final(self):
        print(f"\n=== DONE: blocks={self.blocks} quotes={self.quotes} fills={self.fills} "
              f"pos={self.pos:+d} realized={self.realized:+.2f} spread_earned={self.spread_earned:+.2f}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=100, help="число циклов (0=бесконечно)")
    a = ap.parse_args()
    Trader().run(max_blocks=a.blocks or None)
