"""Run wall-based trading. DRY_RUN only."""
import time
from config import LOOP_SEC, MODEL
from feed import MarketFeed
from walls import WallTracker, WallModel
from trader import Trader


class WallTrader(Trader):
    def __init__(self):
        # обходим Trader.__init__ без его модели
        self.feed = MarketFeed()
        self.tracker = WallTracker()
        self.model = WallModel(self.tracker)
        self.pos, self.entry, self.realized = 0, 0.0, 0.0
        self.spread_earned = 0.0
        self.resting = None
        self.fills = self.quotes = self.blocks = 0
        self.last_event = None
        self.events = []
        self.sl_tp = None  # stop у противоположной стены / take у цели

    def _quote(self, decision, snap):
        sig = decision.get("signal")
        # SL: за стену; TP: symmetrical 1.5R от стенового уровня
        if sig:
            wp = sig["wall_price"]
            if sig["action"] == "buy":
                self.sl_tp = {"sl": wp - 5, "tp": snap["mid"] + (snap["mid"] - wp) * 1.5}
            else:
                self.sl_tp = {"sl": wp + 5, "tp": snap["mid"] - (wp - snap["mid"]) * 1.5}
        super()._quote(decision, snap)

    def run(self, max_blocks=None):
        self.feed.start()
        time.sleep(2.5)
        print(f"[wall-trader] model={self.model.name} DRY_RUN; стены: k=3x avg, min 3000 лотов")
        try:
            while max_blocks is None or self.blocks < max_blocks:
                time.sleep(LOOP_SEC)
                self.blocks += 1
                snap = self.feed.snapshot()
                tape_before = max(0, len(snap["tape"]) - 3)
                self._check_fills(tape_before)
                decision = self.model.decide(snap)
                self._quote(decision, snap)
                # SL/TP по bid/ask
                if self.pos != 0 and self.sl_tp:
                    sl, tp = self.sl_tp["sl"], self.sl_tp["tp"]
                    px = snap["bids"][0][0] if self.pos > 0 else snap["asks"][0][0]
                    hit_sl = self.pos > 0 and px <= sl or self.pos < 0 and px >= sl
                    hit_tp = self.pos > 0 and px >= tp or self.pos < 0 and px <= tp
                    if hit_sl or hit_tp:
                        self._force_close(px, "SL" if hit_sl else "TP")
                if self.blocks % 10 == 0:
                    self._status(decision, snap)
                    walls = self.tracker.last_report or {"walls": []}
                    live = [w for w in walls["walls"] if w.get("event") == "hold"]
                    if live:
                        print("        walls:", "; ".join(
                            f"{w['side']}@{w['price']} x{w['size']:.0f} age{w['age']}" for w in live[:3]))
        except KeyboardInterrupt:
            pass
        finally:
            self.feed.close()
            self._final()

    def _force_close(self, px, why):
        side = "sell" if self.pos > 0 else "buy"
        qty = abs(self.pos)
        if self.pos > 0:
            self.realized += (px - self.entry) * qty
        else:
            self.realized += (self.entry - px) * qty
        self.last_event = {"type": why, "side": side, "price": px, "size": qty,
                           "pos": 0, "realized": round(self.realized, 2)}
        self._log(self.last_event)
        print(f"        {why}: close {qty} @ {px} -> pnl {self.realized:+.2f}")
        self.pos, self.sl_tp = 0, None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=150)
    a = ap.parse_args()
    WallTrader().run(max_blocks=a.blocks or None)
