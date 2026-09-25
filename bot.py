import asyncio
import contextlib
import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

import config as C
import storage as db
from bybit import Bybit, Ticker
from strategy import Candidate, Signal, evaluate, is_candidate

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

bot = Bot(C.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

PRESETS = {
    "aggr": ("🎯 Агрессив", {"PUMP_24H_MIN": 40, "TRIGGER_SCORE": 5.5,
                             "MIN_MIN_SINCE_PEAK": 30, "MIN_TURNOVER": 5_000_000,
                             "COOLDOWN_HOURS": 4}),
    "bal":  ("⚖️ Баланс",  {"PUMP_24H_MIN": 51, "TRIGGER_SCORE": 7.0,
                             "MIN_MIN_SINCE_PEAK": 45, "MIN_TURNOVER": 10_000_000,
                             "COOLDOWN_HOURS": 8}),
    "cons": ("🛡 Консерва", {"PUMP_24H_MIN": 70, "TRIGGER_SCORE": 8.5,
                             "MIN_MIN_SINCE_PEAK": 90, "MIN_TURNOVER": 20_000_000,
                             "COOLDOWN_HOURS": 12}),
}


class State:
    started = time.time()
    paused = False
    symbols = 0
    last_scan = 0.0
    scans = 0
    errors = 0
    watchlist: dict[str, Candidate] = {}
    tickers: dict[str, Ticker] = {}
    signals: dict[int, Signal] = {}
    checks: dict[str, tuple] = {}     # symbol -> (score, comps, why, ts)


S = State()


# ================================================================ утилиты
def fnum(x: float) -> str:
    if x == 0:
        return "0"
    if x >= 1000:
        return f"{x:,.2f}".replace(",", " ")
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.8f}".rstrip("0")


def money(x: float) -> str:
    if x >= 1_000_000_000:
        return f"${x / 1e9:.2f}B"
    if x >= 1_000_000:
        return f"${x / 1e6:.1f}M"
    if x >= 1000:
        return f"${x / 1e3:.0f}K"
    return f"${x:.0f}"


def bar(v: float, mx: float, cells: int = 10) -> str:
    filled = max(0, min(cells, round(v / mx * cells))) if mx else 0
    return "▰" * filled + "▱" * (cells - filled)


def ago(ts: float) -> str:
    s = int(time.time() - ts)
    if s < 60:
        return f"{s}с"
    if s < 3600:
        return f"{s // 60}м"
    if s < 86400:
        return f"{s // 3600}ч {s % 3600 // 60}м"
    return f"{s // 86400}д"


def admin_only(uid: int) -> bool:
    return C.ADMIN_CHAT_ID == 0 or uid == C.ADMIN_CHAT_ID


async def safe_edit(msg: Message, text: str, kb: InlineKeyboardMarkup | None = None):
    with contextlib.suppress(TelegramBadRequest):
        await msg.edit_text(text, reply_markup=kb)


# ================================================================ клавиатуры
def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📡 Статус"), KeyboardButton(text="👀 Наблюдение")],
            [KeyboardButton(text="🚀 Топ 24ч"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="🗂 Сигналы"), KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True, is_persistent=True, input_field_placeholder="Выбери раздел")


def nav(*extra_rows, back: str = "main") -> InlineKeyboardMarkup:
    rows = [list(r) for r in extra_rows if r]
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"nav:{back}"),
        InlineKeyboardButton(text="⬅️ Меню", callback_data="nav:main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📡 Статус", callback_data="nav:status"),
         InlineKeyboardButton(text="👀 Наблюдение", callback_data="nav:watch")],
        [InlineKeyboardButton(text="🚀 Топ 24ч", callback_data="nav:top"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="nav:stats")],
        [InlineKeyboardButton(text="🗂 Сигналы", callback_data="nav:signals"),
         InlineKeyboardButton(text="🔕 Муты", callback_data="nav:mutes")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="nav:settings"),
         InlineKeyboardButton(text="❓ Как работает", callback_data="nav:help")],
        [InlineKeyboardButton(
            text="⏸ Поставить на паузу" if not S.paused else "▶️ Запустить сканер",
            callback_data="pause:toggle")],
    ])


# ================================================================ экраны
def screen_main() -> str:
    st = db.stats(30)
    dot = "⏸ <b>Пауза</b>" if S.paused else "🟢 <b>Сканер работает</b>"
    return (
        "🤖 <b>PUMP-SHORT SCANNER</b>\n"
        "<i>Bybit · шорт после аномального роста</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{dot}   ·   скан {ago(S.last_scan) if S.last_scan else '—'} назад\n\n"
        f"🔍 Монет в скане:  <b>{S.symbols}</b>\n"
        f"👀 Под наблюдением:  <b>{len(S.watchlist)}</b>\n"
        f"🔴 Сигналов за 24ч:  <b>{db.signals_today()}</b>\n"
        f"📂 Открытых сделок:  <b>{st['open']}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Порог: <b>+{C.PUMP_24H_MIN:g}%</b> за 24ч · вход от <b>{C.TRIGGER_SCORE:g}/12</b> баллов"
    )


