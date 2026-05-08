#!/usr/bin/env python3
"""Run the ATLAS Equity Strategy.

Usage:
    # Single daily run:
    python run_atlas.py

    # Daemon mode (runs daily at 9:20 AM IST):
    python run_atlas.py --daemon

    # Paper mode (no real orders):
    python run_atlas.py --paper
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from strategies.atlas_equity.config import AtlasConfig
from strategies.atlas_equity.runner import AtlasRunner

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


def is_trading_day() -> bool:
    """Check if today is a trading day (Mon-Fri, not a holiday)."""
    now = datetime.now(IST)
    return now.weekday() < 5  # Mon=0, Fri=4


def seconds_until_next_run(target_hour: int = 9, target_minute: int = 20) -> float:
    """Calculate seconds until next run time."""
    now = datetime.now(IST)
    target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)

    if now >= target:
        # Already past today's run time, schedule for tomorrow
        target += timedelta(days=1)

    return (target - now).total_seconds()


def main():
    parser = argparse.ArgumentParser(description="ATLAS Equity Strategy Runner")
    parser.add_argument("--daemon", action="store_true", help="Run as daemon (daily at 9:20 IST)")
    parser.add_argument("--paper", action="store_true", help="Paper trading mode")
    parser.add_argument("--universe", default="NIFTY500", choices=["NIFTY50", "NIFTY200", "NIFTY500"])
    parser.add_argument("--capital", type=float, default=500_000.0, help="Total capital")
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("atlas_equity.log", mode="a"),
        ],
    )

    # Validate API key
    if not os.environ.get("GEMINI_API_KEY"):
        logger.error("GEMINI_API_KEY not set in .env")
        sys.exit(1)

    if not os.environ.get("UPSTOX_ACCESS_TOKEN"):
        logger.warning("UPSTOX_ACCESS_TOKEN not set — market data will be limited")

    config = AtlasConfig(
        universe=args.universe,
        total_capital=args.capital,
    )

    logger.info("ATLAS Equity Strategy")
    logger.info("  Universe: %s | Capital: ₹%.0f | Mode: %s",
                config.universe, config.total_capital,
                "PAPER" if args.paper else "LIVE")
    logger.info("  LLM: %s (free tier)", config.llm_model)
    logger.info("")

    runner = AtlasRunner(config)

    if args.daemon:
        logger.info("Running in DAEMON mode — will execute daily at 9:20 AM IST")
        while True:
            if is_trading_day():
                now = datetime.now(IST)
                if now.hour == 9 and 15 <= now.minute <= 25:
                    logger.info("Market open — running daily strategy...")
                    try:
                        runner.run_daily()
                    except Exception as e:
                        logger.error("Daily run failed: %s", e, exc_info=True)

                    # Sleep until next day
                    sleep_time = seconds_until_next_run()
                    logger.info("Next run in %.1f hours", sleep_time / 3600)
                    time.sleep(min(sleep_time, 3600))  # wake up hourly to check
                else:
                    # Not run time yet, sleep 5 min
                    time.sleep(300)
            else:
                # Weekend — sleep until Monday
                sleep_time = seconds_until_next_run()
                logger.info("Weekend — sleeping %.1f hours until next trading day", sleep_time / 3600)
                time.sleep(min(sleep_time, 3600))
    else:
        # Single run
        runner.run_daily()


if __name__ == "__main__":
    main()
