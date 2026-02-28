"""
Main Orchestrator (Live) for Neko Signal System.

This is the async entry point that wires every module together into a
continuous, fault-tolerant scanning loop.

Pipeline (per pair, per cycle):
    ┌─────────────────────────────────────────────────┐
    │  1. Gate 1 — Session Killzone (UTC check)        │
    │  2. Data Fetch — OHLCV + L2 Orderbook            │
    │  3. Gate 2 — Anti-Wash Trading filter            │
    │  4. State Update — resolve TP/SL on open trades  │
    │  5. Skip if pair already has an open position    │
    │  6. Scoring Engine — compute directional score   │
    │  7. Threshold check — score ≥ +4 or ≤ -4?       │
    │  8. Risk Manager — validate TP/SL and RR         │
    │  9. State Manager — lock the pair                │
    │ 10. Notifier — POST signal to webhook            │
    │ 11. Metrics Exporter — write to InfluxDB         │
    └─────────────────────────────────────────────────┘

All five pairs are processed **concurrently** via ``asyncio.gather``, sharing
a single exchange connection and a single HTTP session for efficiency.
The InfluxDB exporter is also shared so connections are pooled.

Run:
    python main_live.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

import aiohttp
import ccxt.async_support as ccxt

from config import (
    LOOP_INTERVAL_S,
    SCORE_LONG_THRESHOLD,
    SCORE_SHORT_THRESHOLD,
    TRADING_PAIRS,
    WEBHOOK_HEADERS,
)
from data_ingestion import create_exchange, fetch_extended_ohlcv, fetch_orderbook
from logic_filters import gate_anti_wash_trading, gate_session_killzone
from notifier import send_signal
from metrics_exporter import InfluxDBExporter
from risk_manager import RiskParams, calculate_risk_params
from scoring_engine import compute_score
from state_manager import StateManager

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    """Configures root logger to write to both stdout and a rotating log file."""
    fmt = "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            "neko_signal.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5,              # keep last 5 rotated files
            encoding="utf-8",
        ),
    ]

    logging.basicConfig(level=logging.INFO, format=fmt, datefmt=datefmt, handlers=handlers)

    # Suppress overly verbose third-party noise
    for lib in ("ccxt", "aiohttp", "asyncio"):
        logging.getLogger(lib).setLevel(logging.WARNING)


logger = logging.getLogger("neko.main")


# ---------------------------------------------------------------------------
# Per-Pair Pipeline Coroutine
# ---------------------------------------------------------------------------

async def _process_pair(
    symbol: str,
    exchange: ccxt.Exchange,
    state_manager: StateManager,
    http_session: aiohttp.ClientSession,
    exporter: InfluxDBExporter,
) -> None:
    """Executes the full signal pipeline for a single trading pair.

    This coroutine is stateless per invocation — all shared state is accessed
    through ``state_manager``. Exceptions are caught at the outermost level so
    that a failure in one pair never interrupts other concurrent pairs.

    Args:
        symbol:        ccxt-formatted trading symbol.
        exchange:      Shared async ccxt exchange instance.
        state_manager: Shared ``StateManager`` instance.
        http_session:  Shared ``aiohttp.ClientSession`` for webhook delivery.
        exporter:      Shared ``InfluxDBExporter`` for metrics telemetry.
    """
    tag: str = f"[{symbol.split('/')[0]}]"  # e.g. "[BTC]" for clean logs

    try:
        # ------------------------------------------------------------------ #
        # Step 1: Gate 1 — Session Killzone                                   #
        # ------------------------------------------------------------------ #
        # if not gate_session_killzone():
        #     logger.info("%s Gate 1 FAIL: Outside killzone (12:00–21:00 UTC).", tag)
        #     return

        # ------------------------------------------------------------------ #
        # Step 2: Fetch Data                                                  #
        # ------------------------------------------------------------------ #
        df, orderbook = await asyncio.gather(
            fetch_extended_ohlcv(exchange, symbol),
            fetch_orderbook(exchange, symbol),
        )

        if df is None or df.empty:
            logger.warning("%s Skipping: OHLCV fetch failed.", tag)
            return

        _last_vol = float(df["volume"].iloc[-1])
        logger.info(
            "%s OHLCV | %d candles | Close=%.4f | Vol=%.2f | TakerBuy=%.1f%%",
            tag, len(df),
            float(df["close"].iloc[-1]),
            _last_vol,
            float(df["taker_buy_volume"].iloc[-1]) / (_last_vol + 1e-12) * 100,
        )

        # ------------------------------------------------------------------ #
        # Step 3: Gate 2 — Anti-Wash Trading                                  #
        # ------------------------------------------------------------------ #
        if not gate_anti_wash_trading(df, symbol):
            logger.info("%s Gate 2 FAIL: Wash-trading signature detected.", tag)
            return

        # ------------------------------------------------------------------ #
        # Step 4: Update Virtual Positions (check TP/SL on open trades)       #
        # ------------------------------------------------------------------ #
        current_price: float = float(df["close"].iloc[-1])
        state_manager.update_virtual_positions({symbol: current_price})

        # ------------------------------------------------------------------ #
        # Step 5: Skip if the pair is already in an open position             #
        # ------------------------------------------------------------------ #
        if not state_manager.is_idle(symbol):
            logger.info(
                "%s Position open (%s). Awaiting TP/SL. Skipping new signal.",
                tag, state_manager.get_state(symbol).name,
            )
            # Still export metrics every cycle regardless of position state
            await exporter.export_live_metrics(
                symbol=symbol,
                df=df,
                score=0,
                trade_state=state_manager.get_state(symbol).name,
            )
            return

        # ------------------------------------------------------------------ #
        # Step 6: Scoring Engine                                              #
        # ------------------------------------------------------------------ #
        score: int = compute_score(df, orderbook, symbol)
        logger.info("%s Score = %+d", tag, score)

        # ------------------------------------------------------------------ #
        # Step 7: Threshold Check                                             #
        # ------------------------------------------------------------------ #
        direction: Optional[str] = None
        if score >= SCORE_LONG_THRESHOLD:
            direction = "LONG"
        elif score <= SCORE_SHORT_THRESHOLD:
            direction = "SHORT"
        else:
            logger.info(
                "%s Score %+d did not meet threshold (±%d). No signal.",
                tag, score, SCORE_LONG_THRESHOLD,
            )
            # Export metrics even when no signal fires
            await exporter.export_live_metrics(
                symbol=symbol,
                df=df,
                score=score,
                trade_state="IDLE",
            )
            return

        # ------------------------------------------------------------------ #
        # Step 8: Risk Manager — validate TP/SL/RR                           #
        # ------------------------------------------------------------------ #
        risk_params: Optional[RiskParams] = calculate_risk_params(df, direction)
        if risk_params is None:
            logger.info(
                "%s %s signal (score=%+d) REJECTED by Risk Manager.", tag, direction, score
            )
            return

        # ------------------------------------------------------------------ #
        # Step 9: Lock the Pair via State Manager                             #
        # ------------------------------------------------------------------ #
        locked: bool = state_manager.lock_pair(
            symbol=symbol,
            direction=direction,
            entry=risk_params["Entry"],
            tp=risk_params["TP"],
            sl=risk_params["SL"],
            rr=risk_params["RR"],
            score=score,
        )
        if not locked:
            # Race condition guard: another logic path locked it first
            return

        # ------------------------------------------------------------------ #
        # Step 10: Dispatch Notification                                      #
        # ------------------------------------------------------------------ #
        await send_signal(
            symbol=symbol,
            direction=direction,
            entry=risk_params["Entry"],
            tp=risk_params["TP"],
            sl=risk_params["SL"],
            rr=risk_params["RR"],
            score=score,
            session=http_session,
        )

        # ------------------------------------------------------------------ #
        # Step 11: Export Telemetry to InfluxDB (non-blocking, fire-and-forget)
        # ------------------------------------------------------------------ #
        await exporter.export_live_metrics(
            symbol=symbol,
            df=df,
            score=score,
            trade_state=direction,  # "LONG" or "SHORT" just confirmed
        )

    except asyncio.CancelledError:
        # Propagate cancellation so the outer loop can shut down cleanly
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s Unhandled exception in pipeline: %s", tag, exc)


# ---------------------------------------------------------------------------
# Main Scanner Loop
# ---------------------------------------------------------------------------

async def run_scanner() -> None:
    """Async main loop: initialises shared resources and drives all pairs.

    Architecture:
        - A **single** ccxt exchange instance is shared across all pairs to
          respect Binance's rate limits via ccxt's built-in semaphore.
        - A **single** ``aiohttp.ClientSession`` is shared for connection pooling.
        - ``asyncio.gather`` with ``return_exceptions=True`` ensures that a
          crash in one pair's coroutine is logged but does not abort others.
        - The loop self-adjusts its sleep duration to maintain a constant
          ``LOOP_INTERVAL_S``-second cadence regardless of processing time.
    """
    _configure_logging()

    logger.info("=" * 65)
    logger.info("  NekoSignal — Enterprise Multi-Pair Signal Engine")
    logger.info("  Pairs    : %s", [p.split("/")[0] for p in TRADING_PAIRS])
    logger.info("  Interval : %.0f seconds", LOOP_INTERVAL_S)
    logger.info("  Session  : 12:00–21:00 UTC (US Killzone)")
    logger.info("=" * 65)

    state_manager = StateManager(pairs=TRADING_PAIRS)
    exchange: ccxt.Exchange = create_exchange()
    logger.info("Connecting to Binance USDM Futures ...")
    await exchange.load_markets()
    logger.info("✓ Binance connected — %d instruments loaded.", len(exchange.markets))
    exporter: InfluxDBExporter = InfluxDBExporter()

    async with aiohttp.ClientSession(headers=WEBHOOK_HEADERS) as http_session:
        try:
            cycle: int = 0
            while True:
                cycle += 1
                cycle_start: datetime = datetime.now(timezone.utc)

                logger.info(
                    "━━━ Cycle #%d | %s ━━━",
                    cycle,
                    cycle_start.strftime("%Y-%m-%d %H:%M:%S UTC"),
                )
                logger.info("Portfolio: %s", state_manager.get_all_states())

                # Run all pair pipelines concurrently
                results = await asyncio.gather(
                    *[
                        _process_pair(symbol, exchange, state_manager, http_session, exporter)
                        for symbol in TRADING_PAIRS
                    ],
                    return_exceptions=True,
                )

                # Surface any unexpected exceptions returned by gather
                for symbol, result in zip(TRADING_PAIRS, results):
                    if isinstance(result, Exception):
                        logger.error(
                            "[%s] gather returned exception: %s", symbol, result
                        )

                elapsed: float = (
                    datetime.now(timezone.utc) - cycle_start
                ).total_seconds()
                sleep_for: float = max(0.0, LOOP_INTERVAL_S - elapsed)

                logger.info(
                    "Cycle #%d complete in %.2fs. Next cycle in %.2fs.",
                    cycle, elapsed, sleep_for,
                )
                await asyncio.sleep(sleep_for)

        except asyncio.CancelledError:
            logger.info("Scanner task cancelled — initiating graceful shutdown.")
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received — shutting down.")
        finally:
            await exporter.close()
            await exchange.close()
            logger.info("Exchange connection closed. NekoSignal stopped. Goodbye. 🐾")


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        asyncio.run(run_scanner())
    except KeyboardInterrupt:
        pass  # Clean exit on Ctrl-C; logging already handled inside run_scanner