def screen_status() -> str:
    up = int(time.time() - S.started)
    st = db.stats(30)
    return (
        "📡 <b>СТАТУС СКАНЕРА</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{'⏸ на паузе' if S.paused else '🟢 работает'}\n"
        f"Аптайм: <b>{up // 3600}ч {up % 3600 // 60}м</b>\n"
        f"Циклов: <b>{S.scans}</b>  ·  ошибок: <b>{S.errors}</b>\n"
        f"Последний скан: <b>{ago(S.last_scan) if S.last_scan else '—'} назад</b>\n"
        f"Контрактов: <b>{S.symbols}</b>  ·  в наблюдении: <b>{len(S.watchlist)}</b>\n"
        f"Открытых сигналов: <b>{st['open']}</b>\n\n"
        "<b>Текущие фильтры</b>\n"
        f"<pre>рост 24ч     ≥ {C.PUMP_24H_MIN:g}%\n"
        f"оборот 24ч   ≥ {money(C.MIN_TURNOVER)}\n"
        f"возраст      ≥ {C.MIN_AGE_DAYS} дн\n"
        f"балл входа   ≥ {C.TRIGGER_SCORE:g}/12\n"
        f"после пика   ≥ {C.MIN_MIN_SINCE_PEAK} мин\n"
        f"кулдаун        {C.COOLDOWN_HOURS} ч\n"
        f"макс. стоп     {C.MAX_RISK_PCT:g}%\n"
        f"риск/сделка    {C.RISK_PCT:g}% от ${C.DEPOSIT_USD:.0f}</pre>"
    )


def screen_watch() -> tuple[str, InlineKeyboardMarkup]:
    if not S.watchlist:
        return ("👀 <b>НАБЛЮДЕНИЕ</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                "Пусто — аномальных пампов сейчас нет.\n\n"
                f"<i>Ищу рост от +{C.PUMP_24H_MIN:g}% за 24ч при обороте от "
                f"{money(C.MIN_TURNOVER)}.</i>", nav(back="watch"))
    rows = sorted(S.watchlist.values(), key=lambda c: -c.score)
    out = ["👀 <b>НАБЛЮДЕНИЕ</b>", "━━━━━━━━━━━━━━━━━━━━"]
    btns = []
    for c in rows[:12]:
        ready = "🔥" if c.score >= C.TRIGGER_SCORE - 1 else ("🟡" if c.score >= 4 else "⚪️")
        out.append(f"{ready} <b>{c.symbol}</b>  +{c.pcnt24:.0f}%  ·  {money(c.turnover)}\n"
                   f"    {bar(c.score, 12, 6)} {c.score:.1f}/12  ·  <i>{c.note}</i>")
        btns.append(InlineKeyboardButton(text=f"{ready} {c.symbol.replace('USDT', '')}",
                                        callback_data=f"w:{c.symbol}"))
    grid = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    return "\n".join(out), nav(*grid, back="watch")


def screen_coin(sym: str) -> tuple[str, InlineKeyboardMarkup]:
    c = S.watchlist.get(sym)
    t = S.tickers.get(sym)
    if not c or not t:
        return "Монета выбыла из наблюдения.", nav(back="watch")
    chk = S.checks.get(sym)
    out = [f"🔎 <b>{sym}</b>", "━━━━━━━━━━━━━━━━━━━━",
           f"Цена: <b>{fnum(t.last)}</b>",
           f"Рост 24ч: <b>+{t.pcnt24:.1f}%</b>  ·  оборот {money(t.turnover24)}",
           f"Хай 24ч: {fnum(t.high24)}  ·  откат от хая <b>{c.from_high:.1f}%</b>",
           f"Funding: <b>{t.funding * 100:+.4f}%</b>",
           "",
           f"Готовность: <b>{c.score:.1f}/12</b>  {bar(c.score, 12)}",
           f"Статус: <i>{c.note}</i>", ""]
    if chk and chk[1]:
        for comp in chk[1]:
            if comp.weight == 0:
                out.append(f"{'✅' if comp.ok else '⛔️'} <b>{comp.name}</b> — <i>{comp.detail}</i>")
            else:
                out.append(f"{'✅' if comp.ok else '➖'} {comp.name} <code>{comp.weight:g}б</code>\n"
                           f"     <i>{comp.detail}</i>")
        out.append(f"\n<i>проверено {ago(chk[3])} назад</i>")
    else:
        out.append("<i>Глубокая проверка ещё не проходила.</i>")
    kb = nav([InlineKeyboardButton(text="📉 Открыть на Bybit",
                                   url=f"https://www.bybit.com/trade/usdt/{sym}"),
              InlineKeyboardButton(text="🔕 Мут 8ч", callback_data=f"mute:{sym}")],
             [InlineKeyboardButton(text="⬅️ К списку", callback_data="nav:watch")],
             back=f"w:{sym}")
    return "\n".join(out), kb


