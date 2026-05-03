"""Single CLI entry point for the trading framework.

Usage:
    # Backtest (default: strategy's preferred interval, 24 months):
    python -m trading_framework.run --strategy ema_crossover --mode backtest

    # Backtest with custom interval:
    python -m trading_framework.run --strategy ema_crossover --mode backtest --interval 5min --months 6

    # Sandbox trading:
    python -m trading_framework.run --strategy ema_crossover --mode sandbox

    # Live trading:
    python -m trading_framework.run --strategy ema_crossover --mode live

    # Use cached CSV:
    python -m trading_framework.run --strategy ema_crossover --mode backtest --csv data.csv

    # List available strategies:
    python -m trading_framework.run --list
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import logging
import os
import pkgutil
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from trading_framework.base_strategy import BaseStrategy

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Strategy discovery
# ------------------------------------------------------------------

def discover_strategies() -> dict[str, type[BaseStrategy]]:
    """Find all strategy classes in the strategies/ directory.

    Scans trading_framework/strategies/ for .py files, imports each module,
    and finds classes that extend BaseStrategy.

    Returns:
        Dict mapping strategy name → strategy class.
    """
    strategies: dict[str, type[BaseStrategy]] = {}

    strategies_dir = os.path.join(os.path.dirname(__file__), "strategies")
    if not os.path.isdir(strategies_dir):
        return strategies

    for finder, module_name, is_pkg in pkgutil.iter_modules([strategies_dir]):
        if module_name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"trading_framework.strategies.{module_name}")
        except Exception as e:
            logger.warning("Failed to import strategy module %s: %s", module_name, e)
            continue

        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                inspect.isclass(attr)
                and issubclass(attr, BaseStrategy)
                and attr is not BaseStrategy
            ):
                strategies[attr.name] = attr

    return strategies


def load_env_file(filepath: str) -> dict[str, str]:
    """Load key=value pairs from a .env file."""
    env: dict[str, str] = {}
    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    env[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return env


# ------------------------------------------------------------------
# Mode runners
# ------------------------------------------------------------------

def run_backtest(
    strategy_cls: type[BaseStrategy],
    interval: str | None,
    months: int,
    csv_path: str | None,
    save_csv: str | None,
    report_path: str | None,
    env: dict[str, str],
    verbose: bool,
) -> None:
    """Run a backtest for the given strategy."""
    from trading_framework.backtester import Backtester
    from trading_framework.data_fetcher import UpstoxDataFetcher, load_from_csv, save_to_csv
    from trading_framework.models import Candle

    strategy = strategy_cls()
    candle_interval = interval or strategy.default_candle_interval

    print(f"📊 Backtesting: {strategy.name} ({strategy.description})")
    print(f"   Interval: {candle_interval} | Months: {months}")
    print(f"   Instrument: {strategy.default_instrument}")
    print(f"   Lot size: {strategy.default_lot_size} | Brokerage: ₹{strategy.brokerage_per_trade:.0f}")
    print()

    # Load candles
    if csv_path:
        print(f"Loading candles from CSV: {csv_path}")
        candles = load_from_csv(csv_path)
        # If CSV has 1min data and strategy wants 5min, aggregate
        if candle_interval == "5min" and candles:
            # Check if data looks like 1min (consecutive candles ~1 min apart)
            if len(candles) > 1:
                diff = (candles[1].timestamp - candles[0].timestamp).total_seconds()
                if diff <= 120:  # Likely 1-min data
                    token = env.get("UPSTOX_ACCESS_TOKEN", "dummy")
                    fetcher = UpstoxDataFetcher(token)
                    candles = fetcher.aggregate_to_5min(candles)
    else:
        token = env.get("UPSTOX_ACCESS_TOKEN") or env.get("UPSTOX_SANDBOX_TOKEN")
        if not token:
            print("Error: No access token found. Set UPSTOX_ACCESS_TOKEN in .env or use --csv", file=sys.stderr)
            sys.exit(1)

        fetcher = UpstoxDataFetcher(token, strategy.default_instrument)
        candles = fetcher.fetch_candles(candle_interval, months)

    if not candles:
        print("No candle data available. Exiting.")
        sys.exit(1)

    print(f"Loaded {len(candles)} candles")

    # Save CSV if requested
    if save_csv:
        save_to_csv(candles, save_csv)
        print(f"Saved candles to {save_csv}")

    # Run backtest
    backtester = Backtester(strategy)
    result = backtester.run(candles)
    result.candle_interval = candle_interval

    # Print report
    backtester.print_report(result)

    # Generate Excel report if requested
    if report_path:
        try:
            from trading_framework.report_generator import ReportGenerator
            generator = ReportGenerator(result)
            generator.generate(report_path)
        except ImportError:
            print("Note: Install openpyxl for Excel reports: pip install openpyxl")


def run_live(
    strategy_cls: type[BaseStrategy],
    mode: str,
    env: dict[str, str],
    poll_interval: int,
    verbose: bool,
) -> None:
    """Run live/sandbox/paper trading for the given strategy."""
    from trading_framework.data_fetcher import UpstoxDataFetcher
    from trading_framework.live_trader import LiveTrader
    from trading_framework.order_executor import (
        LiveOrderExecutor,
        PaperOrderExecutor,
        SandboxOrderExecutor,
    )

    strategy = strategy_cls()
    framework_dir = os.path.dirname(os.path.abspath(__file__))

    # SAFETY: Live mode requires explicit confirmation
    if mode == "live":
        confirm = env.get("CONFIRM_LIVE", "").lower()
        if confirm != "yes":
            print(
                "❌ SAFETY LOCK: Live trading is blocked.\n"
                "To enable, set BOTH of these in your .env:\n"
                "  TRADING_MODE=live\n"
                "  CONFIRM_LIVE=yes\n"
                "\n⚠️  This will place REAL orders with REAL money.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Get access token
    if mode == "sandbox":
        token = env.get("UPSTOX_SANDBOX_TOKEN") or env.get("UPSTOX_ACCESS_TOKEN")
    else:
        token = env.get("UPSTOX_ACCESS_TOKEN")

    if not token and mode != "paper":
        print(f"Error: No access token found for {mode} mode. Set it in .env", file=sys.stderr)
        sys.exit(1)

    instrument_token = env.get("NIFTY_INSTRUMENT_TOKEN", "NSE_FO|NIFTY")

    # Create executor
    if mode == "paper":
        executor = PaperOrderExecutor(
            lot_size=strategy.default_lot_size,
            brokerage=strategy.brokerage_per_trade,
            trade_log_path=os.path.join(framework_dir, f"{strategy.name}_paper_trades.json"),
        )
        print(f"📝 PAPER TRADING MODE — {strategy.name}")
    elif mode == "sandbox":
        executor = SandboxOrderExecutor(
            access_token=token,
            instrument_token=instrument_token,
            lot_size=strategy.default_lot_size,
            brokerage=strategy.brokerage_per_trade,
        )
        print(f"🔵 SANDBOX MODE — {strategy.name}")
    elif mode == "live":
        executor = LiveOrderExecutor(
            access_token=token,
            instrument_token=instrument_token,
            confirm_live=True,
            lot_size=strategy.default_lot_size,
            brokerage=strategy.brokerage_per_trade,
            trade_log_path=os.path.join(framework_dir, f"{strategy.name}_live_trades.json"),
        )
        print(f"🔴 LIVE MODE — {strategy.name} — REAL MONEY!")
    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        sys.exit(1)

    print(f"💰 Brokerage per trade: ₹{strategy.brokerage_per_trade:.0f}")
    print()

    if not token and mode == "paper":
        print("Note: No access token — paper mode cannot fetch live data.")
        sys.exit(0)

    fetcher = UpstoxDataFetcher(token, strategy.default_instrument)
    trader = LiveTrader(strategy, executor, fetcher)

    try:
        trader.run_loop(
            poll_interval=poll_interval,
            mode=mode,
            output_dir=framework_dir,
        )
    except KeyboardInterrupt:
        print("\nTrading stopped by user.")
        summary = executor.get_summary()
        if summary["total_trades"] > 0:
            print(f"Final: {summary['total_trades']} trades, net P&L=₹{summary['net_pnl_rupees']:,.2f}")


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trading Framework — pluggable strategies with shared infrastructure",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m trading_framework.run --strategy ema_crossover --mode backtest\n"
            "  python -m trading_framework.run --strategy ema_crossover --mode backtest --interval 5min --months 6\n"
            "  python -m trading_framework.run --strategy ema_crossover --mode sandbox\n"
            "  python -m trading_framework.run --strategy ema_crossover --mode live\n"
            "  python -m trading_framework.run --strategy ema_crossover --mode backtest --csv data.csv\n"
            "  python -m trading_framework.run --list\n"
        ),
    )
    parser.add_argument(
        "--strategy",
        help="Strategy name (filename without .py in strategies/)",
    )
    parser.add_argument(
        "--mode",
        choices=["backtest", "sandbox", "paper", "live"],
        default="backtest",
        help="Execution mode (default: backtest)",
    )
    parser.add_argument(
        "--interval",
        default=None,
        help="Candle interval (1min, 5min, 30min, day). Defaults to strategy's default.",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=24,
        help="Months of data for backtest (default: 24)",
    )
    parser.add_argument(
        "--csv",
        help="Use cached CSV data for backtest",
    )
    parser.add_argument(
        "--save-csv",
        help="Save fetched data to CSV",
    )
    parser.add_argument(
        "--report",
        help="Generate Excel report at this path",
    )
    parser.add_argument(
        "--poll",
        type=int,
        default=60,
        help="Poll interval in seconds for live/sandbox mode (default: 60)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available strategies",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # Discover strategies
    strategies = discover_strategies()

    # --list mode
    if args.list:
        print("\n📋 Available Strategies:")
        print("-" * 60)
        if not strategies:
            print("  No strategies found in trading_framework/strategies/")
        else:
            for name, cls in sorted(strategies.items()):
                print(f"  {name:<25} {cls.description}")
                print(f"  {'':25} interval={cls.default_candle_interval}, "
                      f"lot={cls.default_lot_size}, "
                      f"instrument={cls.default_instrument}")
                print()
        print("-" * 60)
        return

    # Validate strategy selection
    if not args.strategy:
        print("Error: --strategy is required (or use --list to see available strategies)", file=sys.stderr)
        sys.exit(1)

    if args.strategy not in strategies:
        print(f"Error: Strategy '{args.strategy}' not found.", file=sys.stderr)
        print(f"Available: {', '.join(sorted(strategies.keys()))}", file=sys.stderr)
        sys.exit(1)

    strategy_cls = strategies[args.strategy]

    # Load .env
    framework_dir = os.path.dirname(os.path.abspath(__file__))
    env = load_env_file(os.path.join(framework_dir, ".env"))

    # Dispatch to mode
    if args.mode == "backtest":
        run_backtest(
            strategy_cls=strategy_cls,
            interval=args.interval,
            months=args.months,
            csv_path=args.csv,
            save_csv=args.save_csv,
            report_path=args.report,
            env=env,
            verbose=args.verbose,
        )
    else:
        run_live(
            strategy_cls=strategy_cls,
            mode=args.mode,
            env=env,
            poll_interval=args.poll,
            verbose=args.verbose,
        )


if __name__ == "__main__":
    main()
