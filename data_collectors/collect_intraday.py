#!/usr/bin/env python3
"""Collect Nifty 50 Index 1-minute OHLCV candles from the Upstox V3 API.

Fetches data from January 2022 to today, month by month, and writes to a
single CSV in append mode.  Supports resume — if the script is stopped it
picks up where it left off.

Output
------
trading_framework/data/nifty50_intraday_1min.csv

Columns
-------
timestamp, open, high, low, close, volume, oi,
spread_estimate, transaction_cost_estimate, is_trading_hour

Usage
-----
    python -m trading_framework.data_collectors.collect_intraday
    python -m trading_framework.data_collectors.collect_intraday --from-date 2023-01-01
    python -m trading_framework.data_collectors.collect_intraday --to-date 2024-06-30
    nohup python -m trading_framework.data_collectors.collect_intraday > intraday.log 2>&1 &
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime

from trading_framework.data_collectors._common import (
    DATA_DIR,
    DEFAULT_TRANSACTION_COST,
    NIFTY_INDEX_KEY,
    ProgressTracker,
    UpstoxClient,
    append_rows_to_csv,
    chunk_key,
    ensure_csv_header,
    fmt_duration,
    get_access_token,
    get_month_ranges,
    is_trading_hour,
    setup_logging,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CSV_PATH = os.path.join(DATA_DIR, "nifty50_intraday_1min.csv")
CSV_FIELDS = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "oi",
    "spread_estimate",
    "transaction_cost_estimate",
    "is_trading_hour",
]

DEFAULT_START = date(2022, 1, 1)

logger = setup_logging("collect_intraday")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def fetch_month(
    client: UpstoxClient,
    from_date: date,
    to_date: date,
) -> list[list]:
    """Fetch 1-min candles for one month chunk and return CSV-ready rows."""
    encoded_key = UpstoxClient.encode_key(NIFTY_INDEX_KEY)
    url = (
        f"{UpstoxClient.BASE_V3}/historical-candle/"
        f"{encoded_key}/minutes/1/"
        f"{to_date.isoformat()}/{from_date.isoformat()}"
    )

    data = client.get(url)
    if data.get("status") != "success":
        logger.warning(
            "Non-success response for %s → %s: %s",
            from_date, to_date, data.get("message", data.get("status")),
        )
        return []

    candles = data.get("data", {}).get("candles", [])
    if not candles:
        logger.info("No candles for %s → %s", from_date, to_date)
        return []

    rows: list[list] = []
    for c in candles:
        try:
            ts_str = c[0]
            o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
            vol = int(c[5]) if len(c) > 5 else 0
            oi = int(c[6]) if len(c) > 6 else 0

            spread_est = round((h - l) * 0.01, 4)
            tx_cost = DEFAULT_TRANSACTION_COST

            ts_dt = datetime.fromisoformat(ts_str)
            trading = is_trading_hour(ts_dt)

            rows.append([
                ts_str, o, h, l, cl, vol, oi,
                spread_est, tx_cost, trading,
            ])
        except (IndexError, ValueError, TypeError) as exc:
            logger.debug("Skipping malformed candle %s: %s", c, exc)

    # API returns newest-first; sort chronologically for the CSV
    rows.sort(key=lambda r: r[0])
    return rows


def run(from_date: date, to_date: date, transaction_cost: float) -> None:
    """Main collection loop."""
    global DEFAULT_TRANSACTION_COST  # noqa: PLW0603 — allow override via CLI
    # (we just use the module-level constant in fetch_month, but let the
    #  argparse value flow through)

    token = get_access_token()
    client = UpstoxClient(token)
    tracker = ProgressTracker("intraday")

    ranges = get_month_ranges(from_date, to_date)
    total_chunks = len(ranges)
    total_candles = 0
    start_time = time.time()

    # Estimate: ~375 trading candles/day × ~22 days/month ≈ 8 250 per chunk
    est_calls = sum(1 for f, t in ranges if not tracker.is_done(chunk_key(f, t)))
    est_seconds = est_calls * 0.5  # ~0.5 s per call including overhead
    logger.info(
        "=== Nifty 50 Intraday 1-min Collector ===\n"
        "  Range       : %s → %s\n"
        "  Chunks      : %d (month-sized)\n"
        "  Remaining   : %d API calls\n"
        "  Est. time   : %s\n",
        from_date, to_date, total_chunks, est_calls, fmt_duration(est_seconds),
    )

    ensure_csv_header(CSV_PATH, CSV_FIELDS)

    for idx, (chunk_from, chunk_to) in enumerate(ranges, 1):
        key = chunk_key(chunk_from, chunk_to)
        if tracker.is_done(key):
            logger.info(
                "[%d/%d] Skipping %s → %s (already done)",
                idx, total_chunks, chunk_from, chunk_to,
            )
            continue

        rows = fetch_month(client, chunk_from, chunk_to)
        if rows:
            append_rows_to_csv(CSV_PATH, rows)
            total_candles += len(rows)

        tracker.mark_done(key)

        elapsed = time.time() - start_time
        remaining_chunks = total_chunks - idx
        avg_per_chunk = elapsed / idx if idx else 1
        eta = remaining_chunks * avg_per_chunk

        logger.info(
            "[%d/%d] Fetched %s → %s  (%d candles, total: %d, ETA: %s)",
            idx, total_chunks, chunk_from, chunk_to,
            len(rows), total_candles, fmt_duration(eta),
        )

    elapsed = time.time() - start_time
    logger.info(
        "=== Done ===  %d candles written to %s in %s",
        total_candles, CSV_PATH, fmt_duration(elapsed),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect Nifty 50 index 1-min OHLCV from Upstox V3 API.",
    )
    parser.add_argument(
        "--from-date",
        type=lambda s: date.fromisoformat(s),
        default=DEFAULT_START,
        help="Start date in YYYY-MM-DD (default: 2022-01-01).",
    )
    parser.add_argument(
        "--to-date",
        type=lambda s: date.fromisoformat(s),
        default=date.today(),
        help="End date in YYYY-MM-DD (default: today).",
    )
    parser.add_argument(
        "--transaction-cost",
        type=float,
        default=DEFAULT_TRANSACTION_COST,
        help="Fixed round-trip transaction cost in ₹ (default: 500).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear progress tracker and re-download everything.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.reset:
        ProgressTracker("intraday").reset()
        logger.info("Progress tracker reset.")
    run(args.from_date, args.to_date, args.transaction_cost)


if __name__ == "__main__":
    main()
