"""9/21 EMA Crossover Strategy for Nifty 50.

Buy when 9-EMA crosses above 21-EMA, exit when it crosses back below.
Forced exit at 3:15 PM IST.
"""

from __future__ import annotations

import math
from datetime import time
from typing import Optional
from zoneinfo import ZoneInfo

from trading_framework.base_strategy import BaseStrategy
from trading_framework.models import Candle, Signal, TradeAction

IST = ZoneInfo("Asia/Kolkata")
EOD_CUTOFF = time(15, 15)


class EMACalculator:
    """Inline EMA calculator — no external dependencies."""

    def __init__(self, period: int) -> None:
        self._period = period
        self._multiplier = 2.0 / (period + 1)
        self._value: Optional[float] = None
        self._count = 0
        self._seed_prices: list[float] = []

    @property
    def value(self) -> Optional[float]:
        return self._value

    def update(self, price: float) -> Optional[float]:
        self._count += 1
        if self._count < self._period:
            self._seed_prices.append(price)
            return None
        if self._count == self._period:
            self._seed_prices.append(price)
            self._value = sum(self._seed_prices) / self._period
            self._seed_prices = []
            return self._value
        self._value = (price - self._value) * self._multiplier + self._value
        return self._value


class EMACrossoverStrategy(BaseStrategy):
    """9/21 EMA Crossover on Nifty 50.

    Enters long when the 9-period EMA crosses above the 21-period EMA.
    Exits when the 9-period EMA crosses back below the 21-period EMA,
    or at the 3:15 PM IST end-of-day cutoff.
    """

    name = "ema_crossover"
    description = "9/21 EMA Crossover on Nifty 50"
    default_instrument = "NSE_INDEX|Nifty 50"
    default_lot_size = 25
    default_candle_interval = "5min"
    requires_option_data = False
    brokerage_per_trade = 500.0

    def on_start(self) -> None:
        self.ema_9 = EMACalculator(9)
        self.ema_21 = EMACalculator(21)
        self._prev_ema_9: Optional[float] = None
        self._prev_ema_21: Optional[float] = None
        self._position = "flat"

    def get_position(self) -> str:
        return self._position

    def on_candle(self, candle: Candle) -> Optional[TradeAction]:
        # Validate numeric fields
        for val in [candle.open, candle.high, candle.low, candle.close]:
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                return None

        close = candle.close
        ts = candle.timestamp

        ema_9_val = self.ema_9.update(close)
        ema_21_val = self.ema_21.update(close)

        if ema_9_val is None or ema_21_val is None:
            self._prev_ema_9 = ema_9_val
            self._prev_ema_21 = ema_21_val
            return None

        action: Optional[TradeAction] = None

        # EOD cutoff — force exit if in a position
        ist_time = ts.astimezone(IST).time()
        if ist_time >= EOD_CUTOFF:
            if self._position == "long":
                self._position = "flat"
                action = TradeAction(
                    signal=Signal.EXIT,
                    price=close,
                    timestamp=ts,
                    metadata={"reason": "eod"},
                )
            self._prev_ema_9 = ema_9_val
            self._prev_ema_21 = ema_21_val
            return action

        # Crossover detection
        if self._prev_ema_9 is not None and self._prev_ema_21 is not None:
            # Bullish crossover: 9-EMA crosses above 21-EMA
            if (
                self._prev_ema_9 <= self._prev_ema_21
                and ema_9_val > ema_21_val
                and self._position == "flat"
            ):
                self._position = "long"
                action = TradeAction(
                    signal=Signal.BUY,
                    price=close,
                    timestamp=ts,
                    metadata={"ema_9": ema_9_val, "ema_21": ema_21_val},
                )
            # Bearish crossover: 9-EMA crosses below 21-EMA
            elif (
                self._prev_ema_9 >= self._prev_ema_21
                and ema_9_val < ema_21_val
                and self._position == "long"
            ):
                self._position = "flat"
                action = TradeAction(
                    signal=Signal.EXIT,
                    price=close,
                    timestamp=ts,
                    metadata={"ema_9": ema_9_val, "ema_21": ema_21_val},
                )

        self._prev_ema_9 = ema_9_val
        self._prev_ema_21 = ema_21_val
        return action
