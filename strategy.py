"""
Логика поиска точки входа в шорт после аномального роста.

Двухэтапная схема:
  Этап 1 (cheap)  — по одному снимку рынка ищем монеты, выросшие >= PUMP_24H_MIN за 24ч.
  Этап 2 (deep)   — по каждой такой монете тянем свечи/OI и ищем ПОДТВЕРЖДЕНИЕ разворота.
                    Сигнал выдаётся только при слом структуры + остывший импульс + набранный балл.
"""
from dataclasses import dataclass, field
from typing import Optional

import config as C
from bybit import Candle, Ticker


# ---------------------------------------------------------------- индикаторы
def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    e = values[0]
    out = []
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    n = len(closes)
    if n < period + 1:
        return [None] * n
    gains, losses = [], []
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period

    def _r(a: float, b: float) -> float:
        return 100.0 if b == 0 else 100.0 - 100.0 / (1.0 + a / b)

    out: list[Optional[float]] = [None] * period
    out.append(_r(ag, al))
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out.append(_r(ag, al))
    return out


def swing_highs(candles: list[Candle], k: int = 2) -> list[int]:
    idx = []
    for i in range(k, len(candles) - k):
        h = candles[i].h
        if all(h > candles[j].h for j in range(i - k, i)) and \
           all(h >= candles[j].h for j in range(i + 1, i + k + 1)):
            idx.append(i)
    return idx


def swing_lows(candles: list[Candle], k: int = 2) -> list[int]:
    idx = []
    for i in range(k, len(candles) - k):
        l = candles[i].l
        if all(l < candles[j].l for j in range(i - k, i)) and \
           all(l <= candles[j].l for j in range(i + 1, i + k + 1)):
            idx.append(i)
    return idx


# ---------------------------------------------------------------- модели
@dataclass
class Component:
    name: str
    ok: bool
    weight: float
    detail: str


@dataclass
class Signal:
    symbol: str
    score: float
    max_score: float
    entry: float
    retest: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    risk_pct: float
    pcnt24: float
    turnover: float
    funding: float
    mins_since_peak: int
    high24: float
    qty: float
    notional: float
    components: list[Component] = field(default_factory=list)


@dataclass
class Candidate:
    symbol: str
    pcnt24: float
    turnover: float
    from_high: float          # % отката от 24ч хая
    score: float = 0.0
    note: str = "ждём подтверждения"


# ---------------------------------------------------------------- этап 1
def is_candidate(t: Ticker, instr: dict | None, now_ms: int) -> Optional[Candidate]:
    if not instr or instr.get("status") != "Trading":
        return None
    if t.last < C.MIN_PRICE or t.high24 <= 0:
        return None
    age_days = (now_ms - instr["launch"]) / 86_400_000 if instr["launch"] else 999
    if age_days < C.MIN_AGE_DAYS:
        return None
    if t.pcnt24 < C.PUMP_24H_MIN:
        return None
    if t.turnover24 < C.MIN_TURNOVER:
        return None
    from_high = (t.high24 - t.last) / t.high24 * 100
    if from_high > C.MAX_RETRACE_FROM_HIGH:
        return None          # уже слили — шортить поздно
    return Candidate(t.symbol, t.pcnt24, t.turnover24, from_high)