def screen_top() -> str:
    if not S.tickers:
        return "Данных нет, идёт первый скан."
    rows = sorted(S.tickers.values(), key=lambda t: -t.pcnt24)[:12]
    out = ["🚀 <b>ТОП РОСТА ЗА 24 ЧАСА</b>", "━━━━━━━━━━━━━━━━━━━━", "<pre>"]
    for i, t in enumerate(rows, 1):
        mark = "🔴" if t.pcnt24 >= C.PUMP_24H_MIN else "  "
        out.append(f"{i:>2}.{mark} {t.symbol.replace('USDT', ''):<10}{t.pcnt24:>7.1f}%  "
                   f"{money(t.turnover24):>7}")
    out.append("</pre>")
    out.append(f"<i>🔴 — проходит порог +{C.PUMP_24H_MIN:g}%</i>")
    return "\n".join(out)


def screen_stats(days: int = 30) -> tuple[str, InlineKeyboardMarkup]:
    s = db.stats(days)
    wr = s["winrate"]
    verdict = ("🟢 стратегия окупает риск" if wr >= 60 and s["closed"] >= 20 else
               "🟡 данных мало, продолжай наблюдение" if s["closed"] < 20 else
               "🔴 хвостовой риск не окупается")
    out = [f"📊 <b>СТАТИСТИКА · {days} дн</b>", "━━━━━━━━━━━━━━━━━━━━",
           f"<pre>Сигналов      {s['total']}\n"
           f"Открыто       {s['open']}\n"
           f"Закрыто       {s['closed']}\n"
           f"  🎯 TP       {s['wins']}\n"
           f"  🛑 SL       {s['losses']}\n"
           f"  ⌛ истекло  {s['expired']}</pre>",
           f"Winrate: <b>{wr:.1f}%</b>  {bar(wr, 100)}",
           f"Средний ход в плюс: <b>+{s['avg_best']:.2f}%</b>",
           f"Средняя просадка: <b>{s['avg_worst']:.2f}%</b>",
           "", f"Вывод: {verdict}"]
    kb = nav([InlineKeyboardButton(text="7 дн", callback_data="stats:7"),
              InlineKeyboardButton(text="30 дн", callback_data="stats:30"),
              InlineKeyboardButton(text="Всё", callback_data="stats:3650")],
             [InlineKeyboardButton(text="🗂 Список сигналов", callback_data="nav:signals")],
             back=f"stats:{days}")
    return "\n".join(out), kb


ICON = {"open": "⏳", "tp1": "🎯", "tp2": "🎯", "tp3": "🏁", "sl": "🛑", "expired": "⌛"}


def screen_signals() -> tuple[str, InlineKeyboardMarkup]:
    rows = db.recent_signals(10)
    if not rows:
        return ("🗂 <b>СИГНАЛЫ</b>\n━━━━━━━━━━━━━━━━━━━━\nПока ни одного сигнала.",
                nav(back="signals"))
    out = ["🗂 <b>ПОСЛЕДНИЕ СИГНАЛЫ</b>", "━━━━━━━━━━━━━━━━━━━━"]
    btns = []
    for r in rows:
        mark = {1: "✅", -1: "❌"}.get(r["taken"], "")
        out.append(f"{ICON.get(r['status'], '•')} <b>{r['symbol']}</b> "
                   f"<code>#{r['id']}</code> {mark}\n"
                   f"    {ago(r['ts'])} назад · вход {fnum(r['entry'])} · "
                   f"балл {r['score']:.1f} · макс {r['best']:+.1f}%")
        btns.append(InlineKeyboardButton(text=f"{ICON.get(r['status'], '•')} #{r['id']}",
                                        callback_data=f"sig:{r['id']}"))
    grid = [btns[i:i + 4] for i in range(0, len(btns), 4)]
    return "\n".join(out), nav(*grid, back="signals")


