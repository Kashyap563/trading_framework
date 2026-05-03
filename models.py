"""Shared data models for the trading framework."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Signal(Enum):
    """Trading signal types."""

    NONE = "none"
    BUY = "buy"             # Go long
    SELL = "sell"           # Go short
    EXIT = "exit"           # Close position
    EXIT_LONG = "exit_long"
    EXIT_SHORT = "exit_short"


class PositionType(Enum):
    """Current position state."""

    FLAT = "flat"
    LONG = "long"
    SHORT = "short"


@dataclass
class Candle:
    """OHLCV candle data."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass
class OptionData:
    """Option chain data for a single contract."""

    instrument_key: str
    underlying: str              # e.g., "NIFTY 50"
    expiry: datetime
    strike_price: float
    option_type: str             # "CE" or "PE"
    ltp: float                   # Last traded price (premium)
    open_interest: int = 0
    volume: int = 0
    bid_price: float = 0.0
    ask_price: float = 0.0
    implied_volatility: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    timestamp: Optional[datetime] = None


@dataclass
class TradeAction:
    """What the strategy wants to do."""

    signal: Signal
    price: float                        # Suggested price (close of candle)
    timestamp: datetime
    instrument: str = ""                # Instrument to trade (for options: specific contract)
    quantity: int = 0                   # 0 means use default lot size
    stop_loss: float = 0.0             # Optional SL
    take_profit: float = 0.0           # Optional TP
    metadata: dict = field(default_factory=dict)  # Strategy-specific data


@dataclass
class Trade:
    """A completed trade record."""

    trade_id: int = 0
    entry_time: Optional[datetime] = None
    entry_price: float = 0.0
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    direction: str = "long"             # "long" or "short"
    instrument: str = ""
    quantity: int = 0
    pnl_points: float = 0.0
    pnl_rupees: float = 0.0
    brokerage: float = 500.0
    net_pnl: float = 0.0
    exit_reason: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class BacktestResult:
    """Aggregated performance metrics."""

    strategy_name: str = ""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0
    win_rate: float = 0.0
    total_pnl_points: float = 0.0
    total_pnl_rupees: float = 0.0
    avg_win_points: float = 0.0
    avg_loss_points: float = 0.0
    max_win_points: float = 0.0
    max_loss_points: float = 0.0
    max_drawdown_points: float = 0.0
    max_drawdown_rupees: float = 0.0
    total_brokerage: float = 0.0
    net_total_pnl_rupees: float = 0.0
    profit_factor: float = 0.0
    risk_reward_ratio: float = 0.0
    total_trading_days: int = 0
    start_date: str = ""
    end_date: str = ""
    candle_interval: str = ""           # "1min", "5min", etc.
    trades: list = field(default_factory=list)
