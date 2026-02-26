"""
Data Ingestion Layer for Neko Signal System.

Provides async functions to fetch OHLCV candles with Binance's extended
taker-volume columns, and L2 orderbook snapshots, via ccxt's async support.

Key design choices:
    - Uses Binance's raw kline API (fapiPublicGetKlines) to guarantee access
      to taker_buy_base_volume, which ccxt's generic fetch_ohlcv may strip.
    - Retry logic with exponential back-off isolates transient network issues.
    - Returns typed DataFrames with a consistent schema for downstream modules.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd

from config import (
    EXCHANGE_ID,
    MAX_RETRIES,
    OHLCV_LIMIT,
    ORDERBOOK_DEPTH,
    PRIMARY_TIMEFRAME,
    RETRY_DELAY_S,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column schema returned by fetch_extended_ohlcv
# ---------------------------------------------------------------------------
OHLCV_COLUMNS: Final[list[str]] = [
    "open",
    "high",
    "low",
    "close",
    "volume",          # Total base-asset volume
    "taker_buy_volume",    # Taker buy base-asset volume (Binance extended)
    "taker_sell_volume",   # Derived: volume - taker_buy_volume
]

# Silence the Final import warning for Python <3.11
try:
    from typing import Final
except ImportError:
    from typing_extensions import Final  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Exchange Factory
# ---------------------------------------------------------------------------

def create_exchange(api_key: str = "", api_secret: str = "") -> ccxt.Exchange:
    """Creates and returns a configured ccxt async Binance USDM exchange instance.

    The exchange enables rate limiting and targets the futures market type.
    For public data endpoints (OHLCV, orderbook) no credentials are required.

    Args:
        api_key: Binance API key. Leave empty for public-only access.
        api_secret: Binance API secret. Leave empty for public-only access.

    Returns:
        A ccxt async exchange instance ready for awaiting calls.
    """
    return ccxt.binanceusdm(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "future",
                "adjustForTimeDifference": True,
            },
        }
    )


# ---------------------------------------------------------------------------
# Internal: Raw Binance Kline Fetcher
# ---------------------------------------------------------------------------

async def _fetch_raw_klines(
    exchange: ccxt.Exchange,
    market_id: str,
    interval: str,
    limit: int,
) -> Optional[list[list]]:
    """Fetches raw kline data directly from Binance's fapi endpoint.

    Binance's /fapi/v1/klines returns a 12-element array per candle:
        [open_time, open, high, low, close, volume, close_time,
         quote_volume, trade_count, taker_buy_base, taker_buy_quote, ignore]

    Args:
        exchange: Initialized ccxt binanceusdm exchange.
        market_id: Binance-native symbol (e.g., "BTCUSDT").
        interval: Binance kline interval string (e.g., "1m").
        limit: Number of candles to retrieve.

    Returns:
        List of raw 12-element kline arrays, or None on failure.
    """
    params: dict = {"symbol": market_id, "interval": interval, "limit": limit}
    return await exchange.fapiPublicGetKlines(params)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Public: Extended OHLCV Fetcher
# ---------------------------------------------------------------------------

async def fetch_extended_ohlcv(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str = PRIMARY_TIMEFRAME,
    limit: int = OHLCV_LIMIT,
) -> Optional[pd.DataFrame]:
    """Fetches OHLCV data with Binance's extended taker-volume columns.

    Uses Binance's raw kline API to guarantee access to:
        - ``taker_buy_volume``  (Taker Buy Base Asset Volume)
        - ``taker_sell_volume`` (Derived: Total Volume - Taker Buy Volume)

    Falls back to ccxt's generic ``fetch_ohlcv`` if the raw API call fails,
    estimating taker split at 50/50 (which the anti-wash filter will flag).

    Args:
        exchange: An initialized ccxt async binanceusdm exchange instance.
        symbol: Trading symbol in ccxt format, e.g. ``"BTC/USDT:USDT"``.
        timeframe: Candle timeframe string accepted by ccxt, e.g. ``"1m"``.
        limit: Number of candles to retrieve (max 1500 for Binance).

    Returns:
        A ``pd.DataFrame`` indexed by UTC timestamp with columns:
        ``[open, high, low, close, volume, taker_buy_volume, taker_sell_volume]``
        Returns ``None`` if all retry attempts fail.
    """
    await exchange.load_markets()
    market: dict = exchange.market(symbol)
    market_id: str = market["id"]                            # e.g. "BTCUSDT"
    interval: str = exchange.timeframes.get(timeframe, "1m")  # e.g. "1m"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw: Optional[list[list]] = await _fetch_raw_klines(
                exchange, market_id, interval, limit
            )

            if not raw:
                logger.warning("[%s] Empty kline response (attempt %d).", symbol, attempt)
                await asyncio.sleep(RETRY_DELAY_S * attempt)
                continue

            df = _parse_binance_klines(raw, symbol)
            if df is not None:
                return df

        except ccxt.BadSymbol as exc:
            logger.error("[%s] Bad symbol: %s", symbol, exc)
            return None
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            logger.warning(
                "[%s] Network error attempt %d/%d: %s", symbol, attempt, MAX_RETRIES, exc
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY_S * attempt)
        except ccxt.ExchangeError as exc:
            logger.error("[%s] Exchange error: %s — falling back to generic fetch.", symbol, exc)
            return await _fallback_fetch_ohlcv(exchange, symbol, timeframe, limit)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] Unexpected error fetching klines: %s", symbol, exc)
            return None

    return None


def _parse_binance_klines(raw: list[list], symbol: str) -> Optional[pd.DataFrame]:
    """Parses Binance's raw 12-column kline response into a typed DataFrame.

    Args:
        raw: List of raw kline arrays from Binance fapi endpoint.
        symbol: Symbol name used only for logging.

    Returns:
        Parsed DataFrame or ``None`` if columns are missing.
    """
    if not raw or len(raw[0]) < 10:
        logger.warning("[%s] Kline response has fewer than 10 columns.", symbol)
        return None

    df = pd.DataFrame(
        raw,
        columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trade_count",
            "taker_buy_base_volume", "taker_buy_quote_volume", "ignore",
        ],
    )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"].astype(np.int64), unit="ms", utc=True
    )
    df.set_index("timestamp", inplace=True)

    numeric_cols: list[str] = [
        "open", "high", "low", "close", "volume", "taker_buy_base_volume"
    ]
    df[numeric_cols] = df[numeric_cols].astype(np.float64)

    df["taker_buy_volume"] = df["taker_buy_base_volume"]
    df["taker_sell_volume"] = df["volume"] - df["taker_buy_volume"]

    return df[["open", "high", "low", "close", "volume",
               "taker_buy_volume", "taker_sell_volume"]].copy()


async def _fallback_fetch_ohlcv(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    limit: int,
) -> Optional[pd.DataFrame]:
    """Fallback: uses ccxt's generic fetch_ohlcv with estimated taker split.

    When the raw Binance API is unavailable, taker volumes are estimated at
    50/50, which the anti-wash filter (Gate 2) will subsequently flag and
    reject. This prevents silent data corruption.

    Args:
        exchange: ccxt exchange instance.
        symbol: ccxt trading symbol.
        timeframe: Candle timeframe.
        limit: Number of candles.

    Returns:
        DataFrame with estimated taker columns, or ``None`` on failure.
    """
    try:
        raw: list[list] = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not raw:
            return None

        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        df = df.astype(np.float64)

        # 50/50 split — intentionally triggers Gate 2 anti-wash filter
        df["taker_buy_volume"] = df["volume"] * 0.5
        df["taker_sell_volume"] = df["volume"] * 0.5
        logger.warning(
            "[%s] Fallback OHLCV used. Taker volumes estimated (50/50). "
            "Gate 2 will likely reject this candle.",
            symbol,
        )
        return df[["open", "high", "low", "close", "volume",
                   "taker_buy_volume", "taker_sell_volume"]].copy()
    except Exception as exc:  # noqa: BLE001
        logger.exception("[%s] Fallback OHLCV also failed: %s", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# Public: L2 Orderbook Fetcher
# ---------------------------------------------------------------------------

async def fetch_orderbook(
    exchange: ccxt.Exchange,
    symbol: str,
    depth: int = ORDERBOOK_DEPTH,
) -> Optional[dict]:
    """Fetches an L2 orderbook snapshot for a given symbol.

    Args:
        exchange: An initialized ccxt async exchange instance.
        symbol: Trading symbol in ccxt format, e.g. ``"BTC/USDT:USDT"``.
        depth: Number of price levels to retrieve on each side.

    Returns:
        A dict with keys:
            - ``"bids"``: list of ``[price, size]`` sorted descending.
            - ``"asks"``: list of ``[price, size]`` sorted ascending.
            - ``"timestamp"``: exchange timestamp in ms (may be ``None``).
        Returns ``None`` if all retry attempts fail.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            ob: dict = await exchange.fetch_order_book(symbol, limit=depth)
            return {
                "bids": ob.get("bids", []),
                "asks": ob.get("asks", []),
                "timestamp": ob.get("timestamp"),
            }
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            logger.warning(
                "[%s] Orderbook network error attempt %d/%d: %s",
                symbol, attempt, MAX_RETRIES, exc,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY_S * attempt)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] Unexpected orderbook error: %s", symbol, exc)
            return None

    return None
