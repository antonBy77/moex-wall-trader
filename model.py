"""Decision model. MockModel: imbalance + CVD momentum, deterministic.
JevModel: placeholder — same TradeState shape as jev-trader's."""
import time
from config import HORIZON


def features(snap: dict) -> dict:
    """Сжатый стейт (аналог TradeState из jev-trader)."""
    tape = snap["tape"]
    now = tape[-1]["ts"] if tape else 0
    horizon = HORIZON * 2  # сек (цикл ~2с)
    recent = [t for t in tape if now - t["ts"] <= horizon]
    cvd = sum((1 if t["side"] == "buy" else -1) * t["size"] for t in recent if t["side"])
    buy_vol = sum(t["size"] for t in recent if t["side"] == "buy")
    sell_vol = sum(t["size"] for t in recent if t["side"] == "sell")
    mids = [t["price"] for t in recent]
    return {
        "mid": snap["mid"],
        "spread": snap["spread"] or 0.01,
        "spread_bps": (snap["spread"] / snap["mid"] * 1e4) if snap["mid"] else 0,
        "imbalance": snap["imbalance"],
        "cvd": cvd,
        "tape_buy": buy_vol,
        "tape_sell": sell_vol,
        "ret_1m": (mids[-1] / mids[0] - 1) * 1e4 if len(mids) > 1 else 0.0,  # bps
        "n_trades": len(recent),
    }


class MockModel:
    """Momentum + imbalance + mean reversion. Возвращает buy/sell + уверенность."""
    name = "mock-v1"

    # веса
    W_IMB, W_CVD, W_RET = 1.0, 0.6, 0.3
    THRESH = 0.35       # |score| для входа
    RR = 1.5            # во сколько раз expected edge должен превышать спред

    def decide(self, snap: dict) -> dict:
        f = features(snap)
        imb = f["imbalance"]                      # -1..1
        cvd_n = max(-1.0, min(1.0, f["cvd"] / max(1.0, f["tape_buy"] + f["tape_sell"])))
        ret_n = max(-1.0, min(1.0, f["ret_1m"] / 30.0))
        score = self.W_IMB * imb + self.W_CVD * cvd_n + self.W_RET * ret_n
        score = max(-1.0, min(1.0, score))

        spread = f["spread"]
        # спред-фильтр: ожидаемое движение (score * ATR-прокси) должно покрыть спред*RR
        # простое прокси: движение за 1м в цене
        move = abs(f["ret_1m"]) / 1e4 * f["mid"]
        edge_ok = move >= spread * self.RR * 2  # ход за горизонт должен покрыть спред с запасом

        if abs(score) < self.THRESH or not edge_ok:
            action, conf = "hold", abs(score)
        elif score > 0:
            action, conf = "buy", score
        else:
            action, conf = "sell", -score

        p = {"buy": max(0.0, (score + 1) / 2), "sell": max(0.0, (1 - score) / 2), "hold": 0.0}
        tot = p["buy"] + p["sell"]
        p = {k: v / tot for k, v in p.items()}
        return {
            "action": action, "probabilities": p, "score": score,
            "confidence": conf, "features": f, "latency_ms": 0.1,
        }


class JevModel:
    """TODO: TypeSafe Jev через OpenRouter (experimental_evaluate аналог).
    TradeState уже совместим по форме с jev-trader."""
    name = "jev-todo"

    def decide(self, snap: dict) -> dict:
        raise NotImplementedError("Jev не подключён — используй SPREAD_MODEL=mock")


def get_model(name: str):
    return MockModel() if name == "mock" else JevModel()
