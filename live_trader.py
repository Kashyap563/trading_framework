"""Generic live/sandbox/paper trading loop for any BaseStrategy.

Fetches intraday candle data from Upstox API, feeds it through the strategy,
and executes trades via the configured order executor.
"""

from __future__ import annotations

import logging
import os
import signal
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

        # Wire up live options provider for strategies that need option data
        if getattr(strategy, "requires_option_data", False):
            try:
                from trading_framework.strategies.iron_condor import LiveOptionsProvider
                live_provider = LiveOptionsProvider(fetcher)
                if hasattr(strategy, "set_live_provider"):
                    strategy.set_live_provider(live_provider)
                    logger.info("Live options provider connected to %s", strategy.name)
            except ImportError:
                logger.warning("LiveOptionsProvider not available for %s", strategy.name)

    def run_once(self) -> None:
        """Fetch latest candles and process any new ones."""
        now = datetime.now(IST)
        today = now.date()

        try:
            candles_1min = self.fetcher.fetch_1min_candles(today, today)
        except Exception as e:
            error_str = str(e)
            if "401" in error_str or "Unauthorized" in error_str:
                logger.warning("🔑 Token expired mid-session. Attempting refresh...")
                if self._refresh_token_from_env():
                    logger.info("Token refreshed, retrying...")
                    return
                else:
                    logger.error("Token refresh failed. Update .env and restart.")
                    return
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
        daemon: bool = False,
    ) -> None:
        """Main trading loop. Runs until market close.

        Args:
            poll_interval: Seconds between each data fetch.
            mode: Trading mode for report naming.
            output_dir: Directory to save the report in.
            daemon: If True, sleep after market close and resume next trading day.
        """
        logger.info("🚀 Starting %s trading loop (poll every %ds)...", mode, poll_interval)
        logger.info("💰 Strategy: %s | Lot size: %d", self.strategy.name, self.strategy.default_lot_size)
        if daemon:
            logger.info("🔄 DAEMON MODE: will sleep between days and resume automatically")

        # Graceful shutdown handler
        def _shutdown_handler(signum, frame):
            logger.info("🛑 Shutdown signal received — saving state...")
            self.strategy.on_end()
            raise SystemExit(0)

        signal.signal(signal.SIGINT, _shutdown_handler)
        signal.signal(signal.SIGTERM, _shutdown_handler)

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

                if daemon:
                    # Check if today was expiry day — if so, terminate the cycle
                    # User must manually restart for the next expiry cycle
                    strategy_pos = self.strategy.get_position()
                    if strategy_pos == "flat":
                        logger.info(
                            "🔄 DAEMON: Expiry cycle complete. "
                            "Restart manually for next cycle: "
                            "python -m trading_framework.run --strategy %s --mode %s --daemon",
                            self.strategy.name, mode,
                        )
                        break

                    # Position still open (swing trade) — sleep until next trading day
                    next_open = self._next_trading_day_open(now)
                    sleep_seconds = (next_open - now).total_seconds()
                    logger.info(
                        "🔄 DAEMON: Position open, sleeping until %s (%.1f hours)",
                        next_open.strftime("%Y-%m-%d %H:%M IST"),
                        sleep_seconds / 3600,
                    )
                    time.sleep(sleep_seconds)

                    # Refresh token from .env (you update it each morning)
                    self._refresh_token_from_env()
                    if not self._wait_for_valid_token(max_wait_minutes=30):
                        logger.error("Cannot continue without valid token. Saving state and exiting.")
                        self.strategy.on_end()
                        break

                    self.processed_timestamps.clear()
                    self.strategy.on_start()
                    continue
                else:
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

    @staticmethod
    def _next_trading_day_open(now: datetime) -> datetime:
        """Calculate the next trading day's market open (9:14 AM IST).

        Skips weekends (Saturday/Sunday).
        """
        next_day = now + timedelta(days=1)
        # Skip weekends
        while next_day.weekday() in (5, 6):  # Saturday=5, Sunday=6
            next_day += timedelta(days=1)
        return datetime.combine(
            next_day.date(),
            dt_time(9, 14),
            tzinfo=IST,
        )

    def _refresh_token_from_env(self) -> bool:
        """Re-read .env file and update the fetcher's access token.

        Called each morning in daemon mode to pick up a fresh token.
        Returns True if token was updated, False if unchanged or missing.
        """
        framework_dir = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.join(framework_dir, ".env")

        if not os.path.exists(env_path):
            logger.warning("No .env file found at %s", env_path)
            return False

        env: dict[str, str] = {}
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, _, value = line.partition("=")
                        env[key.strip()] = value.strip()
        except Exception as exc:
            logger.error("Failed to read .env: %s", exc)
            return False

        new_token = env.get("UPSTOX_ACCESS_TOKEN", "")
        if not new_token or new_token == "your_access_token_here":
            new_token = env.get("UPSTOX_SANDBOX_TOKEN", "")

        if not new_token:
            logger.warning("No access token found in .env")
            return False

        old_token = self.fetcher.access_token
        if new_token != old_token:
            self.fetcher.access_token = new_token
            self.fetcher.session.headers["Authorization"] = f"Bearer {new_token}"
            logger.info("🔑 Token refreshed from .env")
            return True

        return False

    def _wait_for_valid_token(self, max_wait_minutes: int = 30) -> bool:
        """Wait for a valid token by polling .env every 30 seconds.

        Called when a 401 is detected. Gives you time to update .env
        with a fresh token before market opens.

        Returns True if a valid token was found, False if timed out.
        """
        logger.warning(
            "⚠️  Token expired or invalid. Update UPSTOX_ACCESS_TOKEN in .env. "
            "Waiting up to %d minutes...", max_wait_minutes,
        )
        start = time.time()
        while (time.time() - start) < max_wait_minutes * 60:
            if self._refresh_token_from_env():
                # Test the new token with a simple API call
                try:
                    today = datetime.now(IST).date()
                    candles = self.fetcher.fetch_1min_candles(today, today)
                    if candles is not None:  # Even empty list means token works
                        logger.info("✅ Token validated successfully")
                        return True
                except Exception:
                    pass
            time.sleep(30)

        logger.error("❌ Token not updated within %d minutes. Stopping.", max_wait_minutes)
        return False
