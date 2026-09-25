import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

import config as C
import storage as db
from bybit import Bybit, Ticker
from strategy import Candidate, Signal, evaluate, is_candidate

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

bot = Bot(C.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


class State:
    started = time.time()
    paused = False
    symbols = 0
    last_scan = 0.0
    scans = 0
    watchlist: dict[str, Candidate] = {}
    tickers: dict[str, Ticker] = {}
    last_signal: dict[int, Signal] = {}     # id -> объект для кнопки «Разбор»


S = State()


# ----------------------------------------------------------------- утилиты
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
    return f"${x / 1e3:.0f}K"


def bar(score: float, mx: float, cells: int = 10) -> str:
    filled = round(score / mx * cells)
    return "▰" * filled + "▱" * (cells - filled)


def admin_only(uid: int) -> bool:
    return C.ADMIN_CHAT_ID == 0 or uid == C.ADMIN_CHAT_ID


# ----------------------------------------------------------------- карточки
def signal_text(s: Signal, sid: int) -> str:
    r = s.sl - s.entry
    lines = [
        f"🔴 <b>SHORT · #{s.symbol}</b>",
        "",
        f"📈 <b>+{s.pcnt24:.1f}%</b> за 24ч   •   💵 {money(s.turnover)}",
        f"🎯 Готовность <b>{s.score:.1f}/{s.max_score:.0f}</b>  {bar(s.score, s.max_score)}",
        "",
        "<pre>"
        f"Вход   {fnum(s.entry)}\n"
        f"Ретест {fnum(s.retest)}  (лучшая цена)\n"
        f"Стоп   {fnum(s.sl)}  +{s.risk_pct:.2f}%\n"
        f"TP1    {fnum(s.tp1)}  −{(s.entry - s.tp1) / s.entry * 100:.2f}%  1R\n"
        f"TP2    {fnum(s.tp2)}  −{(s.entry - s.tp2) / s.entry * 100:.2f}%  2R\n"
        f"TP3    {fnum(s.tp3)}  −{(s.entry - s.tp3) / s.entry * 100:.2f}%  {(s.entry - s.tp3) / r:.1f}R"
        "</pre>",
    ]
    for c in s.components:
        if c.weight == 0:
            continue
        lines.append(f"{'✅' if c.ok else '➖'} {c.name} — <i>{c.detail}</i>")
    lines += [
        "",
        f"⏱ Пик был {s.mins_since_peak // 60}ч {s.mins_since_peak % 60}м назад, хай не обновлялся",
        f"💰 Funding {s.funding * 100:+.4f}% — {'платят шортам' if s.funding > 0 else 'платит шорт'}",
        f"📐 Размер: <b>{s.qty:.4g}</b> монет ≈ {money(s.notional)} "
        f"(риск {C.RISK_PCT}% от ${C.DEPOSIT_USD:.0f})",
        "",
        "⚠️ Стоп ставим сразу. Без усреднений — памп может продолжиться.",
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
    ])


def candidate_text(c: Candidate) -> str:
    return (f"👀 <b>#{c.symbol}</b> в наблюдении\n"
            f"+{c.pcnt24:.1f}% за 24ч • {money(c.turnover)} • "
            f"откат от хая {c.from_high:.1f}%\n"
            f"<i>Вход не даю — жду подтверждения разворота.</i>")


# ----------------------------------------------------------------- команды
@dp.message(Command("start", "help"))
async def cmd_start(m: Message):
    if not admin_only(m.from_user.id):
        return
    await m.answer(
        "🤖 <b>Bybit Pump-Short Scanner</b>\n\n"
        f"Ищу USDT-перпетуалы, выросшие ≥ <b>{C.PUMP_24H_MIN:.0f}%</b> за 24ч, "
        "и жду подтверждённый разворот, чтобы дать точку шорта.\n\n"
        "<b>Команды</b>\n"
        "/status — состояние сканера\n"
        "/watchlist — кто сейчас под наблюдением\n"
        "/top — топ-10 роста за 24ч прямо сейчас\n"
        "/stats — статистика сигналов\n"
        "/settings — настройки порогов\n"
        "/pause /resume — пауза сканера\n\n"
        f"Твой chat_id: <code>{m.chat.id}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📡 Статус", callback_data="status"),
            InlineKeyboardButton(text="👀 Watchlist", callback_data="watch"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"),
        ]]))


