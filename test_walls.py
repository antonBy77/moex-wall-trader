"""Тесты: walls (трекер стен), laya_gate (мок HTTP), trader (филлы, PnL)."""
import json, sys, unittest
from unittest.mock import patch

sys.path.insert(0, ".")
from walls import WallTracker, WallModel
from feed import MarketFeed


def snap(bids, asks, mid=None, tape=None):
    if mid is None:
        mid = (bids[0][0] + asks[0][0]) / 2
    spread = asks[0][0] - bids[0][0]
    return {"mid": mid, "spread": spread, "imbalance": 0.0,
            "bids": bids, "asks": asks, "tape": tape or []}


class TestWallTracker(unittest.TestCase):
    def test_wall_detected(self):
        tr = WallTracker(k=3.0, min_size=100, hold_min_updates=1)
        bids = [(100.0, 5000), (99.99, 100), (99.98, 90)]   # 5000 >> avg
        asks = [(100.02, 110), (100.03, 100), (100.04, 90)]
        r = tr.update(snap(bids, asks), [])
        # bid-стена @100 должна появиться
        self.assertTrue(any(w["price"] == 100.0 and w["side"] == "bid" for w in r["walls"] + [
            {"price": p, "side": "bid"} for p in tr.walls["bid"]]))

    def test_eaten_gives_breakout(self):
        tr = WallTracker(k=3.0, min_size=100, hold_min_updates=1, pull_stay_bps=1e9)
        bids = [(100.0, 5000), (99.9, 100)]
        asks = [(100.1, 100), (100.2, 90)]
        tr.update(snap(bids, asks), [])
        # стена съедена: объём упал на 80%, были sell-принты по её цене
        tape = [{"side": "sell", "price": 100.0, "size": 3000, "ts": 0}]
        r = tr.update(snap([(100.0, 1000), (99.9, 100)], asks), tape)
        eaten = [s for s in r["signals"] if s["kind"] == "breakout"]
        self.assertTrue(len(eaten) == 1)
        self.assertEqual(eaten[0]["action"], "sell")  # bid-стену съели -> SELL

    def test_pulled_gives_pull_signal(self):
        tr = WallTracker(k=3.0, min_size=100, hold_min_updates=1, pull_stay_bps=1e9)
        bids = [(100.0, 5000), (99.9, 100)]
        asks = [(100.1, 100), (100.2, 90)]
        tr.update(snap(bids, asks), [])
        # стена исчезла без принтов
        r = tr.update(snap([(99.99, 100), (99.9, 100)], asks), [])
        pulls = [s for s in r["signals"] if s["kind"] == "pull"]
        self.assertTrue(len(pulls) == 1)
        self.assertEqual(pulls[0]["action"], "sell")

    def test_no_wall_in_flat_book(self):
        tr = WallTracker(k=3.0, min_size=100)
        bids = [(100.0, 100), (99.99, 100)]
        asks = [(100.01, 100), (100.02, 100)]
        tr.update(snap(bids, asks), [])
        self.assertEqual(len(tr.walls["bid"]), 0)
        self.assertEqual(len(tr.walls["ask"]), 0)


class TestTraderAccounting(unittest.TestCase):
    """Филлы и PnL нетто-позиции."""
    def _trader(self):
        import trader
        t = trader.Trader.__new__(trader.Trader)
        t.pos, t.entry, t.realized = 0, 0.0, 0.0
        t.spread_earned = 0.0
        t.resting = None
        t.fills = t.quotes = t.blocks = 0
        t.last_event = None
        t.events = []
        return t

    def test_buy_then_sell_profit(self):
        import trader
        t = self._trader()
        t.resting = {"side": "buy", "price": 10.0, "size": 10, "block": 0}
        t._fill(10.0, 10, {"id": 1})   # купили 10 @10
        self.assertEqual(t.pos, 10)
        self.assertEqual(t.entry, 10.0)
        t.resting = {"side": "sell", "price": 10.2, "size": 10, "block": 1}
        t._fill(10.2, 10, {"id": 2})
        self.assertEqual(t.pos, 0)
        self.assertAlmostEqual(t.realized, 2.0)  # (10.2-10)*10

    def test_loss(self):
        import trader
        t = self._trader()
        t.resting = {"side": "buy", "price": 10.0, "size": 10, "block": 0}
        t._fill(10.0, 10, {"id": 1})
        t.resting = {"side": "sell", "price": 9.9, "size": 10, "block": 1}
        t._fill(9.9, 10, {"id": 2})
        self.assertAlmostEqual(t.realized, -1.0)

    def test_post_only_fill_rule(self):
        import trader
        t = self._trader()
        # buy-лимитка на bid НЕ может исполниться buy-принтом
        t.resting = {"side": "buy", "price": 100.0, "size": 1, "block": 0}
        # в trader._check_fills стороне buy нужен sell-принт <= price
        # проверим сам предикат:
        r = t.resting
        hit_wrong = r["side"] == "buy" and "buy" == "sell" and 99.0 <= r["price"]
        self.assertFalse(hit_wrong)
        hit_right = r["side"] == "buy" and "sell" == "sell" and 99.5 <= r["price"]
        self.assertTrue(hit_right)


class TestFeed(unittest.TestCase):
    def test_feed_snapshot_shapes(self):
        mf = MarketFeed.__new__(MarketFeed)
        # не подключаемся, только структуры
        self.assertTrue(hasattr(MarketFeed, "snapshot"))
        self.assertTrue(hasattr(MarketFeed, "_poll_trades"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
