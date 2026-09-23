"""MOEX spread harvester (jev-trader port) — config."""
import os

# Finam Trade API
FINAM_TOKEN = os.environ["FINAM_TOKEN"]  # секрет только через окружение
ACCOUNT_ID = os.environ.get("FINAM_ACCOUNT", "")  # не публикуем счёт

# Instrument
SYMBOL = "SBER@MISX"
TICK = 0.01          # шаг цены
LOT = 10             # лот (уточняется при старте из спеки)

# Loop
LOOP_SEC = 2.0       # период цикла (Monad 300ms тут невозможен; 2с по стакану)
HORIZON = 60         # "блоков" в горизонте прогноза (~2 мин)

# Position / risk (dry-run accounting)
MAX_POS = 10         # максимум лотов в позиции
CAPITAL_NOTE = "dry-run: виртуальные лимитки, филлы по реальным принтам"

# Storage
LOG_PATH = os.path.expanduser("~/.hermes/scripts-dev/moex-spread/events.jsonl")

# Model: "mock" (imbalance+cvd momentum) | "jev" (через OpenRouter, TODO)
MODEL = os.environ.get("SPREAD_MODEL", "mock")
DRY_RUN = True
