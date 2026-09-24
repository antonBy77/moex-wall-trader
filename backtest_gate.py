"""Бэктест стратегий стен с Laya-гейтом на данных collector_stream (v2).

Отличие от прошлых тестов: данные стриминговые (полная лента + стакан 2с),
гейт — детерминированная реплика Laya-правил (CVD/trend/imbalance/режим),
плюс опциональный живой Laya через laya-serve :8015.

Стратегии:
  A. rejection: 1-е касание стены, вход лимиткой у стены, тейк 5п/10п,
     стоп = пробой стены на 1 тик. Мейкер-мейкер (0 комиссии).
  B. breakout:  стену проели (eaten>=10%, 3+ атаки) -> вход по пробою.

Гейт (replica):
  block = (cvd против направления) OR (trend 5м против) OR (imb_top3 против)
Запись: каждая сделка с флагами гейта -> A/B сравнение.
"""
import sys, os, json, argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from walls_v2 import SmartWallTracker


def load_day(path):
    """Читает NDJSON, возвращает (tape, obs): tape=[{t,p,s,d}], obs=[{t,bid,ask}]."""
    tape, obs = [], []
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if "q" in r:
                if r["q"] == "ob" and r.get("bid") and r.get("ask"):
                    obs.append({"t": r["t"], "bid": r["bid"], "ask": r["ask"]})
            else:
                tape.append({"t": r["t"], "p": r["p"], "s": r["s"], "d": r["d"]})
    return tape, obs


def bisect_ts(arr, t_iso):
    i = 0
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        if arr[mid]["t"] <= t_iso:
            lo = mid + 1
        else:
            hi = mid
    return lo


def gate_features(tape, j, obs, k, direction):
    """Фичи на момент сигнала: cvd(10м), trend 5м, imb топ-3."""
    # CVD за 10 минут до сигнала
    t_sig = datetime.fromisoformat(tape[j]["t"])
    cvd = 0
    m = j
    while m >= 0:
        t_m = datetime.fromisoformat(tape[m]["t"])
        if (t_sig - t_m).total_seconds() > 600:
            break
        cvd += tape[m]["s"] * tape[m]["d"]
        m -= 1
    # trend 5м: (last - close 5м назад) / tick
    px = tape[j]["p"]
    m = j
    px_back = px
    while m >= 0:
        t_m = datetime.fromisoformat(tape[m]["t"])
        if (t_sig - t_m).total_seconds() > 300:
            break
        px_back = tape[m]["p"]
        m -= 1
    trend = (px - px_back)
    # imb топ-3 стакана
    imb = 0.0
    if k < len(obs):
        b3 = sum(s for _, s in obs[k]["bid"][:3])
        a3 = sum(s for _, s in obs[k]["ask"][:3])
        imb = (b3 - a3) / (b3 + a3) if (b3 + a3) else 0
    return cvd * direction, trend * direction, imb * direction


def gate_allows(cvd_dir, trend_dir, imb_dir):
    """Реплика гейта: блок если 2+ фактора против."""
    against = sum(1 for x in (cvd_dir, trend_dir, imb_dir) if x < 0)
    return against < 2