def screen_signal(sid: int) -> tuple[str, InlineKeyboardMarkup]:
    r = db.get_signal(sid)
    if not r:
        return "Сигнал не найден.", nav(back="signals")
    t = S.tickers.get(r["symbol"])
    now_pnl = (r["entry"] - t.last) / r["entry"] * 100 if t else None
    out = [f"{ICON.get(r['status'], '•')} <b>{r['symbol']}</b> · сигнал <code>#{sid}</code>",
           "━━━━━━━━━━━━━━━━━━━━",
           f"Статус: <b>{r['status'].upper()}</b>  ·  открыт {ago(r['ts'])} назад",
           f"Балл входа: <b>{r['score']:.1f}/12</b>  ·  рост был +{r['pcnt24']:.0f}%",
           "",
           f"<pre>Вход  {fnum(r['entry'])}\n"
           f"Стоп  {fnum(r['sl'])}\n"
           f"TP1   {fnum(r['tp1'])}\n"
           f"TP2   {fnum(r['tp2'])}\n"
           f"TP3   {fnum(r['tp3'])}</pre>",
           f"Максимум в плюс: <b>{r['best']:+.2f}%</b>",
           f"Максимальная просадка: <b>{r['worst']:+.2f}%</b>"]
    if now_pnl is not None and r["status"] == "open":
        out.append(f"Сейчас: <b>{now_pnl:+.2f}%</b>  (цена {fnum(t.last)})")
    if r["taken"] == 1:
        out.append("\n✅ Отмечен как взятый")
    elif r["taken"] == -1:
        out.append("\n❌ Отмечен как пропущенный")
    kb = nav([InlineKeyboardButton(text="📉 Bybit",
                                   url=f"https://www.bybit.com/trade/usdt/{r['symbol']}"),
              InlineKeyboardButton(text="✅ Взял", callback_data=f"took:{sid}"),
              InlineKeyboardButton(text="❌ Пропустил", callback_data=f"skip:{sid}")],
             [InlineKeyboardButton(text="⬅️ К списку", callback_data="nav:signals")],
             back=f"sig:{sid}")
    return "\n".join(out), kb


def screen_mutes() -> tuple[str, InlineKeyboardMarkup]:
    rows = db.muted_list()
    if not rows:
        return ("🔕 <b>МУТЫ</b>\n━━━━━━━━━━━━━━━━━━━━\nЗамученных монет нет.",
                nav(back="mutes"))
    out = ["🔕 <b>МУТЫ</b>", "━━━━━━━━━━━━━━━━━━━━"]
    btns = []
    for r in rows:
        left = int((r["until"] - time.time()) / 60)
        out.append(f"• <b>{r['symbol']}</b> — ещё {left // 60}ч {left % 60}м")
        btns.append(InlineKeyboardButton(text=f"🔔 {r['symbol'].replace('USDT', '')}",
                                        callback_data=f"unmute:{r['symbol']}"))
    grid = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    return "\n".join(out) + "\n\n<i>Нажми, чтобы снять мут.</i>", nav(*grid, back="mutes")


LABELS = {
    "PUMP_24H_MIN": "📈 Рост 24ч",
    "MIN_TURNOVER": "💵 Оборот",
    "TRIGGER_SCORE": "🎯 Балл входа",
    "MIN_MIN_SINCE_PEAK": "⏱ После пика",
    "COOLDOWN_HOURS": "🧊 Кулдаун",
    "RISK_PCT": "🛡 Риск/сделка",
}


