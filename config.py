import os


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = _i("ADMIN_CHAT_ID", 0)

# --- Хранилище (на Railway подключи Volume и укажи DB_PATH=/data/bot.db) ---
DB_PATH = os.getenv("DB_PATH", "bot.db")

# --- Сканер ---
SCAN_INTERVAL = _i("SCAN_INTERVAL", 60)          # сек между циклами
MAX_DEEP_CHECKS = _i("MAX_DEEP_CHECKS", 12)      # сколько монет из watchlist глубоко проверяем за цикл

# --- Фильтр кандидатов (этап 1) ---
PUMP_24H_MIN = _f("PUMP_24H_MIN", 51.0)          # % роста за 24ч
MIN_TURNOVER = _f("MIN_TURNOVER", 10_000_000)    # оборот за 24ч, $
MIN_AGE_DAYS = _i("MIN_AGE_DAYS", 30)            # возраст контракта
MAX_RETRACE_FROM_HIGH = _f("MAX_RETRACE_FROM_HIGH", 35.0)  # % отката от хая, дальше поезд ушёл
MIN_PRICE = _f("MIN_PRICE", 0.0000001)

# --- Триггер входа (этап 2) ---
TRIGGER_SCORE = _f("TRIGGER_SCORE", 7.0)         # из 12 баллов
MIN_MIN_SINCE_PEAK = _i("MIN_MIN_SINCE_PEAK", 45)   # мин с момента 24ч-хая
NEW_HIGH_VETO_MIN = _i("NEW_HIGH_VETO_MIN", 20)     # если хай обновлён за это время — вето
MAX_RISK_PCT = _f("MAX_RISK_PCT", 7.0)           # если стоп дальше — сигнал не даём
COOLDOWN_HOURS = _i("COOLDOWN_HOURS", 8)         # пауза по монете после сигнала

# --- Риск-калькулятор в карточке сигнала ---
DEPOSIT_USD = _f("DEPOSIT_USD", 1000)
RISK_PCT = _f("RISK_PCT", 0.5)                   # % депозита на сделку

# --- Сопровождение сигналов ---
TRACK_HOURS = _i("TRACK_HOURS", 24)              # сколько часов ведём сигнал
NOTIFY_CANDIDATES = os.getenv("NOTIFY_CANDIDATES", "1") == "1"

SETTABLE = {
    "PUMP_24H_MIN": (PUMP_24H_MIN, 1.0, "%  рост за 24ч"),
    "MIN_TURNOVER": (MIN_TURNOVER, 2_000_000, "$  оборот 24ч"),
    "TRIGGER_SCORE": (TRIGGER_SCORE, 0.5, "б  порог входа"),
    "MIN_MIN_SINCE_PEAK": (MIN_MIN_SINCE_PEAK, 15, "м  после пика"),
    "COOLDOWN_HOURS": (COOLDOWN_HOURS, 2, "ч  кулдаун"),
    "RISK_PCT": (RISK_PCT, 0.25, "%  риск на сделку"),
}
