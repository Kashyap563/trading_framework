"""Abstract base class for all trading strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from trading_framework.models import Candle, OptionData, Signal, TradeAction


class BaseStrategy(ABC):
    """Base class that every strategy must extend.

    To create a new strategy:
    1. Create a file in strategies/ (e.g., strategies/my_strategy.py)
    2. Create a class that extends BaseStrategy
    3. Implement the required methods
    4. Run: python -m trading_framework.run --strategy my_strategy --mode backtest
    """

    # --- Override these class attributes ---
    name: str = "unnamed"
    description: str = ""
    default_instrument: str = "NSE_INDEX|Nifty 50"  # What to trade
    default_lot_size: int = 25
    default_candle_interval: str = "1min"  # "1min", "5min", "30min", "day"
    requires_option_data: bool = False      # Set True for option strategies
    brokerage_per_trade: float = 500.0

    @abstractmethod
    def on_candle(self, candle: Candle) -> Optional[TradeAction]:
        """Called for each new candle. Return a TradeAction to place a trade, or None.

        This is the ONLY method you MUST implement.

        Args:
            candle: The latest OHLCV candle.

        Returns:
            A TradeAction describing the desired trade, or None to do nothing.
        """
        ...

    def on_option_data(self, options: list[OptionData]) -> Optional[TradeAction]:
        """Called with option chain data (only if requires_option_data=True).

        Override this for option strategies.

        Args:
            options: List of option contracts with greeks, OI, IV, etc.

        Returns:
            A TradeAction or None.
        """
        return None

    def on_start(self) -> None:
        """Called once when the strategy starts. Use for initialization."""
        pass

    def on_end(self) -> None:
        """Called once when the strategy ends (EOD or backtest complete)."""
        pass

    def get_position(self) -> str:
        """Return current position state. Override if tracking internally.

        Returns:
            One of "flat", "long", or "short".
        """
        return "flat"
