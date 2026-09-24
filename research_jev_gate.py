"""Шаг 7: Jev/Laya как гейт над 5 классами событий (стены + 4 микро-паттерна).

Сравниваем на ОДНИХ И ТЕХ ЖЕ событиях:
  A) без гейта — торгуем всё
  B) Jev-гейт — торгуем только allow (вопрос в laya-serve, критерии wall/stack-специфичные)
  C) случайный гейт той же селекции (контроль: режет столько же случайно)
  D) детерминированный гейт: cvd в сторону входа (простое правило)

Jev спрашиваем офлайн-режимом по каждому событию (state -> document), вердикт
allow/block. Скорость ~1-2с/вызов -> сэмплируем события (max 300 на класс).
"""
import json
import os
import random
import statistics
import sys
import time
import urllib.request
from collections import deque
from datetime import datetime

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from research_micro import load, resolve, TICK  # переиспользуем движок

LAYA = "http://127.0.0.1:8015/v1/systemone"
MAX_PER_CLASS = 60          # 5 классов * 60 * ~1.5с ≈ 7.5 мин
CRITERIA = {
    "allow": "conditions favour entry, proceed",
    "block": "adverse-move probability too high, skip",
}


def ask_jev(doc: str, action: str, timeout=12):
    body = json.dumps({
        "state": {"document": doc},
        "questions": {"verdict": {
            "type": "choice",
            "instructions": f"MOEX scalping gate: {action} entry at wall/pattern event. Proceed?",
            "criteria": CRITERIA}}}).encode()
    req = urllib.request.Request(LAYA, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read())
    v = res.get("answers", {}).get("verdict", {})
    ch = str(v.get("choice", "")).lower()
    return ("allow" in ch)


def build_events():
    """Собираем события 5 классов с фичами (как в research_micro + cvd)."""
    tape, obs = load()
    events = []
    ob_i = 0
    prev_ob = None
    tape_window = deque(maxlen=800)
    speed_hist = []
    cvd = 0.0
    day_hi = day_lo = None

    for i, (dt, price, size, d) in enumerate(tape):
        cvd += size * d
        day_hi = price if day_hi is None else max(day_hi, price)
        day_lo = price if day_lo is None else min(day_lo, price)
        tape_window.append((dt, size))

        while ob_i < len(obs) and obs[ob_i][0] <= dt:
            cur = obs[ob_i]
            ob_dt, bids, asks = cur
            if prev_ob is not None:
                pb, pa = prev_ob[1], prev_ob[2]
                for side, now, before in (("bid", bids, pb), ("ask", asks, pa)):
                    if not before:
                        continue
                    sizes = [s for _, s in before[:10]]
                    med = statistics.median(sizes) if sizes else 0
                    for p0, s0 in before[:10]:
                        if s0 >= max(3 * med, 2000):
                            traded = any(abs(t[1] - p0) <= TICK
                                         for t in tape[max(0, i - 1):i])
                            still = any(abs(x[0] - p0) <= TICK for x in now[:10])
                            if not still and not traded:
                                dirn = -1 if side == "bid" else 1
                                events.append({"pat": "spoof", "dt": dt, "i": i,
                                               "dirn": dirn, "entry": tape[i][1],
                                               "cvd": cvd, "depth": d5s(bids, asks)})
                d5_now = sum(s for _, s in bids[:5]) + sum(s for _, s in asks[:5])
                d5_prev = sum(s for _, s in pb[:5]) + sum(s for _, s in pa[:5])
                if d5_prev > 0:
                    chg = d5_now / d5_prev
                    if chg >= 1.8:
                        dirn = -1 if (sum(s for _, s in bids[:5]) <
                                      sum(s for _, s in asks[:5])) else 1
                        events.append({"pat": "stack", "dt": dt, "i": i,
                                       "dirn": dirn, "entry": tape[i][1],
                                       "cvd": cvd, "depth": d5_now})
                    elif chg <= 0.5:
                        dirn = 1 if cvd > 0 else -1
                        events.append({"pat": "flush", "dt": dt, "i": i,
                                       "dirn": dirn, "entry": tape[i][1],
                                       "cvd": cvd, "depth": d5_now})
            prev_ob = cur
            ob_i += 1

        w2 = [x for x in tape_window if (dt - x[0]).total_seconds() <= 2.0]
        if len(w2) >= 5:
            v = sum(x[1] for x in w2)
            speed_hist.append(v)
            if v >= 800 and len(speed_hist) > 100:
                med_v = statistics.median(speed_hist)
                if v >= 3 * med_v:
                    last_sweep = next((e for e in reversed(events)
                                       if e["pat"] == "sweep"), None)
                    if not last_sweep or (dt - last_sweep["dt"]).total_seconds() > 10:
                        events.append({"pat": "sweep", "dt": dt, "i": i,
                                       "dirn": d, "entry": price,
                                       "cvd": cvd, "depth": 0})

    # дебаунс стека/флаша: не чаще раза в 15с на класс
    filtered = []
    last_t = {}
    for e in events:
        k = e["pat"]
        if k in ("stack", "flush"):
            if k in last_t and (e["dt"] - last_t[k]).total_seconds() < 15:
                continue
            last_t[k] = e["dt"]
        filtered.append(e)
    return tape, filtered


