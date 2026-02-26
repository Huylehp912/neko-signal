"""
Logic Filters (The Gatekeeper) for Neko Signal System.

Two sequential gates guard the signal pipeline against noisy or
manipulated market conditions:

    Gate 1 — Session Killzone:
        Rejects any scan that occurs outside the defined UTC trading window.

    Gate 2 — Anti-Wash Trading:
        Three fully-vectorised sub-filters detect low-quality candles caused
        by wash trades, algos printing synthetic volume, or dead-market chop.

Both gates are stateless pure functions and can be unit-tested in isolation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from config import (
    ATR_MA_PERIOD,
    ATR_PERIOD,
    MIN_VOLUME_EFFICIENCY,
    SESSION_END_UTC,
    SESSION_START_UTC,
    WASH_TRADE_TAKER_RATIO_HIGH,
    WASH_TRADE_TAKER_RATIO_LOW,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared Vectorized Helper
# ---------------------------------------------------------------------------

def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Computes Average True Range (ATR) using vectorized Pandas operations.

    ATR is the rolling mean of True Range:
        TR = max(High - Low, |High - Prev_Close|, |Low - Prev_Close|)

    Args:
        df: DataFrame containing columns ``[high, low, close]``.
        period: Rolling window size for the ATR calculation.

    Returns:
        A ``pd.Series`` of ATR values aligned to ``df.index``.
        Early rows where insufficient data exists will be ``NaN``.
    """
    prev_close: pd.Series = df["close"].shift(1)
    tr: pd.Series = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


# ---------------------------------------------------------------------------
# Gate 1: Session Killzone
# ---------------------------------------------------------------------------

def gate_session_killzone(utc_now: datetime | None = None) -> bool:
    """Gate 1: Passes only when the current UTC time is within the Killzone.

    The active session window is defined in ``config.py`` as
    ``[SESSION_START_UTC, SESSION_END_UTC)`` (half-open interval).

    Args:
        utc_now: UTC-aware ``datetime`` override for testing. Defaults to
            ``datetime.now(timezone.utc)``.

    Returns:
        ``True`` if within the active Killzone, ``False`` otherwise.

    Example:
        >>> gate_session_killzone()          # Live call
        True
        >>> from datetime import datetime, timezone
        >>> gate_session_killzone(datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc))
        True
    """
    if utc_now is None:
        utc_now = datetime.now(timezone.utc)

    hour: int = utc_now.hour
    in_session: bool = SESSION_START_UTC <= hour < SESSION_END_UTC

    if not in_session:
        logger.debug(
            "Gate 1 FAIL | UTC hour %02d is outside Killzone [%02d:00, %02d:00).",
            hour,
            SESSION_START_UTC,
            SESSION_END_UTC,
        )
    else:
        logger.debug(
            "Gate 1 PASS | UTC hour %02d is within Killzone.", hour
        )

    return in_session


# ---------------------------------------------------------------------------
# Gate 2: Anti-Wash Trading
# ---------------------------------------------------------------------------

def gate_anti_wash_trading(df: pd.DataFrame) -> bool:
    """Gate 2: Rejects candles that exhibit wash-trading or synthetic-volume signatures.

    Applies three independent sub-filters against the last *completed* candle
    (``df.iloc[-2]`` avoids reacting to an incomplete live candle):

    **Sub-filter 1 — Volume Efficiency**
        ``|Close - Open| / Volume`` must exceed ``MIN_VOLUME_EFFICIENCY``.
        Very low efficiency means the candle moved little relative to its
        volume, a classic signature of circular/wash trading.

    **Sub-filter 2 — ATR Trend**
        The latest ATR must be ≥ its own ``ATR_MA_PERIOD``-period rolling
        mean. A shrinking ATR signals decreasing true volatility, meaning
        the market is in a dead zone unworthy of a signal.

    **Sub-filter 3 — Taker Balance Ratio**
        ``Taker_Buy_Volume / Total_Volume`` must be outside the band
        ``[WASH_TRADE_TAKER_RATIO_LOW, WASH_TRADE_TAKER_RATIO_HIGH]``.
        A perfectly balanced 49–51% split is statistically unlikely in real
        directional flow; it strongly suggests automated volume-printing.

    Args:
        df: DataFrame with columns
            ``[open, high, low, close, volume, taker_buy_volume]``.
            Minimum required rows: ``ATR_MA_PERIOD + ATR_PERIOD + 2``.

    Returns:
        ``True`` if the candle passes all three sub-filters (clean volume).
        ``False`` if any sub-filter detects suspicious activity.
    """
    min_required: int = ATR_MA_PERIOD + ATR_PERIOD + 2
    if df.empty or len(df) < min_required:
        logger.warning(
            "Gate 2 SKIP | Insufficient rows: %d < %d required.",
            len(df),
            min_required,
        )
        return False

    # Use the last *completed* candle (index -2) to avoid reacting to a
    # candle that is still forming in real time.
    candle: pd.Series = df.iloc[-2]

    # ------------------------------------------------------------------ #
    # Sub-filter 1: Volume Efficiency                                      #
    # ------------------------------------------------------------------ #
    total_volume: float = candle["volume"]
    if total_volume <= 0.0:
        logger.debug("Gate 2 FAIL | Zero volume detected.")
        return False

    price_displacement: float = abs(float(candle["close"]) - float(candle["open"]))
    volume_efficiency: float = price_displacement / total_volume

    if volume_efficiency < MIN_VOLUME_EFFICIENCY:
        logger.debug(
            "Gate 2 FAIL | Low Volume Efficiency: %.8f < %.8f threshold.",
            volume_efficiency,
            MIN_VOLUME_EFFICIENCY,
        )
        return False

    # ------------------------------------------------------------------ #
    # Sub-filter 2: ATR Trend (fully vectorised over entire df)            #
    # ------------------------------------------------------------------ #
    atr_series: pd.Series = compute_atr(df, period=ATR_PERIOD)
    atr_ma_series: pd.Series = atr_series.rolling(
        ATR_MA_PERIOD, min_periods=ATR_MA_PERIOD
    ).mean()

    latest_atr: float = float(atr_series.iloc[-2])
    latest_atr_ma: float = float(atr_ma_series.iloc[-2])

    if np.isnan(latest_atr) or np.isnan(latest_atr_ma):
        logger.debug(
            "Gate 2 FAIL | ATR or ATR_MA is NaN (not enough warm-up data)."
        )
        return False

    if latest_atr < latest_atr_ma:
        logger.debug(
            "Gate 2 FAIL | ATR contracting: ATR=%.5f < ATR_MA=%.5f.",
            latest_atr,
            latest_atr_ma,
        )
        return False

    # ------------------------------------------------------------------ #
    # Sub-filter 3: Taker Buy Ratio                                        #
    # ------------------------------------------------------------------ #
    taker_buy: float = float(candle["taker_buy_volume"])
    taker_ratio: float = taker_buy / total_volume

    if WASH_TRADE_TAKER_RATIO_LOW <= taker_ratio <= WASH_TRADE_TAKER_RATIO_HIGH:
        logger.debug(
            "Gate 2 FAIL | Suspiciously balanced taker ratio: %.4f in [%.2f, %.2f].",
            taker_ratio,
            WASH_TRADE_TAKER_RATIO_LOW,
            WASH_TRADE_TAKER_RATIO_HIGH,
        )
        return False

    logger.debug(
        "Gate 2 PASS | Efficiency=%.8f | ATR=%.5f/%.5f | TakerRatio=%.4f.",
        volume_efficiency,
        latest_atr,
        latest_atr_ma,
        taker_ratio,
    )
    return True
