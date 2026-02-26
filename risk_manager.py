"""
Risk Manager (The Execution Guard) for Neko Signal System.

Calculates dynamic Take-Profit and Stop-Loss levels rooted in the market's
own liquidity structure, then validates the resulting Risk:Reward ratio.

Design Philosophy:
    Rather than using a fixed ATR multiplier for both TP and SL, this module
    anchors price targets to identifiable liquidity pools:
        - High Volume Nodes (HVNs) from the Volume Profile act as magnet zones.
        - Swing Highs / Lows act as stop-raid levels and natural TP targets.
        - An ATR-derived buffer is applied to SL only, to avoid premature exits.

    A signal is rejected (returns ``None``) if the resulting RR < MIN_RR_RATIO.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from config import (
    ATR_PERIOD,
    ATR_SL_MULTIPLIER,
    HVN_PERCENTILE,
    MIN_RR_RATIO,
    SWING_LOOKBACK,
    VP_BINS,
)

logger = logging.getLogger(__name__)

# Type alias for the returned dict
RiskParams = dict[str, float]


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------

def _compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    """Computes the latest ATR value from the DataFrame.

    Args:
        df: DataFrame with columns ``[high, low, close]``.
        period: Rolling ATR window size.

    Returns:
        Float ATR value of the most recent completed candle, or 0.0 if NaN.
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
    atr_val: float = float(tr.rolling(period, min_periods=period).mean().iloc[-2])
    return atr_val if not np.isnan(atr_val) else 0.0


def _get_hvn_levels(df: pd.DataFrame) -> np.ndarray:
    """Builds a coarse Volume Profile and returns High Volume Node price levels.

    Args:
        df: DataFrame with columns ``[high, low, close, volume]``.

    Returns:
        Sorted ``np.ndarray`` of HVN price levels (ascending).
        Returns an empty array if price range is degenerate.
    """
    price_min: float = float(df["low"].min())
    price_max: float = float(df["high"].max())

    if price_max <= price_min:
        return np.array([], dtype=np.float64)

    edges: np.ndarray = np.linspace(price_min, price_max, VP_BINS + 1)
    mids: np.ndarray = (edges[:-1] + edges[1:]) / 2.0

    indices: np.ndarray = np.clip(
        np.digitize(df["close"].to_numpy(), edges) - 1, 0, VP_BINS - 1
    )
    vol_bins: np.ndarray = np.zeros(VP_BINS, dtype=np.float64)
    np.add.at(vol_bins, indices, df["volume"].to_numpy())

    threshold: float = float(np.percentile(vol_bins, HVN_PERCENTILE))
    return np.sort(mids[vol_bins >= threshold])


def _get_swing_extremes(df: pd.DataFrame, lookback: int = SWING_LOOKBACK) -> tuple[float, float]:
    """Returns the swing high and swing low over the recent lookback window.

    Args:
        df: DataFrame with columns ``[high, low]``.
        lookback: Number of candles to examine.

    Returns:
        ``(swing_high, swing_low)`` as floats.
    """
    window: pd.DataFrame = df.iloc[-lookback:]
    return float(window["high"].max()), float(window["low"].min())


def _find_nearest_above(levels: np.ndarray, price: float) -> Optional[float]:
    """Returns the closest level strictly above ``price``, or ``None``."""
    above: np.ndarray = levels[levels > price]
    return float(above[0]) if above.size > 0 else None


def _find_nearest_below(levels: np.ndarray, price: float) -> Optional[float]:
    """Returns the closest level strictly below ``price``, or ``None``."""
    below: np.ndarray = levels[levels < price]
    return float(below[-1]) if below.size > 0 else None


# ---------------------------------------------------------------------------
# Public: Risk Parameter Calculator
# ---------------------------------------------------------------------------