def settings_kb() -> InlineKeyboardMarkup:
    rows = []
    for key, (_, step, _) in C.SETTABLE.items():
        val = getattr(C, key)
        shown = (money(val) if key == "MIN_TURNOVER" else
                 f"{val:g}%" if key in ("PUMP_24H_MIN", "RISK_PCT") else
                 f"{val:g}м" if key == "MIN_MIN_SINCE_PEAK" else
                 f"{val:g}ч" if key == "COOLDOWN_HOURS" else f"{val:g}")
        rows.append([
            InlineKeyboardButton(text="➖", callback_data=f"set:{key}:-"),
            InlineKeyboardButton(text=f"{LABELS[key]}  {shown}", callback_data="noop"),
            InlineKeyboardButton(text="➕", callback_data=f"set:{key}:+"),
        ])
    rows.append([InlineKeyboardButton(text=v[0], callback_data=f"preset:{k}")
                 for k, v in PRESETS.items()])
    rows.append([
        InlineKeyboardButton(
            text=("🔔 Кандидаты: вкл" if C.NOTIFY_CANDIDATES else "🔕 Кандидаты: выкл"),
            callback_data="toggle:notify"),
        InlineKeyboardButton(text="♻️ Сброс", callback_data="preset:reset"),
    ])
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data="nav:settings"),
        InlineKeyboardButton(text="⬅️ Меню", callback_data="nav:main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def screen_settings() -> str:
    return ("⚙️ <b>НАСТРОЙКИ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Меняются на лету и сохраняются в базе — редеплой не нужен.\n\n"
            "<b>Рост 24ч</b> — порог аномальности\n"
            "<b>Балл входа</b> — строгость подтверждения (из 12)\n"
            "<b>После пика</b> — сколько ждать, пока импульс остынет\n"
            "<b>Кулдаун</b> — пауза по монете после сигнала\n"
            "<b>Риск</b> — % депозита, от него считается размер позиции")


HELP = (
    "❓ <b>КАК РАБОТАЕТ БОТ</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "<b>Шаг 1 · Кандидат</b>\n"
    "Раз в минуту снимок всего рынка Bybit. В наблюдение попадают монеты с ростом "
    f"≥{C.PUMP_24H_MIN:g}% за 24ч, оборотом ≥{money(C.MIN_TURNOVER)}, "
    f"старше {C.MIN_AGE_DAYS} дней и откатом от хая не более 35%.\n\n"
    "<b>Шаг 2 · Вето</b>\n"
    "Новый хай за 20 минут, меньше "
    f"{C.MIN_MIN_SINCE_PEAK} минут после пика или кулдаун — вход запрещён.\n\n"
    "<b>Шаг 3 · Триггер</b>\n"
    "Обязателен пробой последнего swing-low на 5m. Без него сигнала нет никогда.\n\n"
    "<b>Шаг 4 · Балл из 12</b>\n"
    "нижний максимум · климакс объёма → затухание · остывший RSI 15m · "
    "положительный funding · разгрузка открытого интереса · перерастянутость к EMA · "
    "тень на вершине · время без нового хая · цена ниже VWAP 4ч\n\n"
    "<b>Шаг 5 · Уровни</b>\n"
    "Стоп — от локального хая и ATR, дальше "
    f"{C.MAX_RISK_PCT:g}% сигнал отменяется. TP1=1R, TP2=2R, TP3 — Фибо 38.2%.\n\n"
    "<b>Шаг 6 · Сопровождение</b>\n"
    "Сутки ведёт сделку, фиксирует максимум в плюс и просадку, присылает итог.\n\n"
    "⚠️ Прибыль ограничена, убыток при сквизе — нет. Стоп обязателен, "
    "усреднение запрещено."
)


# ================================================================ команды
@dp.message(Command("start", "menu", "help"))
async def cmd_start(m: Message):
    if not admin_only(m.from_user.id):
        return
    await m.answer(f"Твой chat_id: <code>{m.chat.id}</code>", reply_markup=main_kb())
    await m.answer(screen_main(), reply_markup=menu_kb())


@dp.message(F.text == "📡 Статус")
async def btn_status(m: Message):
    if admin_only(m.from_user.id):
        await m.answer(screen_status(), reply_markup=nav(back="status"))


@dp.message(F.text == "👀 Наблюдение")
async def btn_watch(m: Message):
    if admin_only(m.from_user.id):
        txt, kb = screen_watch()
        await m.answer(txt, reply_markup=kb)


@dp.message(F.text == "🚀 Топ 24ч")
async def btn_top(m: Message):
    if admin_only(m.from_user.id):
        await m.answer(screen_top(), reply_markup=nav(back="top"))


@dp.message(F.text == "📊 Статистика")
async def btn_stats(m: Message):
    if admin_only(m.from_user.id):
        txt, kb = screen_stats(30)
        await m.answer(txt, reply_markup=kb)


@dp.message(F.text == "🗂 Сигналы")
async def btn_signals(m: Message):
    if admin_only(m.from_user.id):
        txt, kb = screen_signals()
        await m.answer(txt, reply_markup=kb)


@dp.message(F.text == "⚙️ Настройки")
async def btn_settings(m: Message):
    if admin_only(m.from_user.id):
        await m.answer(screen_settings(), reply_markup=settings_kb())


@dp.message(Command("status"))
async def cmd_status(m: Message):
    await btn_status(m)


@dp.message(Command("watchlist"))
async def cmd_watch(m: Message):
    await btn_watch(m)


@dp.message(Command("top"))
async def cmd_top(m: Message):
    await btn_top(m)


@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    await btn_stats(m)


@dp.message(Command("settings"))
async def cmd_settings(m: Message):
    await btn_settings(m)


@dp.message(Command("pause"))
async def cmd_pause(m: Message):
    if admin_only(m.from_user.id):
        S.paused = True
        await m.answer("⏸ Сканер на паузе.", reply_markup=menu_kb())


@dp.message(Command("resume"))
async def cmd_resume(m: Message):
    if admin_only(m.from_user.id):
        S.paused = False
        await m.answer("🟢 Сканер запущен.", reply_markup=menu_kb())


# ================================================================ колбэки
@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


@dp.callback_query(F.data.startswith("nav:"))
async def cb_nav(c: CallbackQuery):
    where = c.data.split(":")[1]
    if where == "main":
        await safe_edit(c.message, screen_main(), menu_kb())
    elif where == "status":
        await safe_edit(c.message, screen_status(), nav(back="status"))
    elif where == "watch":
        txt, kb = screen_watch()
        await safe_edit(c.message, txt, kb)
    elif where == "top":
        await safe_edit(c.message, screen_top(), nav(back="top"))
    elif where == "stats":
        txt, kb = screen_stats(30)
        await safe_edit(c.message, txt, kb)
    elif where == "signals":
        txt, kb = screen_signals()
        await safe_edit(c.message, txt, kb)
    elif where == "mutes":
        txt, kb = screen_mutes()
        await safe_edit(c.message, txt, kb)
    elif where == "settings":
        await safe_edit(c.message, screen_settings(), settings_kb())
    elif where == "help":
        await safe_edit(c.message, HELP, nav(back="help"))
    await c.answer()


@dp.callback_query(F.data.startswith("w:"))
async def cb_coin(c: CallbackQuery):
    txt, kb = screen_coin(c.data.split(":")[1])
    await safe_edit(c.message, txt, kb)
    await c.answer()


@dp.callback_query(F.data.startswith("sig:"))
async def cb_sig(c: CallbackQuery):
    txt, kb = screen_signal(int(c.data.split(":")[1]))
    await safe_edit(c.message, txt, kb)
    await c.answer()


@dp.callback_query(F.data.startswith("stats:"))
async def cb_stats(c: CallbackQuery):
    txt, kb = screen_stats(int(c.data.split(":")[1]))
    await safe_edit(c.message, txt, kb)
    await c.answer()


@dp.callback_query(F.data == "pause:toggle")
async def cb_pause(c: CallbackQuery):
    S.paused = not S.paused
    await safe_edit(c.message, screen_main(), menu_kb())
    await c.answer("⏸ Пауза" if S.paused else "🟢 Сканер запущен")


@dp.callback_query(F.data.startswith("set:"))
async def cb_set(c: CallbackQuery):
    _, key, sign = c.data.split(":")
    step = C.SETTABLE[key][1]
    val = max(step, getattr(C, key) + (step if sign == "+" else -step))
    db.save_setting(key, val)
    await safe_edit(c.message, screen_settings(), settings_kb())
    await c.answer(f"{LABELS[key]} → {val:g}")


@dp.callback_query(F.data.startswith("preset:"))
async def cb_preset(c: CallbackQuery):
    name = c.data.split(":")[1]
    if name == "reset":
        db.reset_settings()
        for key, (default, _, _) in C.SETTABLE.items():
            setattr(C, key, default)
        await c.answer("♻️ Сброшено к значениям из переменных окружения")
    else:
        title, values = PRESETS[name]
        for k, v in values.items():
            db.save_setting(k, v)
        await c.answer(f"{title} применён")
    await safe_edit(c.message, screen_settings(), settings_kb())


@dp.callback_query(F.data == "toggle:notify")
async def cb_notify(c: CallbackQuery):
    C.NOTIFY_CANDIDATES = not C.NOTIFY_CANDIDATES
    await safe_edit(c.message, screen_settings(), settings_kb())
    await c.answer("🔔 Уведомления о кандидатах включены" if C.NOTIFY_CANDIDATES
                   else "🔕 Уведомления о кандидатах выключены")


@dp.callback_query(F.data.startswith("mute:"))
async def cb_mute(c: CallbackQuery):
    sym = c.data.split(":")[1]
    db.mute(sym, 8)
    S.watchlist.pop(sym, None)
    await c.answer(f"🔕 {sym} — мут на 8 часов", show_alert=True)


@dp.callback_query(F.data.startswith("unmute:"))
async def cb_unmute(c: CallbackQuery):
    sym = c.data.split(":")[1]
    db.unmute(sym)
    txt, kb = screen_mutes()
    await safe_edit(c.message, txt, kb)
    await c.answer(f"🔔 {sym} снова в скане")


@dp.callback_query(F.data.startswith(("took:", "skip:")))
async def cb_taken(c: CallbackQuery):
    action, sid = c.data.split(":")
    db.update_signal(int(sid), taken=1 if action == "took" else -1)
    await c.answer("✅ Отмечено: взял" if action == "took" else "❌ Отмечено: пропустил")


@dp.callback_query(F.data.startswith("det:"))
async def cb_detail(c: CallbackQuery):
    sid = int(c.data.split(":")[1])
    s = S.signals.get(sid)
    if not s:
        txt, kb = screen_signal(sid)
        await c.message.answer(txt, reply_markup=kb)
        await c.answer()
        return
    txt = [f"🔎 <b>Полный разбор · {s.symbol}</b>", "━━━━━━━━━━━━━━━━━━━━"]
    for comp in s.components:
        mark = "✅" if comp.ok else ("⛔️" if comp.weight == 0 else "➖")
        txt.append(f"{mark} <b>{comp.name}</b> <code>{comp.weight:g}б</code>\n"
                   f"     <i>{comp.detail}</i>")
    txt.append(f"\nИтого: <b>{s.score:.1f}/{s.max_score:.0f}</b>  {bar(s.score, s.max_score)}")
    await c.message.answer("\n".join(txt))
    await c.answer()


# ================================================================ карточка сигнала
def signal_text(s: Signal, sid: int) -> str:
    r = s.sl - s.entry
    lines = [
        "🔴🔴🔴  <b>SHORT SIGNAL</b>  🔴🔴🔴",
        f"<b>{s.symbol}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📈 <b>+{s.pcnt24:.1f}%</b> за 24ч   ·   💵 {money(s.turnover)}",
        f"🎯 Готовность <b>{s.score:.1f}/{s.max_score:.0f}</b>  {bar(s.score, s.max_score)}",
        "",
        "<pre>"
        f"Вход   {fnum(s.entry)}\n"
        f"Ретест {fnum(s.retest)}   лучшая цена\n"
        f"Стоп   {fnum(s.sl)}   +{s.risk_pct:.2f}%\n"
        f"TP1    {fnum(s.tp1)}   −{(s.entry - s.tp1) / s.entry * 100:.2f}%  1R\n"
        f"TP2    {fnum(s.tp2)}   −{(s.entry - s.tp2) / s.entry * 100:.2f}%  2R\n"
        f"TP3    {fnum(s.tp3)}   −{(s.entry - s.tp3) / s.entry * 100:.2f}%  {(s.entry - s.tp3) / r:.1f}R"
        "</pre>",
    ]
    for c in s.components:
        if c.weight == 0:
            continue
        lines.append(f"{'✅' if c.ok else '➖'} {c.name} — <i>{c.detail}</i>")
    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏱ Пик {s.mins_since_peak // 60}ч {s.mins_since_peak % 60}м назад, хай не обновлялся",
        f"💰 Funding {s.funding * 100:+.4f}% — "
        f"{'платят шортам' if s.funding > 0 else 'платит шорт'}",
        f"📐 {s.qty:.4g} монет ≈ {money(s.notional)} "
        f"(риск {C.RISK_PCT:g}% от ${C.DEPOSIT_USD:.0f})",
        "",
        "⚠️ Стоп сразу. Без усреднений — памп может продолжиться.",
        f"<code>#{sid}</code>",
    ]
    return "\n".join(lines)


def signal_kb(sid: int, symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Разбор", callback_data=f"det:{sid}"),
         InlineKeyboardButton(text="📉 Bybit",
                              url=f"https://www.bybit.com/trade/usdt/{symbol}")],
        [InlineKeyboardButton(text="✅ Взял", callback_data=f"took:{sid}"),
         InlineKeyboardButton(text="❌ Пропустил", callback_data=f"skip:{sid}"),
         InlineKeyboardButton(text="🔕 Мут 8ч", callback_data=f"mute:{symbol}")],
        [InlineKeyboardButton(text="📈 Отслеживание", callback_data=f"sig:{sid}")],
    ])


