"""Laya gate model via laya-serve (Jev-compatible /v1/systemone).
Поверх стенового сигнала: гейт решает allow/block/escalate по вероятности
неблагоприятного движения. HTTP, таймауты, fallback на pass-through."""
import time
import urllib.request
import json

from config import LOOP_SEC

LAYA_URL = "http://127.0.0.1:8015/v1/systemone"
LAYA_TIMEOUT = 5.0

QUESTIONS = {
    "risk": {
        "type": "score",
        "instructions": "Probability that this trade loses more than 2% within 5 trading days? 0=low risk..2=certain adverse move.",
        "criteria": ["low risk", "moderate risk", "high risk of adverse move"],
    },
    "verdict": {
        "type": "choice",
        "instructions": "Order-flow wall trade gate: should this trade proceed?",
        "criteria": {
            "allow": "wall supports the direction, proceed",
            "block": "adverse-move probability too high, reject",
            "escalate": "conflicting signals, skip and observe",
        },
    },
}


class LayaGateModel:
    """Wall-сигнал + Laya-гейт. decide(snap) -> decision dict (формат trader.py)."""
    name = "laya-gate"

    def __init__(self, tracker=None, url: str = LAYA_URL,
                 block_threshold: float = 0.35, escalate_threshold: float = 0.35):
        from walls import WallTracker
        self.tracker = tracker or WallTracker()
        self.url = url
        self.block_t = block_threshold    # P(block) >= -> hold (запрет входа)
        self.escalate_t = escalate_threshold
        self.last_signal_block = -1
        self.signal_ttl = 3
        self._n = 0
        self.stats = {"calls": 0, "errors": 0, "gated_out": 0, "latency_sum": 0.0}

    def _call_laya(self, state: dict) -> dict | None:
        payload = json.dumps({"state": state, "questions": QUESTIONS}).encode()
        req = urllib.request.Request(self.url, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=LAYA_TIMEOUT) as resp:
                self.stats["calls"] += 1
                return json.loads(resp.read())
        except Exception:
            self.stats["errors"] += 1
            return None

    def decide(self, snap: dict) -> dict:
        report = self.tracker.update(snap, snap["tape"])
        self._n += 1
        sig = report["signals"][-1] if report["signals"] else None

        # нет свежего сигнала от стены -> hold без вызова модели
        if sig is None or self._n - self.last_signal_block > self.signal_ttl:
            if sig is not None:
                self.last_signal_block = self._n
            return self._decision("hold", 0.0, None, sig)

        # сигнал есть -> спрашиваем гейт
        state = {
            "market": sig["wall_price"],
            "action": sig["action"],
            "reason": sig["reason"],
            "mid": snap["mid"],
            "spread_bps": (snap["spread"] / snap["mid"] * 1e4) if snap["mid"] else 0,
            "imbalance": snap["imbalance"],
            "recent_tape": [
                {"side": t["side"], "price": t["price"], "size": t["size"]}
                for t in snap["tape"][-15:]
            ],
        }
        t0 = time.time()
        res = self._call_laya(state)
        lat = (time.time() - t0) * 1000
        self.stats["latency_sum"] += lat

        if res is None:
            # сервер лежит: НЕ блокируем торговлю, идём по стеновому сигналу как есть
            return self._decision(sig["action"], 0.5 if sig["action"] == "buy" else -0.5, None, sig,
                                  note="laya-down-fallback")

        answers = res["answers"]
        verdict = answers["verdict"]
        probs = verdict.get("probabilities", {})
        p_block = probs.get("block", 0)
        p_esc = probs.get("escalate", 0)
        risk = answers["risk"].get("score", 1.0)

        if verdict["choice"] == "block" or p_block >= self.block_t:
            action, score = "hold", 0.0
            self.stats["gated_out"] += 1
        elif verdict["choice"] == "escalate" or p_esc >= self.escalate_t:
            action, score = "hold", 0.0
            self.stats["gated_out"] += 1
        else:
            action = sig["action"]
            score = 0.5 if action == "buy" else -0.5

        gate = {"verdict": verdict["choice"], "p": {k: round(v, 3) for k, v in probs.items()},
                "risk": round(risk, 2), "latency_ms": round(lat)}
        return self._decision(action, score, gate, sig)

    @staticmethod
    def _decision(action, score, gate, sig, note=None):
        d = {
            "action": action,
            "probabilities": {"buy": max(0, 0.5 + score / 2),
                              "sell": max(0, 0.5 - score / 2), "hold": 0.0},
            "score": score, "confidence": abs(score),
            "signal": sig, "gate": gate, "latency_ms": 1.0,
        }
        if note:
            d["note"] = note
        return d