def calculate_risk_params(
    df: pd.DataFrame,
    direction: str,
    atr_multiplier_sl: float = ATR_SL_MULTIPLIER,
) -> Optional[RiskParams]:
    """Calculates dynamic TP and SL anchored to liquidity structure.

    **LONG trade**:
        - Entry: current close.
        - SL: Highest HVN below entry (or swing low if no HVN below) minus ATR buffer.
        - TP: Lowest HVN above entry (or swing high if no HVN above).

    **SHORT trade**:
        - Entry: current close.
        - SL: Lowest HVN above entry (or swing high if no HVN above) plus ATR buffer.
        - TP: Highest HVN below entry (or swing low if no HVN below).

    The signal is rejected and ``None`` returned if:
        - SL or TP cannot be logically determined.
        - SL and TP are on the wrong side of entry.
        - ``RR = |TP - Entry| / |Entry - SL| < MIN_RR_RATIO``.

    Args:
        df: Clean OHLCV DataFrame with columns
            ``[open, high, low, close, volume]``.
            Minimum of ``ATR_PERIOD + SWING_LOOKBACK`` rows required.
        direction: ``"LONG"`` or ``"SHORT"``.
        atr_multiplier_sl: Multiplier applied to ATR for SL padding.
            Defaults to ``config.ATR_SL_MULTIPLIER``.

    Returns:
        A ``RiskParams`` dict ``{"Entry": float, "SL": float, "TP": float, "RR": float}``
        if the trade passes validation, otherwise ``None``.
    """
    if df is None or df.empty:
        logger.debug("Risk Manager: Empty DataFrame supplied.")
        return None

    if direction not in ("LONG", "SHORT"):
        logger.error("Risk Manager: Invalid direction '%s'. Must be LONG or SHORT.", direction)
        return None

    entry: float = float(df["close"].iloc[-1])
    atr: float = _compute_atr(df)
    sl_buffer: float = atr * atr_multiplier_sl

    hvn_levels: np.ndarray = _get_hvn_levels(df)
    swing_high, swing_low = _get_swing_extremes(df)

    sl: Optional[float] = None
    tp: Optional[float] = None

    if direction == "LONG":
        # SL: best HVN below entry; fall back to swing low
        sl_anchor: Optional[float] = _find_nearest_below(hvn_levels, entry)
        if sl_anchor is None:
            sl_anchor = swing_low
        sl = sl_anchor - sl_buffer

        # TP: nearest HVN above entry; fall back to swing high
        tp = _find_nearest_above(hvn_levels, entry)
        if tp is None:
            tp = swing_high

    else:  # SHORT
        # SL: best HVN above entry; fall back to swing high
        sl_anchor = _find_nearest_above(hvn_levels, entry)
        if sl_anchor is None:
            sl_anchor = swing_high
        sl = sl_anchor + sl_buffer

        # TP: nearest HVN below entry; fall back to swing low
        tp = _find_nearest_below(hvn_levels, entry)
        if tp is None:
            tp = swing_low

    # --- Sanity checks ---
    if sl is None or tp is None:
        logger.debug("Risk Manager: SL or TP could not be determined.")
        return None

    if direction == "LONG" and (sl >= entry or tp <= entry):
        logger.debug(
            "Risk Manager: Invalid LONG geometry — entry=%.6f, SL=%.6f, TP=%.6f.",
            entry, sl, tp,
        )
        return None

    if direction == "SHORT" and (sl <= entry or tp >= entry):
        logger.debug(
            "Risk Manager: Invalid SHORT geometry — entry=%.6f, SL=%.6f, TP=%.6f.",
            entry, sl, tp,
        )
        return None

    risk: float = abs(entry - sl)
    reward: float = abs(tp - entry)

    if risk <= 0.0:
        logger.debug("Risk Manager: Zero or negative risk (SL=%.6f, Entry=%.6f).", sl, entry)
        return None

    rr: float = round(reward / risk, 3)

    if rr < MIN_RR_RATIO:
        logger.debug(
            "Risk Manager: RR=%.3f < MIN_RR=%.1f. Signal REJECTED.", rr, MIN_RR_RATIO
        )
        return None

    logger.debug(
        "Risk Manager: %s APPROVED — Entry=%.6f | SL=%.6f | TP=%.6f | RR=%.3f.",
        direction, entry, sl, tp, rr,
    )

    return {
        "Entry": round(entry, 8),
        "SL": round(sl, 8),
        "TP": round(tp, 8),
        "RR": rr,
    }
