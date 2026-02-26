"""
Configuration Hub for Neko Signal System.

Loads all tunable parameters from a ``.env`` file (via ``python-dotenv``)
and exposes them as typed, ``Final`` module-level constants. Downstream
modules must import from here exclusively — never read ``os.environ``
directly and never hard-code values.

Priority (highest → lowest):
    1. Actual environment variables already set in the shell.
    2. Values declared in the ``.env`` file in the project root.
    3. The default values defined in this module as fallbacks.

Usage::

    from config import TRADING_PAIRS, WEBHOOK_URL, MIN_RR_RATIO
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap: load .env from the project root
# ---------------------------------------------------------------------------

_ENV_PATH: Path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=False)


# ---------------------------------------------------------------------------
# Internal helpers — typed .env readers with fallback defaults
# ---------------------------------------------------------------------------

def _str(key: str, default: str) -> str:
    """Reads a string value from environment, falling back to ``default``."""
    return os.environ.get(key, default).strip()


def _int(key: str, default: int) -> int:
    """Reads an integer value from environment, falling back to ``default``."""
    raw: str = os.environ.get(key, "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    """Reads a float value from environment, falling back to ``default``."""
    raw: str = os.environ.get(key, "").strip()
    try:
        return float(raw)
    except ValueError:
        return default


def _str_list(key: str, default: list[str]) -> list[str]:
    """Reads a comma-separated string from environment into a list."""
    raw: str = os.environ.get(key, "").strip()
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Binance API Credentials
# ---------------------------------------------------------------------------

BINANCE_API_KEY: Final[str] = _str("BINANCE_API_KEY", "")
BINANCE_API_SECRET: Final[str] = _str("BINANCE_API_SECRET", "")

# ---------------------------------------------------------------------------
# Trading Universe (Binance USDM Perpetual Futures format for ccxt)
# ---------------------------------------------------------------------------

TRADING_PAIRS: Final[list[str]] = _str_list(
    "TRADING_PAIRS",
    default=[
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "SOL/USDT:USDT",
        "BNB/USDT:USDT",
        "XRP/USDT:USDT",
    ],
)

# Human-readable display names derived from the symbol list.
# Maps "BTC/USDT:USDT" → "BTCUSDT" by stripping everything after the first "/".
PAIR_DISPLAY_NAMES: Final[dict[str, str]] = {
    sym: sym.split("/")[0] + "USDT" for sym in TRADING_PAIRS
}

# ---------------------------------------------------------------------------
# Exchange Configuration
# ---------------------------------------------------------------------------

EXCHANGE_ID: Final[str] = "binanceusdm"
API_RATE_LIMIT_MS: Final[int] = _int("API_RATE_LIMIT_MS", 200)
MAX_RETRIES: Final[int] = _int("MAX_RETRIES", 3)
RETRY_DELAY_S: Final[float] = _float("RETRY_DELAY_S", 2.0)

# ---------------------------------------------------------------------------
# Timeframes & Data Sizes
# ---------------------------------------------------------------------------

PRIMARY_TIMEFRAME: Final[str] = _str("PRIMARY_TIMEFRAME", "1m")
OHLCV_LIMIT: Final[int] = _int("OHLCV_LIMIT", 500)
ORDERBOOK_DEPTH: Final[int] = _int("ORDERBOOK_DEPTH", 50)

# ---------------------------------------------------------------------------
# Session Killzone — UTC hours [start, end)
# Corresponds to 19:00–04:00 VN Time (ICT) which is 12:00–21:00 UTC
# ---------------------------------------------------------------------------

SESSION_START_UTC: Final[int] = _int("SESSION_START_UTC", 12)
SESSION_END_UTC: Final[int] = _int("SESSION_END_UTC", 21)

# ---------------------------------------------------------------------------
# Signal Quality Thresholds
# ---------------------------------------------------------------------------

SCORE_LONG_THRESHOLD: Final[int] = _int("SCORE_LONG_THRESHOLD", 4)
SCORE_SHORT_THRESHOLD: Final[int] = _int("SCORE_SHORT_THRESHOLD", -4)
MIN_RR_RATIO: Final[float] = _float("MIN_RR_RATIO", 2.0)

# ---------------------------------------------------------------------------
# Anti-Wash Trading Filter Parameters
# ---------------------------------------------------------------------------

WASH_TRADE_TAKER_RATIO_LOW: Final[float] = _float("WASH_TRADE_TAKER_RATIO_LOW", 0.49)
WASH_TRADE_TAKER_RATIO_HIGH: Final[float] = _float("WASH_TRADE_TAKER_RATIO_HIGH", 0.51)
MIN_VOLUME_EFFICIENCY: Final[float] = _float("MIN_VOLUME_EFFICIENCY", 0.0002)
ATR_MA_PERIOD: Final[int] = _int("ATR_MA_PERIOD", 24)
ATR_PERIOD: Final[int] = _int("ATR_PERIOD", 14)

# ---------------------------------------------------------------------------
# Volume Profile
# ---------------------------------------------------------------------------

VP_BINS: Final[int] = _int("VP_BINS", 30)
HVN_PERCENTILE: Final[float] = _float("HVN_PERCENTILE", 75.0)

# ---------------------------------------------------------------------------
# CVD / OFI Rolling Windows
# ---------------------------------------------------------------------------

CVD_WINDOW: Final[int] = _int("CVD_WINDOW", 20)
OFI_WINDOW: Final[int] = _int("OFI_WINDOW", 10)

# ---------------------------------------------------------------------------
# Momentum / Orderbook
# ---------------------------------------------------------------------------

MOMENTUM_ROC_PERIOD: Final[int] = _int("MOMENTUM_ROC_PERIOD", 5)
OB_IMBALANCE_FACTOR: Final[float] = _float("OB_IMBALANCE_FACTOR", 1.2)
OB_PROXIMITY_PCT: Final[float] = _float("OB_PROXIMITY_PCT", 0.005)

# ---------------------------------------------------------------------------
# Risk Manager
# ---------------------------------------------------------------------------

ATR_SL_MULTIPLIER: Final[float] = _float("ATR_SL_MULTIPLIER", 1.5)
SWING_LOOKBACK: Final[int] = _int("SWING_LOOKBACK", 20)
HVN_PROXIMITY_PCT: Final[float] = _float("HVN_PROXIMITY_PCT", 0.005)

# ---------------------------------------------------------------------------
# InfluxDB v2 Telemetry (metrics_exporter.py)
# ---------------------------------------------------------------------------

INFLUXDB_URL: Final[str] = _str("INFLUXDB_URL", "http://localhost:8086")
INFLUXDB_TOKEN: Final[str] = _str("INFLUXDB_TOKEN", "")
INFLUXDB_ORG: Final[str] = _str("INFLUXDB_ORG", "neko")
INFLUXDB_BUCKET: Final[str] = _str("INFLUXDB_BUCKET", "neko_signal")

# ---------------------------------------------------------------------------
# Webhook / Notifier
# ---------------------------------------------------------------------------

WEBHOOK_URL: Final[str] = _str("WEBHOOK_URL", "https://hooks.example.com/neko-signal")
WEBHOOK_TIMEOUT_S: Final[float] = _float("WEBHOOK_TIMEOUT_S", 10.0)
WEBHOOK_HEADERS: Final[dict[str, str]] = {
    "Content-Type": "application/json",
    "User-Agent": "NekoSignal/1.0",
}

# ---------------------------------------------------------------------------
# Main Scan Loop
# ---------------------------------------------------------------------------

LOOP_INTERVAL_S: Final[float] = _float("LOOP_INTERVAL_S", 60.0)
