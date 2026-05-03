"""Generic trading framework — pluggable strategies with shared infrastructure.

To add a new strategy:
1. Create a file in trading_framework/strategies/ (e.g., my_strategy.py)
2. Create a class that extends BaseStrategy
3. Implement the on_candle() method
4. Run: python -m trading_framework.run --strategy my_strategy --mode backtest
"""

from trading_framework.base_strategy import BaseStrategy
from trading_framework.models import (
    BacktestResult,
    Candle,
    OptionData,
    PositionType,
    Signal,
    Trade,
    TradeAction,
)

__all__ = [
    "BaseStrategy",
    "BacktestResult",
    "Candle",
    "OptionData",
    "PositionType",
    "Signal",
    "Trade",
    "TradeAction",
]
