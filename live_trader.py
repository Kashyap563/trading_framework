"""Generic live/sandbox/paper trading loop for any BaseStrategy.

Fetches intraday candle data from Upstox API, feeds it through the strategy,
and executes trades via the configured order executor.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from trading_framework.base_strategy import BaseStrategy
from trading_framework.data_fetcher import UpstoxDataFetcher
from trading_framework.models import BacktestResult, Candle, Signal, Trade, TradeAction
from trading_framework.order_executor import OrderExecutorBase

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)

logger = logging.getLogger(__name__)


def _build_result_from_trades(
    trades: list[Trade],
    lot_size: int = 25,
) -> BacktestResult:
    """Convert Trade objects into a BacktestResult for reporting."""
    closed = [t for t in trades if t.exit_time is not None]
    if not closed:
        return BacktestResult()

    result = BacktestResult()
    result.trades = closed
    result.total_trades = len(closed)

    wins = [t for t in closed if t.pnl_points > 0]
    losses = [t for t in closed if t.pnl_points < 0]
    result.winning_trades = len(wins)
    result.losing_trades = len(losses)
    result.breakeven_trades = len(closed) - len(wins) - len(losses)

    if result.total_trades > 0:
        result.win_rate = (result.winning_trades / result.total_trades) * 100

    result.total_pnl_points = sum(t.pnl_points for t in closed)
    result.total_pnl_rupees = result.total_pnl_points * lot_size
    result.total_brokerage = sum(t.brokerage for t in closed)
    result.net_total_pnl_rupees = result.total_pnl_rupees - result.total_brokerage

    win_pts = [t.pnl_points for t in wins]
    loss_pts = [t.pnl_points for t in losses]
    if win_pts:
        result.avg_win_points = sum(win_pts) / len(win_pts)
        result.max_win_points = max(win_pts)
    if loss_pts:
        result.avg_loss_points = sum(loss_pts) / len(loss_pts)
        result.max_loss_points = min(loss_pts)

    total_win_sum = sum(win_pts) if win_pts else 0.0
    total_loss_sum = abs(sum(loss_pts)) if loss_pts else 0.0
    if total_loss_sum > 0:
        result.profit_factor = total_win_sum / total_loss_sum
    elif total_win_sum > 0:
        result.profit_factor = float("inf")

    if result.avg_loss_points != 0:
        result.risk_reward_ratio = abs(result.avg_win_points / result.avg_loss_points)

    # Max drawdown
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in closed:
        cumulative += t.pnl_points
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    result.max_drawdown_points = -max_dd
    result.max_drawdown_rupees = result.max_drawdown_points * lot_size

    # Date range
    result.start_date = closed[0].entry_time.astimezone(IST).strftime("%Y-%m-%d")
    result.end_date = closed[-1].entry_time.astimezone(IST).strftime("%Y-%m-%d")

    days = set()
    for t in closed:
        days.add(t.entry_time.astimezone(IST).date())
    result.total_trading_days = len(days)

    return result


def _generate_eod_report(
    executor: OrderExecutorBase,
    strategy_name: str,
    lot_size: int,
    output_dir: str,
    mode: str,
) -> None:
    """Generate an Excel report at end of day."""
    try:
        from trading_framework.report_generator import ReportGenerator

        result = _build_result_from_trades(executor.trades, lot_size)
        result.strategy_name = strategy_name
        if result.total_trades == 0:
            print("No trades to report.")
            return

        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        report_name = f"{strategy_name}_{mode}_report_{today_str}.xlsx"
        report_path = os.path.join(output_dir, report_name)

        generator = ReportGenerator(result)
        generator.generate(report_path)
        print(f"\n📊 Excel report saved to: {report_path}")
    except ImportError:
        print("\nNote: Install openpyxl for Excel reports: pip install openpyxl")
    except Exception as e:
        logger.error("Failed to generate report: %s", e)


class LiveTrader:
    """Runs any BaseStrategy on live/intraday data.

    Workflow:
    1. Fetch today's intraday candles from Upstox
    2. Aggregate to the strategy's preferred interval (if needed)
    3. Feed through the strategy to detect signals
    4. Execute trades via the configured OrderExecutor
    5. Repeat every poll_interval seconds until market close
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        executor: OrderExecutorBase,
        fetcher: UpstoxDataFetcher,
    ) -> None:
        self.strategy = strategy
        self.executor = executor
        self.fetcher = fetcher
        self.processed_timestamps: set[str] = set()

    def run_once(self) -> None:
        """Fetch latest candles and process any new ones."""
        now = datetime.now(IST)
        today = now.date()

        try:
            candles_1min = self.fetcher.fetch_1min_candles(today, today)
        except Exception as e:
            logger.error("Failed to fetch intraday data: %s", e)
            return

        if not candles_1min:
            logger.debug("No candles available yet")
            return

        # Aggregate based on strategy's preferred interval
        interval = self.strategy.default_candle_interval
        if interval == "5min":
            candles = self.fetcher.aggregate_to_5min(candles_1min)
        else:
            candles = candles_1min  # 1min or other

        # Process only new candles
        for candle in candles:
            ts_key = candle.timestamp.isoformat()
            if ts_key in self.processed_timestamps:
                continue
            self.processed_timestamps.add(ts_key)

            action = self.strategy.on_candle(candle)
            if action is not None:
                self.executor.execute(action)

        # Print running summary
        summary = self.executor.get_summary()
        if summary["total_trades"] > 0:
            logger.info(
                "📊 Session: %d trades, %d wins, net P&L=₹%.2f",
                summary["total_trades"],
                summary["winning"],
                summary["net_pnl_rupees"],
            )

    def run_loop(
        self,
        poll_interval: int = 60,
        mode: str = "sandbox",
        output_dir: str = "",
    ) -> None:
        """Main trading loop. Runs until market close.

        Args:
            poll_interval: Seconds between each data fetch.
            mode: Trading mode for report naming.
            output_dir: Directory to save the report in.
        """
        logger.info("🚀 Starting %s trading loop (poll every %ds)...", mode, poll_interval)
        logger.info("💰 Strategy: %s | Lot size: %d", self.strategy.name, self.strategy.default_lot_size)

        self.strategy.on_start()

        while True:
            now = datetime.now(IST)
            current_time = now.time()

            if current_time < MARKET_OPEN:
                wait_seconds = (
                    datetime.combine(now.date(), MARKET_OPEN, tzinfo=IST) - now
                ).total_seconds()
                logger.info("Market not open yet. Waiting %.0f seconds...", wait_seconds)
                time.sleep(min(wait_seconds, 60))
                continue

            if current_time > MARKET_CLOSE:
                logger.info("Market closed for today.")
                self.strategy.on_end()

                summary = self.executor.get_summary()
                print("\n" + "=" * 50)
                print("  END OF DAY SUMMARY")
                print("=" * 50)
                print(f"  Strategy: {self.strategy.name}")
                print(f"  Total Trades: {summary['total_trades']}")
                print(f"  Wins: {summary['winning']}, Losses: {summary['losing']}")
                print(f"  Win Rate: {summary['win_rate']:.1f}%")
                print(f"  Gross P&L: ₹{summary['total_pnl_rupees']:,.2f}")
                print(f"  Brokerage: ₹{summary['total_brokerage']:,.2f}")
                print(f"  Net P&L: ₹{summary['net_pnl_rupees']:,.2f}")
                print("=" * 50)

                _generate_eod_report(
                    self.executor,
                    self.strategy.name,
                    self.strategy.default_lot_size,
                    output_dir or ".",
                    mode,
                )
                break

            try:
                self.run_once()
            except KeyboardInterrupt:
                logger.info("Interrupted by user")
                self.strategy.on_end()
                break
            except Exception as e:
                logger.error("Error in trading loop: %s", e)

            time.sleep(poll_interval)