def status_text() -> str:
    up = int(time.time() - S.started)
    ago = int(time.time() - S.last_scan) if S.last_scan else -1
    st = db.stats(30)
    return (
        f"📡 <b>Статус</b>\n\n"
        f"{'⏸ ПАУЗА' if S.paused else '🟢 Работает'}\n"
        f"Аптайм: {up // 3600}ч {up % 3600 // 60}м • циклов: {S.scans}\n"
        f"Монет в скане: <b>{S.symbols}</b> • последний скан: {ago}с назад\n"
        f"В наблюдении: <b>{len(S.watchlist)}</b> • открытых сигналов: {st['open']}\n\n"
        f"<b>Фильтры</b>\n"
        f"<pre>рост 24ч   ≥ {C.PUMP_24H_MIN:.0f}%\n"
        f"оборот     ≥ {money(C.MIN_TURNOVER)}\n"
        f"возраст    ≥ {C.MIN_AGE_DAYS} дн\n"
        f"балл входа ≥ {C.TRIGGER_SCORE:.1f}/12\n"
        f"после пика ≥ {C.MIN_MIN_SINCE_PEAK} мин\n"
        f"кулдаун      {C.COOLDOWN_HOURS} ч</pre>"
    )


@dp.message(Command("status"))
async def cmd_status(m: Message):
    if admin_only(m.from_user.id):
        await m.answer(status_text(), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Обновить", callback_data="status")]]))


@dp.message(Command("watchlist"))
async def cmd_watch(m: Message):
    if admin_only(m.from_user.id):
        await m.answer(watch_text())


def watch_text() -> str:
    if not S.watchlist:
        return "👀 Список наблюдения пуст — аномальных пампов сейчас нет."
    rows = sorted(S.watchlist.values(), key=lambda c: -c.score)
    out = ["👀 <b>Под наблюдением</b>\n"]
    for c in rows[:15]:
        out.append(f"<b>#{c.symbol}</b> +{c.pcnt24:.0f}% • {money(c.turnover)}\n"
                   f"   балл {c.score:.1f}/12 {bar(c.score, 12, 6)} • {c.note}")
    return "\n".join(out)


@dp.message(Command("top"))
async def cmd_top(m: Message):
    if admin_only(m.from_user.id):
        await m.answer(top_text(), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Обновить", callback_data="top")]]))


def top_text() -> str:
    if not S.tickers:
        return "Данных пока нет, идёт первый скан."
    rows = sorted(S.tickers.values(), key=lambda t: -t.pcnt24)[:10]
    out = ["🚀 <b>Топ роста за 24ч</b>\n<pre>"]
    for t in rows:
        out.append(f"{t.symbol:<14}{t.pcnt24:>7.1f}%  {money(t.turnover24):>8}")
    out.append("</pre>")
    return "\n".join(out)


@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    if not admin_only(m.from_user.id):
        return
    s = db.stats(30)
    await m.answer(
        f"📊 <b>Статистика за 30 дней</b>\n\n"
        f"<pre>Сигналов     {s['total']}\n"
        f"Закрыто      {s['closed']}\n"
        f"TP           {s['wins']}\n"
        f"SL           {s['losses']}\n"
        f"Истекло      {s['expired']}\n"
        f"Winrate      {s['winrate']:.1f}%\n"
        f"Средний ход  +{s['avg_best']:.2f}% в плюс\n"
        f"Средняя просадка −{abs(s['avg_worst']):.2f}%</pre>\n"
        "<i>Сначала набери 30–50 сигналов в режиме наблюдения, потом торгуй.</i>")


@dp.message(Command("pause"))
async def cmd_pause(m: Message):
    if admin_only(m.from_user.id):
        S.paused = True
        await m.answer("⏸ Сканер на паузе.")


@dp.message(Command("resume"))
async def cmd_resume(m: Message):
    if admin_only(m.from_user.id):
        S.paused = False
        await m.answer("🟢 Сканер запущен.")


def settings_kb() -> InlineKeyboardMarkup:
    rows = []
    for key, (_, step, label) in C.SETTABLE.items():
        val = getattr(C, key)
        shown = money(val) if key == "MIN_TURNOVER" else f"{val:g}"
        rows.append([
            InlineKeyboardButton(text="➖", callback_data=f"set:{key}:-"),
            InlineKeyboardButton(text=f"{label.split()[1] if ' ' in label else key}: {shown}",
                                 callback_data="noop"),
            InlineKeyboardButton(text="➕", callback_data=f"set:{key}:+"),
        ])
    rows.append([InlineKeyboardButton(text="📡 Статус", callback_data="status")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("settings"))
async def cmd_settings(m: Message):
    if admin_only(m.from_user.id):
        await m.answer("⚙️ <b>Настройки</b>\nМеняются на лету, сохраняются в базе.",
                       reply_markup=settings_kb())


# ----------------------------------------------------------------- колбэки
@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


@dp.callback_query(F.data == "status")
async def cb_status(c: CallbackQuery):
    await c.message.edit_text(status_text(), reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить", callback_data="status")]]))
    await c.answer()


@dp.callback_query(F.data == "watch")
async def cb_watch(c: CallbackQuery):
    await c.message.edit_text(watch_text())
    await c.answer()


@dp.callback_query(F.data == "top")
async def cb_top(c: CallbackQuery):
    await c.message.edit_text(top_text(), reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить", callback_data="top")]]))
    await c.answer()


@dp.callback_query(F.data == "settings")
async def cb_settings(c: CallbackQuery):
    await c.message.edit_text("⚙️ <b>Настройки</b>", reply_markup=settings_kb())
    await c.answer()


@dp.callback_query(F.data.startswith("set:"))
async def cb_set(c: CallbackQuery):
    _, key, sign = c.data.split(":")
    step = C.SETTABLE[key][1]
    val = getattr(C, key) + (step if sign == "+" else -step)
    val = max(step, val)
    db.save_setting(key, val)
    await c.message.edit_reply_markup(reply_markup=settings_kb())
    await c.answer(f"{key} = {val:g}")


@dp.callback_query(F.data.startswith("mute:"))
async def cb_mute(c: CallbackQuery):
    sym = c.data.split(":")[1]
    db.mute(sym, 8)
    S.watchlist.pop(sym, None)
    await c.answer(f"🔕 {sym} замучен на 8 часов", show_alert=True)


@dp.callback_query(F.data.startswith(("took:", "skip:")))
async def cb_taken(c: CallbackQuery):
    action, sid = c.data.split(":")
    db.update_signal(int(sid), taken=1 if action == "took" else -1)
    await c.answer("✅ Отмечено: взял" if action == "took" else "❌ Отмечено: пропустил")


@dp.callback_query(F.data.startswith("det:"))
async def cb_detail(c: CallbackQuery):
    sid = int(c.data.split(":")[1])
    s = S.last_signal.get(sid)
    row = db.get_signal(sid)
    if not s and not row:
        await c.answer("Данные по сигналу не найдены")
        return
    if s:
        txt = ["🔎 <b>Полный разбор</b> #" + s.symbol, "<pre>"]
        for comp in s.components:
            mark = "+" if comp.ok else " "
            txt.append(f"[{mark}] {comp.weight:>4.1f}  {comp.name}")
            txt.append(f"        {comp.detail}")
        txt.append(f"\nИтого {s.score:.1f}/{s.max_score:.0f}</pre>")
        await c.message.answer("\n".join(txt))
    await c.answer()


# ----------------------------------------------------------------- сканер
async def deep_check(api: Bybit, sym: str, t: Ticker):
    k5 = await api.klines(sym, "5", 120)
    k15 = await api.klines(sym, "15", 96)
    try:
        oi = await api.open_interest(sym, "5min", 100)
    except Exception:       # noqa: BLE001
        oi = []
    return evaluate(t, k5, k15, oi)


async def track_signals(tickers: dict[str, Ticker]):
    """Ведём открытые сигналы: MFE/MAE, срабатывание TP/SL, отчёт в чат."""
    for row in db.open_signals():
        t = tickers.get(row["symbol"])
        if not t:
            continue
        entry, price = row["entry"], t.last
        pnl = (entry - price) / entry * 100          # шорт: падение = плюс
        best = max(row["best"], pnl)
        worst = min(row["worst"], pnl)
        upd = {"best": best, "worst": worst}
        status = None
        if price >= row["sl"]:
            status = "sl"
        elif price <= row["tp3"]:
            status = "tp3"
        elif price <= row["tp2"] and row["status"] == "open":
            status = "tp2"
        elif price <= row["tp1"] and row["status"] == "open":
            status = "tp1"
        elif time.time() - row["ts"] > C.TRACK_HOURS * 3600:
            status = "expired"

        if status:
            upd |= {"status": status, "closed_ts": int(time.time())}
            icon = {"sl": "🛑", "expired": "⌛", "tp1": "🎯", "tp2": "🎯", "tp3": "🏁"}[status]
            await bot.send_message(
                C.ADMIN_CHAT_ID,
                f"{icon} <b>#{row['symbol']}</b> — сигнал <code>#{row['id']}</code> закрыт: "
                f"<b>{status.upper()}</b>\n"
                f"Результат {pnl:+.2f}% • максимум в плюс {best:+.2f}% • просадка {worst:+.2f}%")
        db.update_signal(row["id"], **upd)


async def scan_loop():
    api = Bybit()
    instruments = await api.instruments()
    instr_ts = time.time()
    await asyncio.sleep(2)
    if C.ADMIN_CHAT_ID:
        await bot.send_message(C.ADMIN_CHAT_ID,
                               f"🟢 Сканер запущен. Контрактов в работе: {len(instruments)}")

    while True:
        try:
            if S.paused:
                await asyncio.sleep(5)
                continue
            if time.time() - instr_ts > 6 * 3600:
                instruments = await api.instruments()
                instr_ts = time.time()

            tickers = await api.tickers()
            S.tickers = tickers
            S.symbols = len(tickers)
            S.last_scan = time.time()
            S.scans += 1
            now_ms = int(time.time() * 1000)

            await track_signals(tickers)

            # --- этап 1: обновляем watchlist
            fresh: dict[str, Candidate] = {}
            for sym, t in tickers.items():
                cand = is_candidate(t, instruments.get(sym), now_ms)
                if cand and not db.is_muted(sym):
                    fresh[sym] = cand
            for sym, cand in fresh.items():
                if sym not in S.watchlist:
                    S.watchlist[sym] = cand
                    if C.NOTIFY_CANDIDATES and C.ADMIN_CHAT_ID:
                        await bot.send_message(C.ADMIN_CHAT_ID, candidate_text(cand))
                else:
                    S.watchlist[sym].pcnt24 = cand.pcnt24
                    S.watchlist[sym].turnover = cand.turnover
                    S.watchlist[sym].from_high = cand.from_high
            for sym in list(S.watchlist):
                if sym not in fresh:
                    S.watchlist.pop(sym, None)

            # --- этап 2: глубокая проверка
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
                if sig and C.ADMIN_CHAT_ID:
                    sid = db.add_signal(sig)
                    S.last_signal[sid] = sig
                    await bot.send_message(C.ADMIN_CHAT_ID, signal_text(sig, sid),
                                           reply_markup=signal_kb(sid, sym))
                await asyncio.sleep(0.3)

        except Exception as e:      # noqa: BLE001
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
