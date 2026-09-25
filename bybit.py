import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger("bybit")
BASE = "https://api.bybit.com"


@dataclass
class Candle:
    ts: int      # ms, начало свечи
    o: float
    h: float
    l: float
    c: float
    v: float
    turn: float


@dataclass
class Ticker:
    symbol: str
    last: float
    pcnt24: float        # % за 24ч
    high24: float
    low24: float
    prev24: float
    turnover24: float
    funding: float       # ставка за период (0.0001 = 0.01%)
    oi_value: float      # открытый интерес в $


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class Bybit:
    """Только публичные эндпоинты — ключи бирже не нужны."""

    def __init__(self, timeout: float = 15.0):
        self.cli = httpx.AsyncClient(
            base_url=BASE,
            timeout=timeout,
            headers={"User-Agent": "pump-short-scanner/1.0"},
        )

    async def close(self):
        await self.cli.aclose()

    async def _get(self, path: str, params: dict | None = None, retries: int = 3):
        last_err: Optional[Exception] = None
        for attempt in range(retries):
            try:
                r = await self.cli.get(path, params=params)
                r.raise_for_status()
                data = r.json()
                if data.get("retCode") != 0:
                    raise RuntimeError(f"{path} retCode={data.get('retCode')} {data.get('retMsg')}")
                return data["result"]
            except Exception as e:      # noqa: BLE001
                last_err = e
                await asyncio.sleep(1.5 * (attempt + 1))
        raise last_err  # type: ignore[misc]

    async def instruments(self) -> dict[str, dict]:
        """Все USDT-перпетуалы: время листинга, статус, шаг цены."""
        out: dict[str, dict] = {}
        cursor = None
        while True:
            params = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            res = await self._get("/v5/market/instruments-info", params)
            for it in res.get("list", []):
                if it.get("quoteCoin") != "USDT" or it.get("contractType") != "LinearPerpetual":
                    continue
                out[it["symbol"]] = {
                    "launch": int(it.get("launchTime") or 0),
                    "status": it.get("status", ""),
                    "tick": _f(it.get("priceFilter", {}).get("tickSize"), 0.0001),
                }
            cursor = res.get("nextPageCursor")
            if not cursor:
                break
        log.info("instruments loaded: %d", len(out))
        return out

    async def tickers(self) -> dict[str, Ticker]:
        """Один запрос — снимок по всему рынку фьючерсов."""
        res = await self._get("/v5/market/tickers", {"category": "linear"})
        out: dict[str, Ticker] = {}
        for t in res.get("list", []):
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            out[sym] = Ticker(
                symbol=sym,
                last=_f(t.get("lastPrice")),
                pcnt24=_f(t.get("price24hPcnt")) * 100.0,
                high24=_f(t.get("highPrice24h")),
                low24=_f(t.get("lowPrice24h")),
                prev24=_f(t.get("prevPrice24h")),
                turnover24=_f(t.get("turnover24h")),
                funding=_f(t.get("fundingRate")),
                oi_value=_f(t.get("openInterestValue")),
            )
        return out

    async def klines(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
        """Свечи в хронологическом порядке. Последняя (незакрытая) отбрасывается."""
        res = await self._get(
            "/v5/market/kline",
            {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit},
        )
        rows = res.get("list", [])
        out = [
            Candle(int(r[0]), _f(r[1]), _f(r[2]), _f(r[3]), _f(r[4]), _f(r[5]), _f(r[6]))
            for r in rows
        ]
        out.sort(key=lambda c: c.ts)
        return out[:-1] if len(out) > 1 else out

    async def open_interest(self, symbol: str, interval: str = "5min", limit: int = 100):
        """Ряд открытого интереса, хронологически: [(ts_ms, oi_в_монетах), ...]"""
        res = await self._get(
            "/v5/market/open-interest",
            {"category": "linear", "symbol": symbol, "intervalTime": interval, "limit": limit},
        )
        rows = [(int(x["timestamp"]), _f(x["openInterest"])) for x in res.get("list", [])]
        rows.sort(key=lambda x: x[0])
        return rows
