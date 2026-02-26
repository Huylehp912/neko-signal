"""
Metrics Exporter (The Telemetry Layer) for Neko Signal System.

Ships real-time market microstructure indicators and trade-state tags to
an InfluxDB v2 time-series database using the fully **async** client
(``InfluxDBClientAsync``).  All writes are fire-and-forget — database
unavailability is logged as a WARNING and silently swallowed so that the
trading pipeline is **never** blocked or crashed by telemetry issues.

Data Model
----------
Measurement : ``market_data``
Tags        : ``symbol``      — e.g. "BTCUSDT"
              ``trade_state`` — "IDLE" | "LONG" | "SHORT"
Fields      : ``close``       — last close price (float)
              ``volume``      — total candle volume (float)
              ``ofi``         — Order Flow Imbalance: Taker Buy − Taker Sell (float)
              ``cvd``         — Cumulative Volume Delta (rolling OFI sum) (float)
              ``vwap``        — session VWAP (float)
              ``score``       — directional score in [-5, +5] (int → stored as float)

Grafana reads this bucket via the **Flux** query language.
See ``GRAFANA_GUIDE.md`` for ready-to-paste dashboard queries.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
from influxdb_client.client.write_api import ASYNCHRONOUS
from influxdb_client.domain.write_precision import WritePrecision

from config import (
    CVD_WINDOW,
    INFLUXDB_BUCKET,
    INFLUXDB_ORG,
    INFLUXDB_TOKEN,
    INFLUXDB_URL,
    OFI_WINDOW,
    PAIR_DISPLAY_NAMES,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal Indicator Helpers (vectorised, re-used from scoring_engine logic)
# ---------------------------------------------------------------------------

def _compute_ofi_last(df: pd.DataFrame) -> float:
    """Returns the rolling-mean OFI value for the last completed candle.

    Args:
        df: DataFrame with ``[taker_buy_volume, taker_sell_volume]`` columns.

    Returns:
        Rolling OFI float, or 0.0 if data is insufficient.
    """
    if len(df) < OFI_WINDOW:
        return 0.0
    ofi: pd.Series = df["taker_buy_volume"] - df["taker_sell_volume"]
    return float(ofi.rolling(OFI_WINDOW, min_periods=1).mean().iloc[-1])


def _compute_cvd_last(df: pd.DataFrame) -> float:
    """Returns the last Cumulative Volume Delta (rolling OFI sum) value.

    Args:
        df: DataFrame with ``[taker_buy_volume, taker_sell_volume]`` columns.

    Returns:
        Rolling CVD float, or 0.0 if data is insufficient.
    """
    if len(df) < 2:
        return 0.0
    ofi: pd.Series = df["taker_buy_volume"] - df["taker_sell_volume"]
    return float(ofi.rolling(CVD_WINDOW, min_periods=1).sum().iloc[-1])


def _compute_vwap_last(df: pd.DataFrame) -> float:
    """Returns the current session VWAP.

    Args:
        df: DataFrame with ``[high, low, close, volume]`` columns.

    Returns:
        VWAP float, or the latest close price if computation fails.
    """
    typical_price: pd.Series = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_tp_vol: float = float((typical_price * df["volume"]).sum())
    cum_vol: float = float(df["volume"].sum())
    if cum_vol <= 0.0:
        return float(df["close"].iloc[-1])
    return cum_tp_vol / cum_vol


# ---------------------------------------------------------------------------
# InfluxDB Exporter Class
# ---------------------------------------------------------------------------

class InfluxDBExporter:
    """Async telemetry exporter that writes live market metrics to InfluxDB v2.

    Designed for use as a **shared singleton** across all pair coroutines in
    the main loop.  The underlying ``InfluxDBClientAsync`` maintains its own
    internal connection pool; consumers should call ``close()`` on shutdown.

    Usage::

        exporter = InfluxDBExporter()
        await exporter.export_live_metrics(symbol, df, score, trade_state)
        # ... at shutdown:
        await exporter.close()
    """

    def __init__(
        self,
        url: str = INFLUXDB_URL,
        token: str = INFLUXDB_TOKEN,
        org: str = INFLUXDB_ORG,
        bucket: str = INFLUXDB_BUCKET,
    ) -> None:
        """Initialises the async InfluxDB client.

        Credentials default to values loaded from ``.env`` via ``config.py``.
        All parameters can be overridden for testing.

        Args:
            url:    InfluxDB v2 URL, e.g. ``"http://localhost:8086"``.
            token:  InfluxDB API token with write permission to the bucket.
            org:    InfluxDB organisation name.
            bucket: Destination bucket for ``market_data`` measurements.
        """
        self._url: str = url
        self._token: str = token
        self._org: str = org
        self._bucket: str = bucket
        self._client: Optional[InfluxDBClientAsync] = None
        self._enabled: bool = bool(url and token and org and bucket)

        if not self._enabled:
            logger.warning(
                "InfluxDBExporter: Missing credentials in config/env. "
                "Telemetry is DISABLED — bot will continue without metrics."
            )
        else:
            logger.info(
                "InfluxDBExporter: Initialised → %s | org=%s | bucket=%s",
                url, org, bucket,
            )

    def _get_client(self) -> InfluxDBClientAsync:
        """Returns (and lazily creates) the shared async client instance."""
        if self._client is None:
            self._client = InfluxDBClientAsync(
                url=self._url,
                token=self._token,
                org=self._org,
            )
        return self._client

    async def export_live_metrics(
        self,
        symbol: str,
        df: pd.DataFrame,
        score: int,
        trade_state: str,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Writes a single ``market_data`` data point to InfluxDB.

        This method **never raises**. All exceptions are caught and logged at
        WARNING level so the main trading loop is never interrupted.

        Data point schema::

            measurement: market_data
            tags:
                symbol      = "BTCUSDT"
                trade_state = "IDLE" | "LONG" | "SHORT"
            fields:
                close  = 95123.45   (float)
                volume = 1234.56    (float)
                ofi    = 45.23      (float, rolling Taker Buy − Sell mean)
                cvd    = 312.10     (float, rolling OFI cumsum)
                vwap   = 95050.00   (float, session VWAP)
                score  = 4.0        (float, directional score cast from int)
            timestamp: UTC nanoseconds

        Args:
            symbol:      ccxt trading symbol (e.g. ``"BTC/USDT:USDT"``).
            df:          Clean OHLCV DataFrame after Gate 2 passes.
            score:       Directional score from the scoring engine [-5, +5].
            trade_state: Current state string: ``"IDLE"``, ``"LONG"``,
                         or ``"SHORT"``.
            timestamp:   Override timestamp (UTC-aware). Defaults to now.
        """
        if not self._enabled:
            return

        if df is None or df.empty:
            logger.debug("InfluxDB: empty DataFrame, skipping write.")
            return

        ts: datetime = timestamp or datetime.now(timezone.utc)
        display: str = PAIR_DISPLAY_NAMES.get(symbol, symbol.split("/")[0])

        # Compute indicators
        close: float = float(df["close"].iloc[-1])
        volume: float = float(df["volume"].iloc[-1])
        ofi: float = _compute_ofi_last(df)
        cvd: float = _compute_cvd_last(df)
        vwap: float = _compute_vwap_last(df)

        # Build line-protocol point as a dict (influxdb-client accepts dicts)
        point: dict = {
            "measurement": "market_data",
            "tags": {
                "symbol": display,
                "trade_state": trade_state.upper(),
            },
            "fields": {
                "close": close,
                "volume": volume,
                "ofi": ofi,
                "cvd": cvd,
                "vwap": vwap,
                "score": float(score),
            },
            "time": ts,
        }

        try:
            client: InfluxDBClientAsync = self._get_client()
            write_api = client.write_api()
            await write_api.write(
                bucket=self._bucket,
                org=self._org,
                record=point,
                precision=WritePrecision.SECONDS,
            )
            logger.debug(
                "InfluxDB ✓ [%s] state=%s close=%.4f score=%+d ofi=%.2f",
                display, trade_state, close, score, ofi,
            )

        except Exception as exc:  # noqa: BLE001
            # ⚠️  Intentionally broad: telemetry failure must NEVER crash the bot.
            logger.warning(
                "InfluxDB write FAILED for [%s] — %s: %s. "
                "Trading loop continues unaffected.",
                display,
                type(exc).__name__,
                exc,
            )

    async def close(self) -> None:
        """Gracefully closes the underlying async InfluxDB client connection.

        Should be called once during application shutdown (``finally`` block
        in ``main_live.py``).
        """
        if self._client is not None:
            try:
                await self._client.close()
                logger.info("InfluxDBExporter: connection closed.")
            except Exception as exc:  # noqa: BLE001
                logger.warning("InfluxDBExporter: error on close — %s", exc)
            finally:
                self._client = None