def candidate_text(c: Candidate) -> str:
    return (f"👀 <b>{c.symbol}</b> — взял в наблюдение\n"
            f"+{c.pcnt24:.1f}% за 24ч · {money(c.turnover)} · откат от хая {c.from_high:.1f}%\n"
            f"<i>Вход не даю, жду подтверждения разворота.</i>")


def candidate_kb(sym: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔎 Детали", callback_data=f"w:{sym}"),
        InlineKeyboardButton(text="📉 Bybit",
                             url=f"https://www.bybit.com/trade/usdt/{sym}"),
        InlineKeyboardButton(text="🔕 Мут", callback_data=f"mute:{sym}"),
    ]])


# ================================================================ сканер
async def deep_check(api: Bybit, sym: str, t: Ticker):
    k5 = await api.klines(sym, "5", 120)
    k15 = await api.klines(sym, "15", 96)
    try:
        oi = await api.open_interest(sym, "5min", 100)
    except Exception:       # noqa: BLE001
        oi = []
    return evaluate(t, k5, k15, oi)


async def track_signals(tickers: dict[str, Ticker]):
    for row in db.open_signals():
        t = tickers.get(row["symbol"])
        if not t:
            continue
        entry, price = row["entry"], t.last
        pnl = (entry - price) / entry * 100
        upd = {"best": max(row["best"], pnl), "worst": min(row["worst"], pnl)}
        status = None
        if price >= row["sl"]:
            status = "sl"
        elif price <= row["tp3"]:
            status = "tp3"
        elif price <= row["tp2"]:
            status = "tp2"
        elif price <= row["tp1"]:
            status = "tp1"
        elif time.time() - row["ts"] > C.TRACK_HOURS * 3600:
            status = "expired"
        if status:
            upd |= {"status": status, "closed_ts": int(time.time())}
            await bot.send_message(
                C.ADMIN_CHAT_ID,
                f"{ICON[status]} <b>{row['symbol']}</b> · сигнал <code>#{row['id']}</code> "
                f"закрыт: <b>{status.upper()}</b>\n"
                f"Результат <b>{pnl:+.2f}%</b> · максимум в плюс {upd['best']:+.2f}% · "
                f"просадка {upd['worst']:+.2f}%",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔎 Карточка", callback_data=f"sig:{row['id']}"),
                    InlineKeyboardButton(text="📊 Статистика", callback_data="nav:stats")]]))
        db.update_signal(row["id"], **upd)


