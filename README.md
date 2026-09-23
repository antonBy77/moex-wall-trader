# MOEX Wall Trader

Торговый робот для Мосбиржи (данные Finam Trade API): детект **стен** в стакане,
отслеживание их жизненного цикла и торговля от них, с **AI-гейтом** (Laya / Jev-совместимый)
поверх сигналов. Порт идеи [jev-trader](https://github.com/jarrodwatts/jev-trader)
(post-only лимитки внутрь спреда, честная симуляция филлов) на рынок MOEX.

> **DRY_RUN only.** Боевых ордеров нет — лимитки виртуальные, филлы симулируются
> по реальным принтам ленты (post-only честно: bid-заявку бьёт только sell-принт).

## Архитектура

```
feed.py            FinamPy gRPC: стакан (40 уровней) + лента (1000 сделок, side) раз в 2с
walls.py           WallTracker: появление/удержание/съедание/снятие стен
                   Стена = уровень >= K× среднего соседних уровней (K=3, min 3000 лотов)
                   Сигналы: rejection (отбой от стены), breakout (стену съели),
                            pull (стену сняли без боёв)
laya_gate.py       AI-гейт: HTTP на laya-serve (Jev-совместимый /v1/systemone).
                   Laya (ModernBERT 421M, RLCD-калибровка) даёт allow/block/escalate
                   + риск-скор. Fallback на стеновой сигнал при падении сервера.
trader.py          Цикл: read book -> decide -> виртуальная post-only лимитка
                   на тик внутрь касания -> fill по принтам -> нетто PnL, SL/TP
laya_wall_trader.py То же + Laya-гейт
backtest_redis.py  Бэктест по истории из Redis (finam:trades / finam:ob / finam:dhist)
stats_events.py    Статистика прогона из events.jsonl
viz_report.py      PNG-отчёт: equity, филлы, цены, позиция
test_walls.py      Юнит-тесты (unittest)
```

## Запуск

```bash
export FINAM_TOKEN="твой токен Finam Trade API"

# данные + торговля без гейта (mock)
python3 wall_trader.py --blocks 150

# с Laya-гейтом: поднять сервер (один раз, в торговые часы)
cd laya_test && LAYA_DEVICE=cpu LAYA_PRELOAD=1 LAYA_PORT=8015 ./laya-venv/bin/laya-serve &
cd .. && python3 laya_wall_trader.py --blocks 150

# статистика и отчёт
python3 stats_events.py events.jsonl
python3 viz_report.py events.jsonl report.png

# бэктест по Redis-истории
python3 backtest_redis.py --symbols SBER@MISX,GAZP@MISX

# тесты
FINAM_TOKEN=x python3 test_walls.py
```

## Сигналы стен

| Событие | Что случилось | Действие |
|---|---|---|
| `rejection` | Цена тестировала стену, боевые принты, стена держится | Против импульса (отбой) |
| `breakout` | Стену ели (сброс ≥40% объёма при принтах) | По импульсу (пробой) |
| `pull` | Стену сняли без боевых принтов | Против прежней стороны (спуфинг) |

Bid-стена: rejection→BUY, breakout/pull→SELL. Ask-стена — зеркально.

## Результаты живых прогонов (2026-09-23, SBER@MISX, DRY_RUN)

| Прогон | Циклы | Котировок | Филлов | PnL | Комментарий |
|---|---|---|---|---|---|
| wall-v1 (без гейта) | 120 | 86 | 55 | +0.27 ₽ | ask-стена @278.09, 45.9К лотов, отбой отработан |
| laya-gate | 90 | 4 | 2 | +0.01 ₽ | гейт зарубил 25/26 сигналов (P(block)≈0.45); рынок падал — гейт прав |

Бэктест по лентам Redis (200 сделок/тикер, momentum-вход, комиссия 0.035%):
стратегия **без стенового фильтра убыточна** на всех 4 тикерах — входить по
голому CVD-импульсу нельзя, нужен именно стеновой контекст. Это подтверждает
выбор стратегии.

## Требования

- Python 3.11+, токен Finam Trade API
- `pip install FinamPy grpcio protobuf keyring redis matplotlib` (или venv)
- Для AI-гейта: [laya](https://huggingface.co/convaiinnovations/laya) (`pip install "laya[serve]"`)
- Redis (опционально, для бэктеста по истории)

## Часы работы

MOEX: будни 10:00–23:50 MSK, клиринги 14:00–14:05 и 18:45–19:00.
laya-serve держать поднятым только в торговые часы (см. cron/systemd-таймер).

## Дисклеймер

Экспериментальный исследовательский код. Не инвестиционная рекомендация.
Не подключён к боевым деньгам.
