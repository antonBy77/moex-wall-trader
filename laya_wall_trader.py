"""Wall trading с Laya-гейтом. DRY_RUN only."""
import time
from config import LOOP_SEC
from feed import MarketFeed
from walls import WallTracker
from laya_gate import LayaGateModel
from trader import Trader


class LayaWallTrader(Trader):
    def __init__(self):
        self.feed = MarketFeed()
        self.tracker = WallTracker()
        self.model = LayaGateModel(self.tracker)
        self.pos, self.entry, self.realized = 0, 0.0, 0.0
        self.spread_earned = 0.0
        self.resting = None
        self.fills = self.quotes = self.blocks = 0
        self.last_event = None
        self.events = []
        self.sl_tp = None

    def _quote(self, decision, snap):
        sig = decision.get("signal")
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
        print(f"[laya-wall] model={self.model.name} url={self.model.url} DRY_RUN")
        try:
            while max_blocks is None or self.blocks < max_blocks:
                time.sleep(LOOP_SEC)
                self.blocks += 1
                snap = self.feed.snapshot()
                tape_before = max(0, len(snap["tape"]) - 3)
                self._check_fills(tape_before)
                decision = self.model.decide(snap)
                self._quote(decision, snap)
                if self.pos != 0 and self.sl_tp:
                    sl, tp = self.sl_tp["sl"], self.sl_tp["tp"]
                    px = snap["bids"][0][0] if self.pos > 0 else snap["asks"][0][0]
                    hit_sl = self.pos > 0 and px <= sl or self.pos < 0 and px >= sl
                    hit_tp = self.pos > 0 and px >= tp or self.pos < 0 and px <= tp
                    if hit_sl or hit_tp:
                        self._force_close(px, "SL" if hit_sl else "TP")
                if self.blocks % 10 == 0:
                    self._status(decision, snap)
                    g = decision.get("gate")
                    if g:
                        print(f"        gate: {g['verdict']} p={g['p']} risk={g['risk']} ({g['latency_ms']}ms)")
                    st = self.model.stats
                    if self.blocks % 50 == 0:
                        avg_lat = st["latency_sum"] / max(1, st["calls"])
                        print(f"        [laya] calls={st['calls']} err={st['errors']} gated_out={st['gated_out']} avg={avg_lat:.0f}ms")
        except KeyboardInterrupt:
            pass
        finally:
            self.feed.close()
            self._final()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=150)
    a = ap.parse_args()
    LayaWallTrader().run(max_blocks=a.blocks or None)