def simulate(tape, obs, wall_k=3.0, wall_min=3000, take_ticks=(5, 10),
             use_gate=True, strategy="rejection"):
    tracker = SmartWallTracker()
    trades = []
    signals = 0
    # индекс стакана по времени — курсор
    k = 0
    open_pos = None
    for j, tr in enumerate(tape):
        t_iso = tr["t"]
        # продвигаем стакан
        while k < len(obs) and obs[k]["t"] <= t_iso:
            k += 1
        if k >= len(obs):
            break
        ob = obs[k]
        mid = (ob["bid"][0][0] + ob["ask"][0][0]) / 2
        # обновляем трекер на снапшоте стакана (по последнему снапшоту на тик —
        # для скорости обновляем только на каждом 5-м тике)
        if j % 5 == 0:
            tracker.update({"mid": mid, "spread": ob["ask"][0][0] - ob["bid"][0][0],
                            "imbalance": 0, "bids": ob["bid"], "asks": ob["ask"]},
                           tape[max(0, j - 10):j])
        # --- открытая позиция: тейк/стоп ---
        if open_pos:
            px_in = open_pos["entry"]
            tick = open_pos["tick"]
            take = open_pos["take"]
            stop = open_pos["stop"]
            if strategy == "rejection":
                hit_tp = (tr["p"] >= px_in + take) if open_pos["dir"] > 0 else (tr["p"] <= px_in - take)
                hit_sl = (tr["p"] <= stop) if open_pos["dir"] > 0 else (tr["p"] >= stop)
            else:  # breakout: stop = возврат за уровень стены
                hit_tp = (tr["p"] >= px_in + take) if open_pos["dir"] > 0 else (tr["p"] <= px_in - take)
                hit_sl = (tr["p"] <= stop) if open_pos["dir"] > 0 else (tr["p"] >= stop)
            if hit_tp:
                trades.append({**open_pos, "exit": px_in + take * open_pos["dir"],
                               "result": "tp", "t_out": t_iso})
                open_pos = None
            elif hit_sl:
                trades.append({**open_pos, "exit": stop,
                               "result": "sl", "t_out": t_iso})
                open_pos = None
            elif (datetime.fromisoformat(t_iso) -
                  datetime.fromisoformat(open_pos["t_in"])).total_seconds() > 600:
                trades.append({**open_pos, "exit": tr["p"],
                               "result": "time", "t_out": t_iso})
                open_pos = None
            continue
        # --- сигналы от трекера ---
        rep = getattr(tracker, "last_report", None) or {}
        sigs = list(rep.get("signals", []))
        if not sigs:
            continue
        # берём сигналы этого снапшота (они генерятся на update, т.е. каждый 5-й тик)
        while sigs:
            sig = sigs.pop(0)
            stype = sig.get("kind", "")
            side = sig.get("wall_side", sig.get("side", ""))           # bid/ask
            price = sig.get("wall_price", sig.get("price"))
            if price is None:
                continue
            direction = 0
            entry = stop = None
            if strategy == "rejection" and stype == "rejection":
                direction = 1 if side == "bid" else -1
                entry = price
                stop = price - 0.02 * direction
            elif strategy == "breakout" and stype == "breakout":
                direction = 1 if side == "ask" else -1   # съели ask -> BUY
                entry = mid
                stop = price + 0.02 * -direction
            if not direction:
                continue
            signals += 1
            cvd_d, trend_d, imb_d = gate_features(tape, j, obs, k, direction)
            allowed = gate_allows(cvd_d, trend_d, imb_d)
            rec = {"t": t_iso, "type": stype, "side": side, "dir": direction,
                   "cvd": cvd_d, "trend": trend_d, "imb": imb_d,
                   "gate": allowed, "entry": entry, "stop": stop,
                   "tick": 0.01, "take": 0.05}
            if use_gate and not allowed:
                trades.append({**rec, "result": "blocked"})
                continue
            if open_pos is None:
                open_pos = {**rec, "t_in": t_iso}
                break
    return trades, signals


def report(trades, signals, label):
    filled = [t for t in trades if t.get("result") != "blocked"]
    blocked = [t for t in trades if t.get("result") == "blocked"]
    tp = sum(1 for t in filled if t["result"] == "tp")
    sl = sum(1 for t in filled if t["result"] == "sl")
    to = sum(1 for t in filled if t["result"] == "time")
    # PnL в рублях: 1 лот SBER, цена в рублях
    pnl = sum((t["exit"] - t["entry"]) * t["dir"] for t in filled
              if t["result"] != "blocked")
    win = sum(1 for t in filled if (t["exit"] - t["entry"]) * t["dir"] > 0)
    print(f"\n== {label} ==")
    print(f"сигналов: {signals}, заблокировано гейтом: {len(blocked)}, "
          f"сделок: {len(filled)} (tp {tp} / sl {sl} / timeout {to})")
    if filled:
        print(f"PnL: {pnl:+.2f} руб на 1 лот | win: {win}/{len(filled)}"
              f" = {win/len(filled)*100:.0f}%")
    return pnl, len(filled), (win / len(filled) * 100 if filled else 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/home/user/.hermes/scripts-dev/moex-spread/data/SBER-MISX-2026-09-24.ndjson")
    ap.add_argument("--strategy", default="rejection", choices=["rejection", "breakout"])
    a = ap.parse_args()
    tape, obs = load_day(a.data)
    print(f"данные: {len(tape)} сделок, {len(obs)} снапшотов стакана")

    # A/B: без гейта и с гейтом
    t0, s0 = simulate(tape, obs, use_gate=False, strategy=a.strategy)
    p0, n0, w0 = report(t0, s0, f"{a.strategy} БЕЗ гейта")
    t1, s1 = simulate(tape, obs, use_gate=True, strategy=a.strategy)
    p1, n1, w1 = report(t1, s1, f"{a.strategy} С гейтом (cvd/trend/imb)")
    if n0:
        print(f"\nэффект гейта: PnL {p0:+.2f} -> {p1:+.2f} руб "
              f"({p1 - p0:+.2f}), сделок {n0} -> {n1}")


if __name__ == "__main__":
    main()
