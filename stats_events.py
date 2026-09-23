#!/usr/bin/env python3
"""Анализ events.jsonl — статистика живых прогонов wall/laya трейдера."""
import json, sys
from collections import Counter
from datetime import datetime

path = sys.argv[1] if len(sys.argv) > 1 else "events.jsonl"
evs = [json.loads(l) for l in open(path) if l.strip()]
if not evs:
    print("нет событий"); sys.exit(0)

fills = [e for e in evs if e["type"] == "fill"]
closes = [e for e in evs if e["type"] in ("SL", "TP")]
quotes = [e for e in evs if e["type"] == "quote"]

print(f"=== Статистика прогона ({len(evs)} событий) ===")
print(f"Период: {evs[0]['ts']} .. {evs[-1]['ts']}")
print(f"Котировок: {len(quotes)}, филлов: {len(fills)}, закрытий: {len(closes)}")

if closes:
    pnls = [c["realized"] for c in closes]
    print(f"\nЗакрытия по типам: {dict(Counter(c['type'] for c in closes))}")
    print(f"Реализованный PnL на закрытиях: {pnls}")
wins = sum(1 for c in closes if c["realized"] > 0)
if closes:
    print(f"Win rate закрытий: {wins}/{len(closes)} = {wins/len(closes):.0%}")

sides = Counter(f["side"] for f in fills)
print(f"\nФиллы по сторонам: {dict(sides)}")
if quotes:
    acts = Counter(q["action"] for q in quotes)
    print(f"Котировки по сигналам: {dict(acts)}")
    scores = [q["score"] for q in quotes]
    print(f"Score сигналов: min={min(scores):.2f} max={max(scores):.2f} avg={sum(scores)/len(scores):.2f}")

# длительность удержания позиции: филл->филл противоположной стороны
opens = [f for f in fills]
print(f"\nИтого событий в файле: {len(evs)}")