# ---------------------------------------------------------------- этап 2
def evaluate(t: Ticker, k5: list[Candle], k15: list[Candle],
             oi: list[tuple[int, float]]) -> tuple[float, list[Component], Optional[Signal], str]:
    """Возвращает (балл, компоненты, сигнал|None, причина_отказа)."""
    comps: list[Component] = []
    if len(k5) < 40 or len(k15) < 30:
        return 0.0, comps, None, "мало истории"

    price = t.last
    last = k5[-1]
    now = last.ts
    high24, low24 = t.high24, t.low24
    impulse = max(high24 - low24, 1e-12)

    # ---- пик импульса по 5m
    peak_i = max(range(len(k5)), key=lambda i: k5[i].h)
    peak = k5[peak_i]
    mins_since_peak = int((now - peak.ts) / 60000)

    # ---- ВЕТО -------------------------------------------------------------
    fresh = [c for c in k5 if (now - c.ts) / 60000 <= C.NEW_HIGH_VETO_MIN]
    if fresh and max(c.h for c in fresh) >= high24 * 0.999:
        return 0.0, comps, None, "хай только что обновлён — импульс жив"
    if mins_since_peak < C.MIN_MIN_SINCE_PEAK:
        return 0.0, comps, None, f"после пика всего {mins_since_peak} мин"

    # ---- ОБЯЗАТЕЛЬНЫЙ ТРИГГЕР: слом структуры на 5m ------------------------
    # уровень = последний сформированный swing-low после пика (запасной вариант — минимум окна)
    sl_idx = [i for i in swing_lows(k5) if i > peak_i and i <= len(k5) - 3]
    if sl_idx:
        level = k5[sl_idx[-1]].l
    else:
        level = min(c.l for c in k5[-13:-2])
    broke = last.c < level
    comps.append(Component("Слом структуры 5m", broke, 0.0,
                           f"закрытие {last.c:.8g} {'<' if broke else '≥'} уровень {level:.8g}"))
    if not broke:
        return 0.0, comps, None, "структура цела"

    score = 0.0

    # 1. Нижний максимум после пика (2)
    sh = [i for i in swing_highs(k5) if i > peak_i]
    lower_high = bool(sh) and k5[sh[-1]].h < peak.h * 0.995
    lh_price = k5[sh[-1]].h if sh else peak.h
    score += 2 if lower_high else 0
    comps.append(Component("Нижний максимум", lower_high, 2,
                           f"{lh_price:.8g} против пика {peak.h:.8g}" if sh else "новых вершин нет"))

    # 2. Климакс объёма на хае и его затухание (2)
    tail = k5[-24:]
    vmax_c = max(tail, key=lambda c: c.v)
    near_top = vmax_c.h >= high24 * 0.985
    fade = last.v < vmax_c.v * 0.4
    vol_ok = near_top and fade
    score += 2 if vol_ok else 0
    comps.append(Component("Климакс объёма → затухание", vol_ok, 2,
                           f"пик объёма {'на хае' if near_top else 'не на хае'}, "
                           f"сейчас {last.v / max(vmax_c.v, 1e-9) * 100:.0f}% от него"))

    # 3. Медвежья дивергенция / слом RSI на 15m (2)
    cl15 = [c.c for c in k15]
    r15 = rsi(cl15, 14)
    r_now = r15[-1] if r15 else None
    r_hist = [x for x in r15[-40:] if x is not None]
    r_peak = max(r_hist) if r_hist else 0
    div = bool(r_now and r_peak >= 75 and r_now < 55)
    score += 2 if div else 0
    comps.append(Component("RSI 15m остыл / дивергенция", div, 2,
                           f"пик RSI {r_peak:.0f} → сейчас {r_now:.0f}" if r_now else "нет данных"))

    # 4. Funding: толпа в лонгах, шорт получает выплаты (1.5)
    fund_ok = t.funding >= 0.0004
    score += 1.5 if fund_ok else 0
    comps.append(Component("Funding в пользу шорта", fund_ok, 1.5,
                           f"{t.funding * 100:+.4f}% за период"))

    # 5. Разгрузка открытого интереса (1.5)
    oi_ok, oi_note = False, "нет данных OI"
    if len(oi) >= 24:
        oi_vals = [v for _, v in oi]
        oi_peak = max(oi_vals[-72:])
        oi_now = oi_vals[-1]
        oi_grow = oi_peak / max(min(oi_vals[-72:]), 1e-9)
        off_peak = (oi_peak - oi_now) / max(oi_peak, 1e-9) * 100
        oi_ok = oi_grow >= 1.2 and off_peak >= 2.0
        oi_note = f"OI набрал +{(oi_grow - 1) * 100:.0f}%, откат от пика −{off_peak:.1f}%"
    score += 1.5 if oi_ok else 0
    comps.append(Component("Лонги разгружаются (OI)", oi_ok, 1.5, oi_note))

    # 6. Растяжение от EMA (1)
    e21 = ema([c.c for c in k15], 21)
    stretch = (peak.h - e21[-1]) / max(e21[-1], 1e-12) * 100
    ext_ok = stretch >= 12
    score += 1 if ext_ok else 0
    comps.append(Component("Перерастянутость к EMA21 15m", ext_ok, 1,
                           f"хай был на +{stretch:.1f}% от EMA"))

    # 7. Верхняя тень / отбой на хае (1)
    rng = max(peak.h - peak.l, 1e-12)
    wick = (peak.h - max(peak.c, peak.o)) / rng
    wick_ok = wick >= 0.4
    score += 1 if wick_ok else 0
    comps.append(Component("Отбой от вершины (тень)", wick_ok, 1,
                           f"верхняя тень {wick * 100:.0f}% свечи"))

    # 8. Импульс остыл по времени (1)
    stall_ok = mins_since_peak >= C.MIN_MIN_SINCE_PEAK * 2
    score += 1 if stall_ok else 0
    comps.append(Component("Импульс остыл", stall_ok, 1,
                           f"{mins_since_peak // 60}ч {mins_since_peak % 60}м без нового хая"))

    # 9. Ниже VWAP-прокси последних 4ч (1)
    tail48 = k5[-48:]
    vwap = sum(((c.h + c.l + c.c) / 3) * c.v for c in tail48) / max(sum(c.v for c in tail48), 1e-12)
    vwap_ok = price < vwap
    score += 1 if vwap_ok else 0
    comps.append(Component("Цена ниже VWAP 4ч", vwap_ok, 1, f"VWAP {vwap:.8g}"))

    max_score = 12.0
    if score < C.TRIGGER_SCORE:
        return score, comps, None, f"балл {score:.1f} < {C.TRIGGER_SCORE}"

    # ---- уровни -----------------------------------------------------------
    entry = price
    # стоп: ближайшая структура + запас на волатильность (чтобы не выбило шумом)
    local_high = max(c.h for c in k5[-12:]) * 1.003
    trs = [max(c.h - c.l, abs(c.h - k5[i - 1].c), abs(c.l - k5[i - 1].c))
           for i, c in enumerate(k5) if i >= len(k5) - 14 and i > 0]
    atr = sum(trs) / len(trs) if trs else 0.0
    sl = max(local_high, entry + 1.2 * atr)
    if sl <= entry:
        return score, comps, None, "стоп не над входом"
    risk_pct = (sl - entry) / entry * 100
    if risk_pct > C.MAX_RISK_PCT:
        return score, comps, None, f"стоп слишком далеко ({risk_pct:.1f}%)"

    r = sl - entry
    tp1 = entry - 1.0 * r
    tp2 = entry - 2.0 * r
    fib382 = high24 - 0.382 * impulse
    tp3 = min(fib382, entry - 3.0 * r)

    risk_usd = C.DEPOSIT_USD * C.RISK_PCT / 100
    qty = risk_usd / r
    notional = qty * entry

    sig = Signal(
        symbol=t.symbol, score=score, max_score=max_score,
        entry=entry, retest=level, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
        risk_pct=risk_pct, pcnt24=t.pcnt24, turnover=t.turnover24,
        funding=t.funding, mins_since_peak=mins_since_peak, high24=high24,
        qty=qty, notional=notional, components=comps,
    )
    return score, comps, sig, ""