async def scan_loop():
    api = Bybit()
    instruments = await api.instruments()
    instr_ts = time.time()
    await asyncio.sleep(2)
    if C.ADMIN_CHAT_ID:
        await bot.send_message(C.ADMIN_CHAT_ID,
                               f"🟢 Сканер запущен. Контрактов: <b>{len(instruments)}</b>",
                               reply_markup=main_kb())
        await bot.send_message(C.ADMIN_CHAT_ID, screen_main(), reply_markup=menu_kb())

    while True:
        try:
            if S.paused:
                await asyncio.sleep(5)
                continue
            if time.time() - instr_ts > 6 * 3600:
                instruments = await api.instruments()
                instr_ts = time.time()

            tickers = await api.tickers()
            S.tickers, S.symbols = tickers, len(tickers)
            S.last_scan, S.scans = time.time(), S.scans + 1
            now_ms = int(time.time() * 1000)

            await track_signals(tickers)

            fresh: dict[str, Candidate] = {}
            for sym, t in tickers.items():
                cand = is_candidate(t, instruments.get(sym), now_ms)
                if cand and not db.is_muted(sym):
                    fresh[sym] = cand
            for sym, cand in fresh.items():
                if sym not in S.watchlist:
                    S.watchlist[sym] = cand
                    if C.NOTIFY_CANDIDATES and C.ADMIN_CHAT_ID:
                        await bot.send_message(C.ADMIN_CHAT_ID, candidate_text(cand),
                                               reply_markup=candidate_kb(sym))
                else:
                    w = S.watchlist[sym]
                    w.pcnt24, w.turnover, w.from_high = cand.pcnt24, cand.turnover, cand.from_high
            for sym in list(S.watchlist):
                if sym not in fresh:
                    S.watchlist.pop(sym, None)
                    S.checks.pop(sym, None)

            queue = sorted(S.watchlist.values(), key=lambda c: -c.pcnt24)[:C.MAX_DEEP_CHECKS]
            for cand in queue:
                sym = cand.symbol
                if db.recent_signal(sym, C.COOLDOWN_HOURS) or db.is_muted(sym):
                    cand.note = "кулдаун"
                    continue
                try:
                    score, comps, sig, why = await deep_check(api, sym, tickers[sym])
                except Exception as e:      # noqa: BLE001
                    log.warning("deep_check %s: %s", sym, e)
                    continue
                cand.score = score
                cand.note = why or "сигнал выдан"
                S.checks[sym] = (score, comps, why, time.time())
                if sig and C.ADMIN_CHAT_ID:
                    sid = db.add_signal(sig)
                    S.signals[sid] = sig
                    await bot.send_message(C.ADMIN_CHAT_ID, signal_text(sig, sid),
                                           reply_markup=signal_kb(sid, sym))
                await asyncio.sleep(0.3)

        except Exception as e:      # noqa: BLE001
            S.errors += 1
            log.exception("scan loop: %s", e)
        await asyncio.sleep(C.SCAN_INTERVAL)


async def main():
    if not C.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан")
    db.load_settings()
    asyncio.create_task(scan_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
