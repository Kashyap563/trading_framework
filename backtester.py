"""Generic backtester that works with any BaseStrategy.

Feeds historical candles through a strategy, tracks trades, and computes
comprehensive performance metrics including win rate, profit factor,
max drawdown, and risk/reward ratio.
"""

from __future__ import annotations

import logging
from datetime import date
from zoneinfo import ZoneInfo

from trading_framework.base_strategy import BaseStrategy
from trading_framework.models import (
    BacktestResult,
    Candle,
    Signal,
    Trade,
    TradeAction,
)

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


class Backtester:
    """Runs any BaseStrategy over historical candles and tracks trades.

    Usage:
        strategy = EMACrossoverStrategy()
        backtester = Backtester(strategy)
        result = backtester.run(candles)
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        brokerage_per_trade: float | None = None,
    ) -> None:
        """Initialize the backtester.

        Args:
            strategy: A BaseStrategy instance to backtest.
            brokerage_per_trade: Override brokerage cost per trade.
                Defaults to strategy.brokerage_per_trade.
        """
        self.strategy = strategy
        self.brokerage = (
            brokerage_per_trade
            if brokerage_per_trade is not None
            else strategy.brokerage_per_trade
        )
        self.lot_size = strategy.default_lot_size
        self.trades: list[Trade] = []
        self._current_trade: Trade | None = None
        self._trade_counter = 0

    def run(self, candles: list[Candle]) -> BacktestResult:
        """Run the strategy over historical candles and compute results.

        For each candle, calls strategy.on_candle() and processes the
        returned TradeAction (if any). Handles BUY, SELL, EXIT, EXIT_LONG,
        and EXIT_SHORT signals.

        At the end, closes any open position at the last candle's close.

        Args:
            candles: List of Candle objects sorted chronologically.

        Returns:
            BacktestResult with all computed metrics and trade list.
        """
        if not candles:
            logger.warning("No candles provided for backtesting")
            return BacktestResult(strategy_name=self.strategy.name)

        self.trades = []
        self._current_trade = None
        self._trade_counter = 0

        self.strategy.on_start()

        prev_day: date | None = None

        for candle in candles:
            # Reset any day-level state if strategy tracks it
            current_day = candle.timestamp.astimezone(IST).date()
            if prev_day is not None and current_day != prev_day:
                pass  # Strategy handles its own day resets
            prev_day = current_day

            action = self.strategy.on_candle(candle)
            if action is not None:
                self._process_action(action, candle)

        # Close any open position at end of data
        if self._current_trade is not None:
            last = candles[-1]
            self._close_trade(last.close, last.timestamp, "end_of_data")

        self.strategy.on_end()
        return self._compute_metrics(candles)

    def _process_action(self, action: TradeAction, candle: Candle) -> None:
        """Process a TradeAction from the strategy.

        Args:
            action: The trade action to process.
            candle: The candle that triggered this action.
        """
        signal = action.signal

        if signal == Signal.BUY:
            if self._current_trade is None:
                self._open_trade(action.price, action.timestamp, "long", action)

        elif signal == Signal.SELL:
            if self._current_trade is None:
                self._open_trade(action.price, action.timestamp, "short", action)

        elif signal in (Signal.EXIT, Signal.EXIT_LONG, Signal.EXIT_SHORT):
            if self._current_trade is not None:
                reason = action.metadata.get("reason", "signal")
                self._close_trade(action.price, action.timestamp, reason)

    def _open_trade(
        self,
        price: float,
        timestamp,
        direction: str,
        action: TradeAction,
    ) -> None:
        """Open a new trade."""
        self._trade_counter += 1
        qty = action.quantity if action.quantity > 0 else self.lot_size
        self._current_trade = Trade(
            trade_id=self._trade_counter,
            entry_time=timestamp,
            entry_price=price,
            direction=direction,
            instrument=action.instrument or self.strategy.default_instrument,
            quantity=qty,
            brokerage=self.brokerage,
            metadata=dict(action.metadata),
        )
        logger.debug(
            "Trade #%d entry (%s): price=%.2f, time=%s",
            self._trade_counter, direction, price, timestamp,
        )

    def _close_trade(self, price: float, timestamp, reason: str) -> None:
        """Close the current open trade."""
        trade = self._current_trade
        if trade is None:
            return

        trade.exit_time = timestamp
        trade.exit_price = price
        trade.exit_reason = reason

        if trade.direction == "long":
            trade.pnl_points = price - trade.entry_price
        else:  # short
            trade.pnl_points = trade.entry_price - price

        trade.pnl_rupees = trade.pnl_points * trade.quantity
        trade.net_pnl = trade.pnl_rupees - trade.brokerage

        self.trades.append(trade)
        logger.debug(
            "Trade #%d exit (%s): price=%.2f, pnl=%.2f pts, reason=%s",
            trade.trade_id, reason, price, trade.pnl_points, reason,
        )
        self._current_trade = None

    def _compute_metrics(self, candles: list[Candle]) -> BacktestResult:
        """Compute all backtest metrics from the trade list.

        Args:
            candles: The original candle list (used for date range and day count).

        Returns:
            BacktestResult with all fields populated.
        """
        result = BacktestResult()
        result.strategy_name = self.strategy.name
        result.trades = list(self.trades)
        result.total_trades = len(self.trades)

        if not self.trades:
            return result

        # Date range
        first_ts = candles[0].timestamp.astimezone(IST)
        last_ts = candles[-1].timestamp.astimezone(IST)
        result.start_date = first_ts.strftime("%Y-%m-%d")
        result.end_date = last_ts.strftime("%Y-%m-%d")

        # Count unique trading days
        trading_days: set[date] = set()
        for candle in candles:
            trading_days.add(candle.timestamp.astimezone(IST).date())
        result.total_trading_days = len(trading_days)

        # Classify trades
        wins: list[float] = []
        losses: list[float] = []

        for trade in self.trades:
            if trade.pnl_points > 0:
                result.winning_trades += 1
                wins.append(trade.pnl_points)
            elif trade.pnl_points < 0:
                result.losing_trades += 1
                losses.append(trade.pnl_points)
            else:
                result.breakeven_trades += 1

        # Win rate
        if result.total_trades > 0:
            result.win_rate = (result.winning_trades / result.total_trades) * 100

        # P&L totals
        result.total_pnl_points = sum(t.pnl_points for t in self.trades)
        result.total_pnl_rupees = result.total_pnl_points * self.lot_size

        # Brokerage totals
        result.total_brokerage = len(self.trades) * self.brokerage
        result.net_total_pnl_rupees = result.total_pnl_rupees - result.total_brokerage

        # Average win/loss
        if wins:
            result.avg_win_points = sum(wins) / len(wins)
            result.max_win_points = max(wins)
        if losses:
            result.avg_loss_points = sum(losses) / len(losses)
            result.max_loss_points = min(losses)  # Most negative

        # Profit factor
        total_wins = sum(wins) if wins else 0.0
        total_losses = abs(sum(losses)) if losses else 0.0
        if total_losses > 0:
            result.profit_factor = total_wins / total_losses
        elif total_wins > 0:
            result.profit_factor = float("inf")

        # Risk/Reward ratio
        if result.avg_loss_points != 0:
            result.risk_reward_ratio = abs(
                result.avg_win_points / result.avg_loss_points
            )

        # Max drawdown
        cumulative_pnl = 0.0
        peak_pnl = 0.0
        max_dd = 0.0
        for trade in self.trades:
            cumulative_pnl += trade.pnl_points
            if cumulative_pnl > peak_pnl:
                peak_pnl = cumulative_pnl
            drawdown = peak_pnl - cumulative_pnl
            if drawdown > max_dd:
                max_dd = drawdown

        result.max_drawdown_points = -max_dd  # Negative to indicate loss
        result.max_drawdown_rupees = result.max_drawdown_points * self.lot_size

        return result

    def print_report(self, result: BacktestResult) -> None:
        """Print a formatted backtest report to the console.

        Args:
            result: BacktestResult with computed metrics.
        """
        pnl_sign = "+" if result.total_pnl_points >= 0 else ""
        dd_str = f"{result.max_drawdown_points:.1f}"

        print()
        print("=" * 55)
        print(f"   {result.strategy_name.upper()} BACKTEST REPORT")
        print("=" * 55)
        print(f"  Period: {result.start_date} to {result.end_date}")
        print(f"  Total Trading Days: {result.total_trading_days}")
        print("-" * 55)

        print("  TRADE SUMMARY")
        print(f"  Total Trades:    {result.total_trades}")
        if result.total_trades > 0:
            win_pct = f"({result.win_rate:.1f}%)"
            loss_pct = f"({100 - result.win_rate:.1f}%)"
            print(f"  Winning Trades:  {result.winning_trades} {win_pct}")
            print(f"  Losing Trades:   {result.losing_trades} {loss_pct}")
            if result.breakeven_trades > 0:
                print(f"  Breakeven:       {result.breakeven_trades}")
        print("-" * 55)

        print("  PROFIT & LOSS")
        print(
            f"  Total P&L:       {pnl_sign}{result.total_pnl_points:.2f} points "
            f"(₹{result.total_pnl_rupees:,.2f})"
        )
        if result.winning_trades > 0:
            print(f"  Avg Win:         +{result.avg_win_points:.2f} points")
            print(f"  Max Win:         +{result.max_win_points:.2f} points")
        if result.losing_trades > 0:
            print(f"  Avg Loss:        {result.avg_loss_points:.2f} points")
            print(f"  Max Loss:        {result.max_loss_points:.2f} points")
        print("-" * 55)

        print("  BROKERAGE & NET P&L")
        print(
            f"  Brokerage:       ₹{result.total_brokerage:,.2f} "
            f"({result.total_trades} trades x ₹{self.brokerage:.0f})"
        )
        net_sign = "+" if result.net_total_pnl_rupees >= 0 else ""
        print(
            f"  Net P&L:         {net_sign}₹{result.net_total_pnl_rupees:,.2f} (after brokerage)"
        )
        print("-" * 55)

        print("  RISK METRICS")
        print(f"  Win Rate:        {result.win_rate:.1f}%")
        pf_str = (
            f"{result.profit_factor:.2f}"
            if result.profit_factor != float("inf")
            else "∞"
        )
        print(f"  Profit Factor:   {pf_str}")
        print(f"  Risk/Reward:     1:{result.risk_reward_ratio:.2f}")
        print(f"  Max Drawdown:    {dd_str} points (₹{result.max_drawdown_rupees:,.2f})")
        print("-" * 55)

        # Trade log (first 10 trades)
        if result.trades:
            print("  TRADE LOG (first 10 trades)")
            print(
                f"  {'#':<4} {'Entry Time':<20} {'Entry':>10} "
                f"{'Exit':>10} {'P&L Pts':>10} {'Reason':<10}"
            )
            print("  " + "-" * 68)
            for i, trade in enumerate(result.trades[:10], 1):
                entry_ts = trade.entry_time.astimezone(IST).strftime("%Y-%m-%d %H:%M")
                exit_ts = (
                    trade.exit_time.astimezone(IST).strftime("%Y-%m-%d %H:%M")
                    if trade.exit_time
                    else "OPEN"
                )
                pnl_str = f"{trade.pnl_points:+.2f}"
                print(
                    f"  {i:<4} {entry_ts:<20} {trade.entry_price:>10.2f} "
                    f"{trade.exit_price:>10.2f} {pnl_str:>10} {trade.exit_reason:<10}"
                )
            if len(result.trades) > 10:
                print(f"  ... and {len(result.trades) - 10} more trades")

        print("=" * 55)

        # Exit reason breakdown
        signal_exits = sum(1 for t in result.trades if t.exit_reason == "signal")
        eod_exits = sum(1 for t in result.trades if t.exit_reason == "eod")
        data_exits = sum(1 for t in result.trades if t.exit_reason == "end_of_data")
        print(f"  Exit Reasons: signal={signal_exits}, eod={eod_exits}", end="")
        if data_exits:
            print(f", end_of_data={data_exits}", end="")
        print()
        print("=" * 55)
        print()
