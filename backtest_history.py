"""Реальный бэктест WallTracker на исторических стаканах + тэйпе (arena_data.db).

Данные: ob_snapshots (каждые ~15с, 14.4К снапшотов на MOEX-тикер, май-июль 2026)
        + trades (SiM6: 4599 сделок с side).
Метод: прогоняем WallTracker по каждому историческому снапшоту стакана как
по живому фиду. Сигналы проверяем по фактическому движению цены mid на
горизонте (следующие N снапшотов). Считаем: hit rate сигналов, PnL с
комиссией и спредом, сравнение со случайным входом (baseline).
"""
import json, sqlite3, sys, time
from datetime import datetime

from walls import WallTracker

DB = "/home/user/.openclaw/workspace/scripts/arena/arena_data.db"
FEE = 0.00035   # 0.035% акция


def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _levels(raw, n=10):
    d = json.loads(raw)
    if not isinstance(d, list) or not d:
        return []
    if isinstance(d[0], dict):
        return [(float(x["price"]), float(x["size"])) for x in d[:n]]
    return [(float(x[0]), float(x[1])) for x in d[:n]]


def load_ob_series(db, symbol, limit=3000):
    cur = db.execute(
        "SELECT ts, ts_unix, bids, asks FROM ob_snapshots WHERE symbol=? ORDER BY ts_unix",
        (symbol,))
    rows = cur.fetchmany(limit * 2)
    out = []
    for ts, tux, bids, asks in rows:
        try:
            bl = _levels(bids)
            al = _levels(asks)
            if bl and al:
                out.append({"ts": tux, "bids": bl, "asks": al})
        except Exception:
            continue
    return out[-limit:]


def run_symbol(db, symbol, horizon_snapshots=20, fee_bps_override=None):
    """horizon_snapshots: снапшоты по ~15с => 20 шт ~ 5 минут."""
    obs = load_ob_series(db, symbol)
    if len(obs) < 100:
        return {"symbol": symbol, "error": f"мало данных: {len(obs)}"}

    tr = WallTracker(k=3.0, min_size=3000, hold_min_updates=2)
    signals = []
    for i, o in enumerate(obs):
        bids, asks = o["bids"], o["asks"]
        if not bids or not asks:
            continue
        mid = (bids[0][0] + asks[0][0]) / 2
        snap = {"mid": mid, "spread": asks[0][0] - bids[0][0], "imbalance": 0.0,
                "bids": bids, "asks": asks, "tape": []}
        rep = tr.update(snap, [])
        for sig in rep["signals"]:
            signals.append({"i": i, "ts": o["ts"], **sig})

    # исходы: движение mid от сигнала до i+horizon
    results = []
    for s in signals:
        i = s["i"]
        if i + horizon_snapshots >= len(obs):
            continue
        mid0 = (obs[i]["bids"][0][0] + obs[i]["asks"][0][0]) / 2
        mid1 = (obs[i + horizon_snapshots]["bids"][0][0] + obs[i + horizon_snapshots]["asks"][0][0]) / 2
        move_bps = (mid1 / mid0 - 1) * 1e4
        direction = 1 if s["action"] == "buy" else -1
        pnl_bps = direction * move_bps - 7  # круг: комиссия 3.5+3.5 bps
        results.append({"kind": s["kind"], "action": s["action"],
                        "pnl_bps": round(pnl_bps, 1), "move_bps": round(move_bps, 1)})

    if not results:
        return {"symbol": symbol, "n_signals": len(signals), "note": "нет оценённых сигналов"}

    wins = [r for r in results if r["pnl_bps"] > 0]
    by_kind = {}
    for k in ("rejection", "breakout", "pull"):
        rs = [r["pnl_bps"] for r in results if r["kind"] == k]
        if rs:
            by_kind[k] = {"n": len(rs), "avg_bps": round(sum(rs) / len(rs), 1),
                          "win": round(len([x for x in rs if x > 0]) / len(rs), 2)}
    return {
        "symbol": symbol,
        "snapshots": len(obs),
        "signals": len(signals),
        "evaluated": len(results),
        "avg_pnl_bps": round(sum(r["pnl_bps"] for r in results) / len(results), 1),
        "win_rate": round(len(wins) / len(results), 2),
        "total_bps": round(sum(r["pnl_bps"] for r in results), 1),
        "by_kind": by_kind,
    }


def baseline(db, symbol, horizon=20, n=200):
    """Контроль: случайные входы той же частоты."""
    import random
    random.seed(42)
    obs = load_ob_series(db, symbol, limit=1000)
    if len(obs) < horizon + 10:
        return None
    pnls = []
    for _ in range(n):
        i = random.randint(0, len(obs) - horizon - 1)
        d = random.choice([1, -1])
        m0 = (obs[i]["bids"][0][0] + obs[i]["asks"][0][0]) / 2
        m1 = (obs[i + horizon]["bids"][0][0] + obs[i + horizon]["asks"][0][0]) / 2
        pnls.append(d * (m1 / m0 - 1) * 1e4 - 7)
    return round(sum(pnls) / len(pnls), 1)


if __name__ == "__main__":
    symbols = sys.argv[1].split(",") if len(sys.argv) > 1 else \
        ["SBER@MISX", "GAZP@MISX", "LKOH@MISX", "GMKN@MISX", "ROSN@MISX", "CHMF@MISX"]
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    print(f"{'symbol':12s} {'snaps':>6s} {'sigs':>5s} {'eval':>5s} {'avg_bps':>8s} {'win':>5s} {'total':>8s}  by_kind")
    tot = []
    for sym in symbols:
        r = run_symbol(db, sym)
        b = baseline(db, sym)
        if "error" in r:
            print(f"{sym:12s} {r['error']}")
            continue
        tot.append(r["total_bps"])
        bk = " ".join(f"{k}:{v['n']}({v['avg_bps']:+})" for k, v in r["by_kind"].items())
        print(f"{r['symbol']:12s} {r['snapshots']:>6d} {r['signals']:>5d} {r['evaluated']:>5d} "
              f"{r['avg_pnl_bps']:>+8.1f} {r['win_rate']:>5.0%} {r['total_bps']:>+8.1f}  {bk}  baseline={b}")
    if tot:
        print(f"\nИТОГО: {len(tot)} тикеров, суммарно {sum(tot):+.0f} bps, "
              f"в среднем {sum(tot)/len(tot):+.1f} bps/тикер")
