"""
State Manager (The Portfolio Tracker) for Neko Signal System.

Implements a virtual State Machine that tracks every pair's lifecycle:

    IDLE → (signal fires) → LONG or SHORT → (TP/SL hit) → IDLE

The manager is intentionally synchronous and single-threaded — it is designed
for use within a single asyncio event loop where all pair coroutines share the
same thread. No locks are required because asyncio's cooperative multitasking
guarantees that state mutations are never concurrent.

Components:
    - ``PairState`` enum:       Represents the three possible pair states.
    - ``VirtualPosition`` DC:   Immutable snapshot of an open virtual trade.
    - ``StateManager`` class:   The central registry managing all pairs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from config import TRADING_PAIRS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class PairState(Enum):
    """Possible lifecycle states for a trading pair.

    Attributes:
        IDLE:  No open position. The pair is eligible to receive a new signal.
        LONG:  A virtual long position is currently open.
        SHORT: A virtual short position is currently open.
    """
    IDLE = auto()
    LONG = auto()
    SHORT = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VirtualPosition:
    """Immutable record of an open virtual (paper) position.

    Frozen to prevent accidental in-place mutation. To update, replace the
    entire record in the StateManager's internal registry.

    Attributes:
        symbol:    ccxt-formatted trading symbol, e.g. ``"BTC/USDT:USDT"``.
        direction: ``"LONG"`` or ``"SHORT"``.
        entry:     Entry price at the time the signal fired.
        tp:        Take-profit price level.
        sl:        Stop-loss price level.
        rr:        Risk:Reward ratio.
        score:     Directional score that triggered this signal.
    """
    symbol: str
    direction: str
    entry: float
    tp: float
    sl: float
    rr: float
    score: int


# ---------------------------------------------------------------------------
# State Manager
# ---------------------------------------------------------------------------

class StateManager:
    """Central virtual State Machine for all tracked trading pairs.

    Manages transitions between IDLE ↔ LONG / SHORT, stores open virtual
    positions, and automatically resolves them when TP or SL is breached.

    Typical usage pattern in the main loop::

        sm = StateManager()

        # Before scoring: check if the pair can receive a new signal
        if sm.is_idle(symbol):
            score = compute_score(df)
            risk = calculate_risk_params(df, direction)
            sm.lock_pair(symbol, direction, **risk, score=score)

        # Every cycle: check if any open position has hit its target
        sm.update_virtual_positions({"BTC/USDT:USDT": 70_000.0, ...})
    """

    def __init__(self, pairs: list[str] = TRADING_PAIRS) -> None:
        """Initialises all pairs to the IDLE state.

        Args:
            pairs: List of ccxt-formatted trading symbols to track.
        """
        self._states: dict[str, PairState] = {p: PairState.IDLE for p in pairs}
        self._positions: dict[str, Optional[VirtualPosition]] = {p: None for p in pairs}
        logger.info("StateManager initialised for %d pairs: %s", len(pairs), pairs)

    # ------------------------------------------------------------------ #
    # Accessors                                                            #
    # ------------------------------------------------------------------ #

    def get_state(self, symbol: str) -> PairState:
        """Returns the current ``PairState`` for a symbol.

        Args:
            symbol: ccxt trading symbol.

        Returns:
            The current ``PairState``. Defaults to ``IDLE`` for unknown symbols.
        """
        return self._states.get(symbol, PairState.IDLE)

    def is_idle(self, symbol: str) -> bool:
        """Checks whether a pair is available for a new signal.

        Args:
            symbol: ccxt trading symbol.

        Returns:
            ``True`` if the pair has no open virtual position, else ``False``.
        """
        return self._states.get(symbol) == PairState.IDLE

    def get_position(self, symbol: str) -> Optional[VirtualPosition]:
        """Returns the open virtual position for a symbol, if any.

        Args:
            symbol: ccxt trading symbol.

        Returns:
            ``VirtualPosition`` if a position is open, else ``None``.
        """
        return self._positions.get(symbol)

    def get_all_states(self) -> dict[str, str]:
        """Returns a human-readable summary of all pair states.

        Useful for logging and monitoring dashboards.

        Returns:
            Dict mapping each symbol to its state name string.
        """
        return {sym: state.name for sym, state in self._states.items()}

    # ------------------------------------------------------------------ #
    # Mutators                                                             #
    # ------------------------------------------------------------------ #

    def lock_pair(
        self,
        symbol: str,
        direction: str,
        entry: float,
        tp: float,
        sl: float,
        rr: float,
        score: int,
    ) -> bool:
        """Transitions a pair from IDLE → LONG or SHORT after a signal fires.

        Does nothing and returns ``False`` if the pair is already locked,
        preventing double-entry on the same signal cycle.

        Args:
            symbol:    ccxt trading symbol.
            direction: ``"LONG"`` or ``"SHORT"``.
            entry:     Entry price.
            tp:        Take-profit price.
            sl:        Stop-loss price.
            rr:        Risk:Reward ratio.
            score:     Signal score that authorised this trade.

        Returns:
            ``True`` if the pair was successfully locked.
            ``False`` if the pair was already in a non-IDLE state.
        """
        if not self.is_idle(symbol):
            logger.warning(
                "lock_pair: %s already in state %s. Ignoring duplicate signal.",
                symbol, self._states[symbol].name,
            )
            return False

        new_state: PairState = PairState.LONG if direction == "LONG" else PairState.SHORT
        self._states[symbol] = new_state
        self._positions[symbol] = VirtualPosition(
            symbol=symbol,
            direction=direction,
            entry=entry,
            tp=tp,
            sl=sl,
            rr=rr,
            score=score,
        )

        logger.info(
            "LOCKED → [%s] %s | Entry=%.8f | TP=%.8f | SL=%.8f | RR=%.3f | Score=%+d",
            symbol, direction, entry, tp, sl, rr, score,
        )
        return True

    def unlock_pair(self, symbol: str, reason: str = "manual") -> None:
        """Resets a pair to IDLE and clears its virtual position record.

        Args:
            symbol: ccxt trading symbol.
            reason: Human-readable reason logged alongside the state change
                    (e.g., ``"TP_HIT"``, ``"SL_HIT"``, ``"manual"``).
        """
        prev: PairState = self._states.get(symbol, PairState.IDLE)
        self._states[symbol] = PairState.IDLE
        self._positions[symbol] = None

        logger.info(
            "UNLOCKED → [%s] (was %s). Reason: %s", symbol, prev.name, reason
        )

    def update_virtual_positions(self, current_prices: dict[str, float]) -> None:
        """Evaluates all open virtual positions against the latest prices.

        Iterates over every locked pair and checks whether the current market
        price has breached the TP or SL level. If a level is hit, the pair is
        automatically unlocked and returned to IDLE.

        This method should be called once per scan cycle *before* any new
        signal scoring to ensure the position state is current.

        Args:
            current_prices: Mapping of ccxt symbol → current market price.
                            Pairs absent from this dict are silently skipped.
        """
        for symbol, position in list(self._positions.items()):
            if position is None:
                continue

            price: Optional[float] = current_prices.get(symbol)
            if price is None:
                continue

            if position.direction == "LONG":
                if price >= position.tp:
                    logger.info(
                        "TP HIT → [%s] LONG closed at %.8f (TP=%.8f).", symbol, price, position.tp
                    )
                    self.unlock_pair(symbol, reason="TP_HIT")
                elif price <= position.sl:
                    logger.info(
                        "SL HIT → [%s] LONG stopped at %.8f (SL=%.8f).", symbol, price, position.sl
                    )
                    self.unlock_pair(symbol, reason="SL_HIT")

            elif position.direction == "SHORT":
                if price <= position.tp:
                    logger.info(
                        "TP HIT → [%s] SHORT closed at %.8f (TP=%.8f).", symbol, price, position.tp
                    )
                    self.unlock_pair(symbol, reason="TP_HIT")
                elif price >= position.sl:
                    logger.info(
                        "SL HIT → [%s] SHORT stopped at %.8f (SL=%.8f).", symbol, price, position.sl
                    )
                    self.unlock_pair(symbol, reason="SL_HIT")