def d5s(bids, asks):
    return sum(s for _, s in bids[:5]) + sum(s for _, s in asks[:5])


def main():
    tape, events = build_events()
    print(f"событий: {len(events)}")
    by_class = {}
    for e in events:
        by_class.setdefault(e["pat"], []).append(e)

    # сэмпл: резолвим исходы и фильтруем уникальные
    sample = []
    for pat, es in by_class.items():
        random.seed(42)
        es_sorted = sorted(es, key=lambda e: e["dt"])   # хронологически!
        cnt = 0
        seen_t = None
        for e in es_sorted:
            if cnt >= MAX_PER_CLASS:
                break
            if seen_t and (e["dt"] - seen_t).total_seconds() < 120:
                continue
            r = resolve(tape, e["i"], e["dirn"], e["entry"])
            e.update(r)
            sample.append(e)
            seen_t = e["dt"]
            cnt += 1
    print(f"сэмпл для Jev: {len(sample)}")

    # --- спрашиваем Jev ---
    t0 = time.time()
    n_allow = 0
    for k, e in enumerate(sample):
        doc = (f"Symbol SBER@MISX t={e['dt'].strftime('%H:%M:%S')}. "
               f"Pattern: {e['pat']}. Direction: {'long' if e['dirn'] > 0 else 'short'}. "
               f"Day CVD: {e['cvd']:+.0f} lots. Depth top5: {e['depth']:.0f}. "
               f"Entry: {e['entry']:.2f} limit post-only. TP/SL 5 ticks.")
        try:
            e["jev_allow"] = ask_jev(doc, e["pat"])
        except Exception as ex:
            e["jev_allow"] = None
            e["jev_err"] = str(ex)[:60]
        n_allow += 1
        if (k + 1) % 25 == 0:
            el = time.time() - t0
            print(f"  {k+1}/{len(sample)} jev ({el:.0f}с)", flush=True)
    ok = [e for e in sample if e.get("jev_allow") is not None]
    print(f"Jev ответил: {len(ok)}/{len(sample)}")

    # --- сводка A/B/C/D ---
    def stats(es):
        n = len(es)
        pnl = sum(e["pnl"] for e in es)
        win = sum(1 for e in es if e["pnl"] > 0) / n if n else 0
        return n, pnl, win

    A = ok
    B = [e for e in ok if e["jev_allow"]]
    random.seed(1)
    n_b = len(B)
    C = random.sample(ok, n_b)   # случайная селекция того же размера
    D = [e for e in ok if (e["cvd"] * e["dirn"] > 0)]

    print(f"\n{'Вариант':22s} {'n':>5} {'P/L':>9} {'win':>6}")
    for name, es in (("A: без гейта", A), ("B: Jev allow", B),
                     ("C: случайный (контроль)", C), ("D: cvd в сторону", D)):
        n, pnl, win = stats(es)
        print(f"{name:22s} {n:>5} {pnl:>+8.2f}₽ {win*100:>5.0f}%")

    # по классам: Jev allow rate и P/L
    print("\nПо классам (n, allow%, P/L всё, P/L allow):")
    for pat in sorted({e["pat"] for e in ok}):
        es = [e for e in ok if e["pat"] == pat]
        al = [e for e in es if e["jev_allow"]]
        print(f"  {pat:8s}: n={len(es):>3} allow {len(al)/len(es)*100:>3.0f}%  "
              f"P/L {sum(e['pnl'] for e in es):>+7.2f} -> allow {sum(e['pnl'] for e in al):>+7.2f}")

    with open(os.path.join(HERE, "research_jev_gate_results.json"), "w") as f:
        json.dump([{k: (str(v) if isinstance(v, datetime) else v)
                    for k, v in e.items()} for e in ok], f,
                  ensure_ascii=False, indent=1)
    print("saved research_jev_gate_results.json")


if __name__ == "__main__":
    main()
