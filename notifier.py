"""
Notifier (The Comms Layer) for Neko Signal System.

Responsible for:
    1. Building a rich, structured JSON signal payload from trade parameters.
    2. Dispatching the payload asynchronously to the configured webhook URL.

Design choices:
    - ``build_signal_payload`` is a pure function — fully testable without I/O.
    - ``send_signal`` accepts an optional shared ``aiohttp.ClientSession`` to
      enable connection pooling when called from the main loop. Falls back to
      creating its own session for standalone usage.
    - All network errors are caught and logged; the function never raises so
      that a webhook failure never crashes the main loop.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from config import (
    PAIR_DISPLAY_NAMES,
    WEBHOOK_HEADERS,
    WEBHOOK_TIMEOUT_S,
    WEBHOOK_URL,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Payload Builder (pure function)
# ---------------------------------------------------------------------------

def build_signal_payload(
    symbol: str,
    direction: str,
    entry: float,
    tp: float,
    sl: float,
    rr: float,
    score: int,
) -> dict:
    """Constructs the structured JSON signal payload for webhook delivery.

    The payload is self-describing and contains all information needed for
    a human or downstream system to act on the signal without consulting
    any other data source.

    Args:
        symbol:    ccxt-formatted trading symbol, e.g. ``"BTC/USDT:USDT"``.
        direction: ``"LONG"`` or ``"SHORT"``.
        entry:     Entry price.
        tp:        Take-profit price level.
        sl:        Stop-loss price level.
        rr:        Risk:Reward ratio (e.g. ``2.35``).
        score:     Directional score in ``[-5, +5]``.

    Returns:
        A ``dict`` ready for ``json.dumps``. Keys follow a stable schema
        versioned via the ``"schema_version"`` field.

    Example payload (LONG on BTCUSDT)::

        {
            "schema_version": "1.0",
            "system": "Neko Signal",
            "timestamp_utc": "2026-02-26T19:05:00+00:00",
            "signal": {
                "pair": "BTCUSDT",
                "direction": "LONG",
                "emoji": "🟢 LONG",
                "entry_price": 95000.12,
                "take_profit": 97000.50,
                "stop_loss": 94000.00,
                "risk_reward": 2.0,
                "score": 4,
                "score_label": "+4/5",
                "score_bar": "████░"
            },
            "meta": {
                "session": "US Killzone",
                "strategy": "Market Microstructure + Volume Profile"
            }
        }
    """
    display_name: str = PAIR_DISPLAY_NAMES.get(symbol, symbol)
    direction_upper: str = direction.upper()
    emoji: str = "🟢 LONG" if direction_upper == "LONG" else "🔴 SHORT"

    # Visual score bar: filled blocks for magnitude, grey for remainder
    abs_score: int = abs(score)
    score_bar: str = "█" * abs_score + "░" * (5 - abs_score)
    score_label: str = f"{'+' if score > 0 else ''}{score}/5"

    return {
        "schema_version": "1.0",
        "system": "Neko Signal",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "signal": {
            "pair": display_name,
            "direction": direction_upper,
            "emoji": emoji,
            "entry_price": round(entry, 8),
            "take_profit": round(tp, 8),
            "stop_loss": round(sl, 8),
            "risk_reward": round(rr, 3),
            "score": score,
            "score_label": score_label,
            "score_bar": score_bar,
        },
        "meta": {
            "session": "US Killzone",
            "strategy": "Market Microstructure + Volume Profile",
        },
    }


# ---------------------------------------------------------------------------
# Async Dispatcher
# ---------------------------------------------------------------------------

async def send_signal(
    symbol: str,
    direction: str,
    entry: float,
    tp: float,
    sl: float,
    rr: float,
    score: int,
    webhook_url: str = WEBHOOK_URL,
    session: Optional[aiohttp.ClientSession] = None,
) -> bool:
    """Builds and POSTs a structured signal payload to the configured webhook.

    Handles both shared-session (production, connection-pooled) and
    standalone-session (testing) modes transparently.

    Args:
        symbol:      ccxt trading symbol.
        direction:   ``"LONG"`` or ``"SHORT"``.
        entry:       Entry price.
        tp:          Take-profit price.
        sl:          Stop-loss price.
        rr:          Risk:Reward ratio.
        score:       Signal score.
        webhook_url: Override the default ``WEBHOOK_URL`` from config.
        session:     Optional shared ``aiohttp.ClientSession``. When provided,
                     this function does **not** close it after use, leaving
                     lifecycle management to the caller. When ``None``, a
                     temporary session is created and closed after the call.

    Returns:
        ``True`` if the webhook responded with HTTP 2xx.
        ``False`` on any network error, timeout, or non-2xx status.
    """
    payload: dict = build_signal_payload(symbol, direction, entry, tp, sl, rr, score)
    json_body: str = json.dumps(payload, ensure_ascii=False, indent=2)

    display: str = PAIR_DISPLAY_NAMES.get(symbol, symbol)
    logger.info(
        "→ Dispatching signal: [%s] %s | Entry=%.8f | TP=%.8f | SL=%.8f | RR=%.3f | Score=%+d",
        display, direction, entry, tp, sl, rr, score,
    )

    timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(total=WEBHOOK_TIMEOUT_S)
    _owns_session: bool = session is None

    try:
        if _owns_session:
            session = aiohttp.ClientSession(headers=WEBHOOK_HEADERS)

        async with session.post(  # type: ignore[union-attr]
            webhook_url,
            data=json_body,
            headers=WEBHOOK_HEADERS,
            timeout=timeout,
        ) as response:
            if response.status < 300:
                logger.info(
                    "✓ Webhook ACK %d for [%s].", response.status, display
                )
                return True
            else:
                error_body: str = await response.text()
                logger.error(
                    "✗ Webhook NACK %d for [%s]: %s", response.status, display, error_body
                )
                return False

    except aiohttp.ServerTimeoutError:
        logger.error("Webhook TIMEOUT for [%s] after %.1fs.", display, WEBHOOK_TIMEOUT_S)
        return False
    except aiohttp.ClientConnectionError as exc:
        logger.error("Webhook CONNECTION ERROR for [%s]: %s", display, exc)
        return False
    except aiohttp.ClientResponseError as exc:
        logger.error("Webhook RESPONSE ERROR for [%s]: %s", display, exc)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected webhook error for [%s]: %s", display, exc)
        return False
    finally:
        if _owns_session and session is not None:
            await session.close()
