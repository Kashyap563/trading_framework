#!/usr/bin/env python3
"""Collect Nifty 50 Futures 1-minute OHLCV candles from the Upstox V2
expired-instruments API.

Process
-------
1. Fetch all expiry dates for Nifty 50.
2. For each expiry, fetch the futures contract instrument key.
3. For each contract, fetch 1-min candles month-by-month.
4. Write rows to a single CSV in append mode with contract metadata.
5. Also produce a rollover metadata CSV.

Output
------
trading_framework/data/nifty50_futures_1min.csv
trading_framework/data/nifty50_futures_rollover.csv

Usage
-----
    python -m trading_framework.data_collectors.collect_futures
    python -m trading_framework.data_collectors.collect_futures --from-date 2023-01-01
    nohup python -m trading_framework.data_collectors.collect_futures > futures.log 2>&1 &
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta

from trading_framework.data_collectors._common import (
    DATA_DIR,
    DEFAULT_FUTURES_MARGIN,
    NIFTY_INDEX_KEY,
    NIFTY_LOT_SIZE,
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

CSV_PATH = os.path.join(DATA_DIR, "nifty50_futures_1min.csv")
CSV_FIELDS = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "oi",
    "contract_expiry",
    "instrument_key",
    "lot_size",
    "days_to_expiry",
    "is_near_month",
    "spread_estimate",
    "margin_estimate",
]

ROLLOVER_CSV = os.path.join(DATA_DIR, "nifty50_futures_rollover.csv")
ROLLOVER_FIELDS = [
    "expiry_date",
    "instrument_key",
    "first_trade_date",
    "last_trade_date",
    "total_candles",
]

DEFAULT_START = date(2022, 1, 1)

logger = setup_logging("collect_futures")


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def fetch_expiries(client: UpstoxClient) -> list[date]:
    """Get all available expiry dates for Nifty 50 from the V2 API."""
    encoded = UpstoxClient.encode_key(NIFTY_INDEX_KEY)
    url = f"{UpstoxClient.BASE_V2}/expired-instruments/expiries?instrument_key={encoded}"
    data = client.get(url)
    if data.get("status") != "success":
        logger.error("Failed to fetch expiries: %s", data)
        return []

    raw = data.get("data", [])
    expiries: list[date] = []
    for item in raw:
        try:
            # The API may return date strings or ISO timestamps
            d = item if isinstance(item, str) else str(item)
            expiries.append(date.fromisoformat(d[:10]))
        except (ValueError, TypeError) as exc:
            logger.debug("Skipping unparseable expiry %r: %s", item, exc)
    expiries.sort()
    return expiries


def fetch_futures_contract(
    client: UpstoxClient,
    expiry_date: date,
) -> str | None:
    """Get the futures contract instrument key for a given expiry."""
    encoded = UpstoxClient.encode_key(NIFTY_INDEX_KEY)
    url = (
        f"{UpstoxClient.BASE_V2}/expired-instruments/future/contract"
        f"?instrument_key={encoded}&expiry_date={expiry_date.isoformat()}"
    )
    data = client.get(url)
    if data.get("status") != "success":
        logger.warning("No futures contract for expiry %s: %s", expiry_date, data)
        return None

    contracts = data.get("data", [])
    if not contracts:
        return None

    # Pick the first (usually only) contract
    contract = contracts[0] if isinstance(contracts, list) else contracts
    if isinstance(contract, dict):
        return contract.get("instrument_key")
    return str(contract)


def fetch_candles_for_contract(
    client: UpstoxClient,
    instrument_key: str,
    from_date: date,
    to_date: date,
) -> list[list]:
    """Fetch 1-min candles for an expired futures contract via V2 API.

    Returns raw candle arrays: [timestamp, o, h, l, c, vol, oi].
    """
    encoded = UpstoxClient.encode_key(instrument_key)
    url = (
        f"{UpstoxClient.BASE_V2}/expired-instruments/historical-candle/"
        f"{encoded}/1minute/{to_date.isoformat()}/{from_date.isoformat()}"
    )
    data = client.get(url)
    if data.get("status") != "success":
        return []
    return data.get("data", {}).get("candles", [])


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def process_contract(
    client: UpstoxClient,
    instrument_key: str,
    expiry_date: date,
    is_near: bool,
    tracker: ProgressTracker,
    start_date: date,
    margin_estimate: float,
) -> tuple[int, str | None, str | None]:
    """Fetch all 1-min candles for one futures contract and append to CSV.

    Returns (total_candles, first_trade_date_str, last_trade_date_str).
    """
    # The contract is typically active ~2 months before expiry
    contract_start = max(expiry_date - timedelta(days=60), start_date)
    contract_end = expiry_date

    ranges = get_month_ranges(contract_start, contract_end)
    total_candles = 0
    first_ts: str | None = None
    last_ts: str | None = None

    for chunk_from, chunk_to in ranges:
        key = f"fut_{instrument_key}_{chunk_key(chunk_from, chunk_to)}"
        if tracker.is_done(key):
            continue

        raw_candles = fetch_candles_for_contract(
            client, instrument_key, chunk_from, chunk_to,
        )
        if not raw_candles:
            tracker.mark_done(key)
            continue

        rows: list[list] = []
        for c in raw_candles:
            try:
                ts_str = c[0]
                o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
                vol = int(c[5]) if len(c) > 5 else 0
                oi = int(c[6]) if len(c) > 6 else 0

                ts_dt = datetime.fromisoformat(ts_str)
                candle_date = ts_dt.date()
                dte = (expiry_date - candle_date).days
                spread_est = round((h - l) * 0.01, 4)

                rows.append([
                    ts_str, o, h, l, cl, vol, oi,
                    expiry_date.isoformat(),
                    instrument_key,
                    NIFTY_LOT_SIZE,
                    dte,
                    is_near,
                    spread_est,
                    margin_estimate,
                ])
            except (IndexError, ValueError, TypeError) as exc:
                logger.debug("Skipping malformed candle: %s", exc)

        # Sort chronologically (API returns newest first)
        rows.sort(key=lambda r: r[0])

        if rows:
            append_rows_to_csv(CSV_PATH, rows)
            total_candles += len(rows)
            if first_ts is None:
                first_ts = rows[0][0]
            last_ts = rows[-1][0]

        tracker.mark_done(key)

    return total_candles, first_ts, last_ts


def run(
    from_date: date,
    to_date: date,
    margin_estimate: float,
) -> None:
    """Main collection loop."""
    token = get_access_token()
    client = UpstoxClient(token)
    tracker = ProgressTracker("futures")

    logger.info("=== Nifty 50 Futures 1-min Collector ===")
    logger.info("Fetching expiry dates …")

    all_expiries = fetch_expiries(client)
    if not all_expiries:
        logger.error("No expiry dates returned. Check your access token.")
        return

    # Filter to requested date range
    expiries = [e for e in all_expiries if from_date <= e <= to_date]
    logger.info(
        "Found %d total expiries, %d in range %s → %s",
        len(all_expiries), len(expiries), from_date, to_date,
    )

    if not expiries:
        logger.warning("No expiries in the requested range.")
        return

    # Determine near-month for each expiry
    sorted_expiries = sorted(expiries)

    est_calls = len(expiries) * 3  # ~3 month-chunks per contract + metadata
    logger.info(
        "  Contracts   : %d\n"
        "  Est. calls  : ~%d\n"
        "  Est. time   : %s\n",
        len(expiries), est_calls, fmt_duration(est_calls * 0.5),
    )

    ensure_csv_header(CSV_PATH, CSV_FIELDS)
    ensure_csv_header(ROLLOVER_CSV, ROLLOVER_FIELDS)

    start_time = time.time()
    rollover_rows: list[list] = []

    for idx, expiry in enumerate(sorted_expiries, 1):
        # Determine if this is the near-month contract
        is_near = (idx == 1) or (
            idx > 1 and expiry == sorted_expiries[idx - 1]
        )
        # More accurately: near-month is the earliest unexpired at any point.
        # For simplicity, mark the first expiry in the sorted list as near.
        # The is_near_month flag is approximate for historical data.
        is_near = True  # All contracts are "near month" at some point

        contract_key_str = f"contract_{expiry.isoformat()}"
        if tracker.is_done(contract_key_str):
            logger.info(
                "[%d/%d] Skipping expiry %s (contract already done)",
                idx, len(sorted_expiries), expiry,
            )
            continue

        logger.info(
            "[%d/%d] Processing expiry %s …",
            idx, len(sorted_expiries), expiry,
        )

        instrument_key = fetch_futures_contract(client, expiry)
        if not instrument_key:
            logger.warning("  No contract found for expiry %s — skipping.", expiry)
            tracker.mark_done(contract_key_str)
            continue

        logger.info("  Contract: %s", instrument_key)

        candle_count, first_ts, last_ts = process_contract(
            client=client,
            instrument_key=instrument_key,
            expiry_date=expiry,
            is_near=is_near,
            tracker=tracker,
            start_date=from_date,
            margin_estimate=margin_estimate,
        )

        rollover_rows.append([
            expiry.isoformat(),
            instrument_key,
            first_ts or "",
            last_ts or "",
            candle_count,
        ])

        tracker.mark_done(contract_key_str)

        elapsed = time.time() - start_time
        remaining = len(sorted_expiries) - idx
        avg = elapsed / idx
        eta = remaining * avg
        logger.info(
            "  → %d candles | ETA: %s",
            candle_count, fmt_duration(eta),
        )

    # Write rollover metadata
    if rollover_rows:
        append_rows_to_csv(ROLLOVER_CSV, rollover_rows)

    elapsed = time.time() - start_time
    logger.info(
        "=== Done ===  Processed %d contracts in %s\n"
        "  Data  : %s\n"
        "  Rollover: %s",
        len(sorted_expiries), fmt_duration(elapsed), CSV_PATH, ROLLOVER_CSV,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect Nifty 50 futures 1-min OHLCV from Upstox V2 API.",
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
        "--margin-estimate",
        type=float,
        default=DEFAULT_FUTURES_MARGIN,
        help="Approximate margin per lot in ₹ (default: 120000).",
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
        ProgressTracker("futures").reset()
        logger.info("Progress tracker reset.")
    run(args.from_date, args.to_date, args.margin_estimate)


if __name__ == "__main__":
    main()
