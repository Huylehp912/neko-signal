"""
Scoring Engine (The Alpha Brain) for Neko Signal System.

Evaluates five independent market-microstructure conditions on a clean OHLCV
DataFrame and aggregates them into a directional score in the range [-5, +5].

Score Convention:
    Each condition contributes +1 (bullish signal) or -1 (bearish signal).
    A score of 0 from a condition means it is ambiguous / neutral.

    Final score ≥ +4  → Strong LONG bias.
    Final score ≤ -4  → Strong SHORT bias.
    Otherwise        → No signal (market is not sufficiently aligned).

Conditions:
    1. OFI      — Order Flow Imbalance (rolling Taker Buy vs Sell delta).
    2. CVD      — Cumulative Volume Delta trend (slope of rolling OFI sum).
    3. VWAP     — Session VWAP discount (bearish) vs premium (bullish).
    4. Momentum — 5-candle Rate-of-Change combined with L2 orderbook clearance.
    5. Liquidity— Volume Profile HVN zone interaction & Swing H/L sweep detection.

All calculations are fully vectorised using NumPy and Pandas.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from config import (
    CVD_WINDOW,
    HVN_PERCENTILE,
    HVN_PROXIMITY_PCT,
    MOMENTUM_ROC_PERIOD,
    OB_IMBALANCE_FACTOR,
    OB_PROXIMITY_PCT,
    OFI_WINDOW,
    VP_BINS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared Internal Helpers
# ---------------------------------------------------------------------------

def _compute_ofi(df: pd.DataFrame) -> pd.Series:
    """Computes per-candle Order Flow Imbalance (OFI).

    OFI = Taker Buy Volume - Taker Sell Volume.
    Positive → net aggressive buying pressure.
    Negative → net aggressive selling pressure.

    Args:
        df: DataFrame with columns ``[taker_buy_volume, taker_sell_volume]``.

    Returns:
        A ``pd.Series`` of OFI values aligned to ``df.index``.
    """
    return df["taker_buy_volume"] - df["taker_sell_volume"]


def _compute_vwap(df: pd.DataFrame) -> pd.Series:
    """Computes session VWAP using cumulative typical price × volume.

    VWAP = Σ(Typical_Price × Volume) / Σ(Volume)
    where Typical_Price = (High + Low + Close) / 3

    Args:
        df: DataFrame with columns ``[high, low, close, volume]``.

    Returns:
        A ``pd.Series`` of VWAP values aligned to ``df.index``.
    """
    typical_price: pd.Series = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_tp_vol: pd.Series = (typical_price * df["volume"]).cumsum()
    cum_vol: pd.Series = df["volume"].cumsum()
    # Guard against division by zero on the first candle
    return cum_tp_vol / cum_vol.replace(0, np.nan)


def _build_volume_profile(df: pd.DataFrame) -> pd.DataFrame:
    """Builds a coarse price-volume histogram over the full DataFrame.

    Each candle's volume is assigned to the bin containing its close price.
    The result approximates a market profile / footprint chart in O(n) time.

    Args:
        df: DataFrame with columns ``[high, low, close, volume]``.

    Returns:
        A DataFrame with columns ``[price_mid, volume]``,
        sorted ascending by ``price_mid``.
    """
    price_min: float = float(df["low"].min())
    price_max: float = float(df["high"].max())

    if price_max <= price_min:
        return pd.DataFrame({"price_mid": [], "volume": []})

    edges: np.ndarray = np.linspace(price_min, price_max, VP_BINS + 1)
    mids: np.ndarray = (edges[:-1] + edges[1:]) / 2.0

    # Vectorised bin assignment — no Python loops
    indices: np.ndarray = np.clip(
        np.digitize(df["close"].to_numpy(), edges) - 1, 0, VP_BINS - 1
    )
    vol_bins: np.ndarray = np.zeros(VP_BINS, dtype=np.float64)
    np.add.at(vol_bins, indices, df["volume"].to_numpy())

    return pd.DataFrame({"price_mid": mids, "volume": vol_bins})


def _get_hvn_levels(df: pd.DataFrame) -> np.ndarray:
    """Returns price levels classified as High Volume Nodes (HVNs).

    An HVN is a price bin whose accumulated volume exceeds the
    ``HVN_PERCENTILE``-th percentile of all bins.

    Args:
        df: DataFrame with columns ``[high, low, close, volume]``.

    Returns:
        Sorted ``np.ndarray`` of HVN price levels (ascending).
    """
    vp: pd.DataFrame = _build_volume_profile(df)
    if vp.empty:
        return np.array([], dtype=np.float64)

    threshold: float = float(np.percentile(vp["volume"].to_numpy(), HVN_PERCENTILE))
    hvn_mask: np.ndarray = vp["volume"].to_numpy() >= threshold
    return np.sort(vp.loc[hvn_mask, "price_mid"].to_numpy())


# ---------------------------------------------------------------------------
# Condition 1: OFI
# ---------------------------------------------------------------------------

def _score_ofi(df: pd.DataFrame) -> int:
    """Condition 1: Rolling Order Flow Imbalance.

    Computes a smoothed OFI by rolling-averaging the last ``OFI_WINDOW``
    candles. A positive mean indicates sustained aggressive buying pressure.

    Args:
        df: DataFrame with taker-volume columns.

    Returns:
        ``+1`` if net buying pressure, ``-1`` if net selling, ``0`` if flat.
    """
    ofi: pd.Series = _compute_ofi(df)
    rolling_ofi: float = float(
        ofi.rolling(OFI_WINDOW, min_periods=1).mean().iloc[-1]
    )

    if rolling_ofi > 0:
        return 1
    if rolling_ofi < 0:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Condition 2: CVD Trend
# ---------------------------------------------------------------------------

def _score_cvd_trend(df: pd.DataFrame) -> int:
    """Condition 2: Cumulative Volume Delta (CVD) slope.

    CVD is the rolling sum of OFI. We measure its slope by comparing the
    latest CVD value to the value at the midpoint of the rolling window.
    A rising CVD confirms sustained accumulation; a falling CVD confirms
    sustained distribution.

    Args:
        df: DataFrame with taker-volume columns.

    Returns:
        ``+1`` if CVD is rising, ``-1`` if falling, ``0`` if flat.
    """
    ofi: pd.Series = _compute_ofi(df)
    cvd: pd.Series = ofi.rolling(CVD_WINDOW, min_periods=1).sum()

    if len(cvd) < 2:
        return 0

    # Compare the tail to the midpoint of the CVD window
    mid_offset: int = max(1, CVD_WINDOW // 2)
    mid_idx: int = max(0, len(cvd) - mid_offset - 1)
    slope: float = float(cvd.iloc[-1]) - float(cvd.iloc[mid_idx])

    if slope > 0:
        return 1
    if slope < 0:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Condition 3: VWAP Premium / Discount
# ---------------------------------------------------------------------------

def _score_vwap(df: pd.DataFrame) -> int:
    """Condition 3: VWAP Premium or Discount.

    If price is trading *above* VWAP → institutions are paying premium,
    confirming bullish intent (score +1).
    If price is *below* VWAP → selling at discount, bearish bias (score -1).

    Args:
        df: DataFrame with columns ``[high, low, close, volume]``.

    Returns:
        ``+1`` if close > VWAP, ``-1`` if close < VWAP, ``0`` if equal.
    """
    vwap: pd.Series = _compute_vwap(df)
    current_close: float = float(df["close"].iloc[-1])
    current_vwap: float = float(vwap.iloc[-1])

    if np.isnan(current_vwap):
        return 0
    if current_close > current_vwap:
        return 1
    if current_close < current_vwap:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Condition 4: Momentum + Orderbook Clearance
# ---------------------------------------------------------------------------

def _score_momentum_and_orderbook(df: pd.DataFrame, orderbook: dict) -> int:
    """Condition 4: Price momentum combined with L2 orderbook depth clearance.

    Two sub-components, both must agree for a non-zero score:

    **Momentum sub-component**
        Uses a simple ``MOMENTUM_ROC_PERIOD``-candle Rate-of-Change (ROC):
        ``ROC = (close_now - close_n_ago) / close_n_ago``

    **Orderbook clearance sub-component**
        Sums bid and ask liquidity within ``OB_PROXIMITY_PCT`` of mid-price.
        If bid liquidity exceeds ask liquidity by ``OB_IMBALANCE_FACTOR``
        the path upward is considered clear (and vice versa).

    Args:
        df: DataFrame with column ``[close]``.
        orderbook: Dict ``{"bids": [[price, size], ...], "asks": [...]}``

    Returns:
        ``+1`` if momentum and book both lean bullish.
        ``-1`` if both lean bearish.
        ``0`` if signals are mixed or data is missing.
    """
    # --- Momentum (vectorised ROC) ---
    momentum_score: int = 0
    if len(df) >= MOMENTUM_ROC_PERIOD + 1:
        close_now: float = float(df["close"].iloc[-1])
        close_ago: float = float(df["close"].iloc[-(MOMENTUM_ROC_PERIOD + 1)])
        roc: float = (close_now - close_ago) / (close_ago + 1e-12)
        if roc > 0:
            momentum_score = 1
        elif roc < 0:
            momentum_score = -1

    # --- Orderbook Clearance (vectorised slicing) ---
    book_score: int = 0
    bids_raw: list = orderbook.get("bids", []) if orderbook else []
    asks_raw: list = orderbook.get("asks", []) if orderbook else []

    if bids_raw and asks_raw:
        bids: np.ndarray = np.asarray(bids_raw, dtype=np.float64)
        asks: np.ndarray = np.asarray(asks_raw, dtype=np.float64)

        if bids.ndim == 2 and asks.ndim == 2:
            mid: float = (bids[0, 0] + asks[0, 0]) / 2.0
            lower: float = mid * (1.0 - OB_PROXIMITY_PCT)
            upper: float = mid * (1.0 + OB_PROXIMITY_PCT)

            bid_liq: float = bids[bids[:, 0] >= lower, 1].sum()
            ask_liq: float = asks[asks[:, 0] <= upper, 1].sum()

            if bid_liq > ask_liq * OB_IMBALANCE_FACTOR:
                book_score = 1
            elif ask_liq > bid_liq * OB_IMBALANCE_FACTOR:
                book_score = -1

    # Require both sub-components to agree
    if momentum_score == 1 and book_score >= 0:
        return 1
    if momentum_score == -1 and book_score <= 0:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Condition 5: Liquidity Zones
# ---------------------------------------------------------------------------

def _score_liquidity_zones(df: pd.DataFrame) -> int:
    """Condition 5: Volume Profile HVN interaction & Swing H/L sweep detection.

    Two sub-components evaluated in priority order:

    **Swing Sweep (highest priority)**
        If the last candle's high pierced above the lookback swing high but
        closed below it → bearish stop-hunt (bear trap). Score: ``-1``.
        If the last candle's low pierced below the lookback swing low but
        closed above it → bullish liquidity grab (bull trap). Score: ``+1``.

    **HVN Zone (secondary)**
        If the current close is within ``HVN_PROXIMITY_PCT`` of the nearest
        HVN level, and price is above that HVN → support (``+1``).
        If price is below that HVN → resistance (``-1``).

    Args:
        df: DataFrame with columns ``[high, low, close, volume]``.

    Returns:
        ``+1``, ``-1``, or ``0`` based on the detected liquidity condition.
    """
    min_rows: int = 22
    if len(df) < min_rows:
        return 0

    last: pd.Series = df.iloc[-1]
    current_close: float = float(last["close"])
    lookback: int = 20

    # Swing extremes from the prior window (exclude the last candle itself)
    prior: pd.DataFrame = df.iloc[-(lookback + 1):-1]
    swing_high: float = float(prior["high"].max())
    swing_low: float = float(prior["low"].min())

    # --- Sweep Detection ---
    if float(last["high"]) > swing_high and current_close < swing_high:
        logger.debug("Liquidity: Bearish swing-high sweep detected.")
        return -1
    if float(last["low"]) < swing_low and current_close > swing_low:
        logger.debug("Liquidity: Bullish swing-low sweep detected.")
        return 1

    # --- HVN Zone Interaction ---
    hvn_levels: np.ndarray = _get_hvn_levels(df)
    if hvn_levels.size == 0:
        return 0

    # Find the nearest HVN to current price
    distances: np.ndarray = np.abs(hvn_levels - current_close)
    nearest_hvn: float = float(hvn_levels[np.argmin(distances)])
    proximity: float = abs(current_close - nearest_hvn) / (nearest_hvn + 1e-12)

    if proximity <= HVN_PROXIMITY_PCT:
        if current_close >= nearest_hvn:
            logger.debug("Liquidity: Price at HVN support %.4f.", nearest_hvn)
            return 1
        else:
            logger.debug("Liquidity: Price at HVN resistance %.4f.", nearest_hvn)
            return -1

    return 0


# ---------------------------------------------------------------------------
# Public: Aggregate Scorer
# ---------------------------------------------------------------------------

def compute_score(
    df: pd.DataFrame,
    orderbook: Optional[dict] = None,
    symbol: str = "",
) -> int:
    """Computes the aggregate directional score for a single symbol.

    Evaluates all five market conditions and sums their individual votes.
    The result is an integer in ``[-5, +5]``.

    Args:
        df: Clean OHLCV DataFrame indexed by UTC timestamp with columns:
            ``[open, high, low, close, volume, taker_buy_volume, taker_sell_volume]``.
            Minimum of 30 rows required for reliable calculations.
        orderbook: Optional L2 orderbook dict with keys ``"bids"`` and ``"asks"``.
            If ``None``, Condition 4's orderbook sub-component scores 0.

    Returns:
        An integer score in ``[-5, +5]``.
        Returns ``0`` if data is insufficient.

    Example:
        >>> score = compute_score(df, orderbook)
        >>> if score >= 4:
        ...     direction = "LONG"
    """
    if df is None or df.empty or len(df) < 30:
        logger.warning("Scoring skipped: DataFrame has %d rows (need ≥30).",
                       len(df) if df is not None else 0)
        return 0

    ob: dict = orderbook if orderbook is not None else {}

    c1 = _score_ofi(df)
    c2 = _score_cvd_trend(df)
    c3 = _score_vwap(df)
    c4 = _score_momentum_and_orderbook(df, ob)
    c5 = _score_liquidity_zones(df)

    total: int = c1 + c2 + c3 + c4 + c5

    _tag: str = f"[{symbol.split('/')[0]}] " if symbol else ""
    logger.info(
        "%sScore → OFI=%+d | CVD=%+d | VWAP=%+d | Momentum=%+d | Liquidity=%+d | TOTAL=%+d",
        _tag, c1, c2, c3, c4, c5, total,
    )

    return total
