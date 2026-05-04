#!/usr/bin/env python3
"""Collect Nifty 50 Options 1-minute OHLCV candles from the Upstox V2
expired-instruments API.

Process
-------
1. Fetch all expiry dates for Nifty 50.
2. For each expiry, fetch all option contracts (CE + PE at various strikes).
3. Filter to strikes within ±10 % of the underlying price.
4. For each contract, fetch 1-min candles month-by-month.
5. Write rows to a single CSV in append mode with contract metadata.
6. Also produce a contracts metadata CSV.

Output
------
trading_framework/data/nifty50_options_1min.csv
trading_framework/data/nifty50_options_contracts.csv

Usage
-----
    python -m trading_framework.data_collectors.collect_options
    python -m trading_framework.data_collectors.collect_options --from-date 2023-01-01
    python -m trading_framework.data_collectors.collect_options --max-strikes 10
    nohup python -m trading_framework.data_collectors.collect_options > options.log 2>&1 &
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta

from trading_framework.data_collectors._common import (
    DATA_DIR,
    DEFAULT_OPTIONS_MARGIN,
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

CSV_PATH = os.path.join(DATA_DIR, "nifty50_options_1min.csv")
CSV_FIELDS = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "oi",
    "strike_price",
    "expiry",
    "option_type",
    "instrument_key",
    "lot_size",
    "underlying_close",
    "days_to_expiry",
    "spread_estimate",
    "margin_estimate",
]

CONTRACTS_CSV = os.path.join(DATA_DIR, "nifty50_options_contracts.csv")
CONTRACTS_FIELDS = [
    "expiry_date",
    "strike_price",
    "option_type",
    "instrument_key",
    "total_candles",
]

# Default: only fetch the last 2 years of expiries
DEFAULT_LOOKBACK_YEARS = 2
DEFAULT_MAX_STRIKES = 20  # per side (CE / PE) per expiry
DEFAULT_START = date(2022, 1, 1)

logger = setup_logging("collect_options")


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def fetch_expiries(client: UpstoxClient) -> list[date]:
    """Get all available expiry dates for Nifty 50."""
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
            d = item if isinstance(item, str) else str(item)
            expiries.append(date.fromisoformat(d[:10]))
        except (ValueError, TypeError) as exc:
            logger.debug("Skipping unparseable expiry %r: %s", item, exc)
    expiries.sort()
    return expiries


def fetch_option_contracts(
    client: UpstoxClient,
    expiry_date: date,
) -> list[dict]:
    """Get all option contracts (CE + PE) for a given expiry.

    Returns a list of dicts with keys: instrument_key, strike_price, option_type.
    """
    encoded = UpstoxClient.encode_key(NIFTY_INDEX_KEY)
    url = (
        f"{UpstoxClient.BASE_V2}/expired-instruments/option/contract"
        f"?instrument_key={encoded}&expiry_date={expiry_date.isoformat()}"
    )
    data = client.get(url)
    if data.get("status") != "success":
        logger.warning("No option contracts for expiry %s: %s", expiry_date, data)
        return []

    contracts_raw = data.get("data", [])
    contracts: list[dict] = []
    for c in contracts_raw:
        if isinstance(c, dict):
            try:
                opt_type = c.get("option_type") or ""
                # If option_type is None/empty, extract from trading_symbol
                if not opt_type:
                    symbol = c.get("trading_symbol", "")
                    if " CE " in symbol or symbol.endswith(" CE"):
                        opt_type = "CE"
                    elif " PE " in symbol or symbol.endswith(" PE"):
                        opt_type = "PE"
                contracts.append({
                    "instrument_key": c.get("instrument_key", ""),
                    "strike_price": float(c.get("strike_price", 0)),
                    "option_type": opt_type,
                })
            except (ValueError, TypeError):
                pass
    return contracts


def fetch_candles_expired(
    client: UpstoxClient,
    instrument_key: str,
    from_date: date,
    to_date: date,
) -> list[list]:
    """Fetch 1-min candles for an expired option contract via V2 API."""
    encoded = UpstoxClient.encode_key(instrument_key)
    url = (
        f"{UpstoxClient.BASE_V2}/expired-instruments/historical-candle/"
        f"{encoded}/1minute/{to_date.isoformat()}/{from_date.isoformat()}"
    )
    data = client.get(url)
    if data.get("status") != "success":
        return []
    return data.get("data", {}).get("candles", [])


def fetch_underlying_close_map(
    client: UpstoxClient,
    from_date: date,
    to_date: date,
) -> dict[str, float]:
    """Fetch Nifty 50 index 1-min candles and build a timestamp→close map.

    Used to populate the ``underlying_close`` column in the options CSV.
    Chunks requests into 29-day segments to stay within API limits.
    """
    encoded = UpstoxClient.encode_key(NIFTY_INDEX_KEY)
    close_map: dict[str, float] = {}

    # Chunk into 29-day segments (API limit for 1-min candles)
    current = from_date
    while current <= to_date:
        chunk_end = min(current + timedelta(days=29), to_date)
        url = (
            f"{UpstoxClient.BASE_V3}/historical-candle/"
            f"{encoded}/minutes/1/{chunk_end.isoformat()}/{current.isoformat()}"
        )
        data = client.get(url)
        if data.get("status") == "success":
            candles = data.get("data", {}).get("candles", [])
            for c in candles:
                try:
                    close_map[c[0]] = float(c[4])
                except (IndexError, ValueError, TypeError):
                    pass
        current = chunk_end + timedelta(days=1)

    return close_map


# ---------------------------------------------------------------------------
# Strike filtering
# ---------------------------------------------------------------------------

def filter_strikes(
    contracts: list[dict],
    underlying_price: float,
    max_strikes: int,
    pct_range: float = 0.10,
) -> list[dict]:
    """Keep only strikes within ±pct_range of the underlying, capped at
    *max_strikes* per side (CE / PE)."""
    low = underlying_price * (1 - pct_range)
    high = underlying_price * (1 + pct_range)

    in_range = [c for c in contracts if low <= c["strike_price"] <= high]

    # Split by option type and keep closest strikes
    ce = sorted(
        [c for c in in_range if c["option_type"] == "CE"],
        key=lambda c: abs(c["strike_price"] - underlying_price),
    )[:max_strikes]
    pe = sorted(
        [c for c in in_range if c["option_type"] == "PE"],
        key=lambda c: abs(c["strike_price"] - underlying_price),
    )[:max_strikes]

    return ce + pe


def estimate_underlying_price(
    client: UpstoxClient,
    ref_date: date,
) -> float:
    """Get an approximate Nifty 50 close price near *ref_date* for strike
    filtering.  Falls back to a sensible default."""
    # Fetch a single day candle around the reference date
    encoded = UpstoxClient.encode_key(NIFTY_INDEX_KEY)
    from_d = ref_date - timedelta(days=7)
    url = (
        f"{UpstoxClient.BASE_V3}/historical-candle/"
        f"{encoded}/minutes/1/{ref_date.isoformat()}/{from_d.isoformat()}"
    )
    data = client.get(url)
    if data.get("status") == "success":
        candles = data.get("data", {}).get("candles", [])
        if candles:
            # Return the close of the most recent candle
            try:
                return float(candles[0][4])
            except (IndexError, ValueError, TypeError):
                pass
    # Fallback
    return 18_000.0


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def process_option_contract(
    client: UpstoxClient,
    contract: dict,
    expiry_date: date,
    underlying_map: dict[str, float],
    tracker: ProgressTracker,
    start_date: date,
    margin_estimate: float,
) -> int:
    """Fetch all 1-min candles for one option contract and append to CSV.

    Returns the number of candles written.
    """
    instrument_key = contract["instrument_key"]
    strike = contract["strike_price"]
    opt_type = contract["option_type"]

    # Options are typically active from listing to expiry.
    # Conservatively fetch from 2 months before expiry.
    contract_start = max(expiry_date - timedelta(days=60), start_date)
    contract_end = expiry_date

    ranges = get_month_ranges(contract_start, contract_end)
    total = 0

    for chunk_from, chunk_to in ranges:
        key = f"opt_{instrument_key}_{chunk_key(chunk_from, chunk_to)}"
        if tracker.is_done(key):
            continue

        raw_candles = fetch_candles_expired(client, instrument_key, chunk_from, chunk_to)
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
                spread_est = round((h - l) * 0.02, 4)  # wider for options
                underlying_cl = underlying_map.get(ts_str, 0.0)

                rows.append([
                    ts_str, o, h, l, cl, vol, oi,
                    strike,
                    expiry_date.isoformat(),
                    opt_type,
                    instrument_key,
                    NIFTY_LOT_SIZE,
                    underlying_cl,
                    dte,
                    spread_est,
                    margin_estimate,
                ])
            except (IndexError, ValueError, TypeError) as exc:
                logger.debug("Skipping malformed candle: %s", exc)

        rows.sort(key=lambda r: r[0])

        if rows:
            append_rows_to_csv(CSV_PATH, rows)
            total += len(rows)

        tracker.mark_done(key)

    return total


def run(
    from_date: date,
    to_date: date,
    max_strikes: int,
    margin_estimate: float,
    lookback_years: int,
) -> None:
    """Main collection loop."""
    token = get_access_token()
    client = UpstoxClient(token)
    tracker = ProgressTracker("options")

    logger.info("=== Nifty 50 Options 1-min Collector ===")
    logger.info("Fetching expiry dates …")

    all_expiries = fetch_expiries(client)
    if not all_expiries:
        logger.error("No expiry dates returned. Check your access token.")
        return

    # Limit to last N years and requested range
    cutoff = date.today() - timedelta(days=lookback_years * 365)
    effective_start = max(from_date, cutoff)
    expiries = [e for e in all_expiries if effective_start <= e <= to_date]
    logger.info(
        "Found %d total expiries, %d in range %s → %s (lookback %d yr)",
        len(all_expiries), len(expiries), effective_start, to_date, lookback_years,
    )

    if not expiries:
        logger.warning("No expiries in the requested range.")
        return

    # Rough estimate: each expiry → ~max_strikes*2 contracts × ~3 API calls
    est_calls = len(expiries) * max_strikes * 2 * 3
    logger.info(
        "  Expiries    : %d\n"
        "  Max strikes : %d per side\n"
        "  Est. calls  : ~%d (upper bound)\n"
        "  Est. time   : %s\n",
        len(expiries), max_strikes, est_calls, fmt_duration(est_calls * 0.5),
    )

    ensure_csv_header(CSV_PATH, CSV_FIELDS)
    ensure_csv_header(CONTRACTS_CSV, CONTRACTS_FIELDS)

    start_time = time.time()
    contract_meta_rows: list[list] = []

    for exp_idx, expiry in enumerate(sorted(expiries), 1):
        expiry_key = f"expiry_{expiry.isoformat()}"
        if tracker.is_done(expiry_key):
            logger.info(
                "[%d/%d] Skipping expiry %s (already done)",
                exp_idx, len(expiries), expiry,
            )
            continue

        logger.info(
            "[%d/%d] Processing expiry %s …",
            exp_idx, len(expiries), expiry,
        )

        # 1. Get option contracts for this expiry
        contracts = fetch_option_contracts(client, expiry)
        if not contracts:
            logger.warning("  No contracts for expiry %s — skipping.", expiry)
            tracker.mark_done(expiry_key)
            continue

        # 2. Estimate underlying price for strike filtering
        underlying_price = estimate_underlying_price(client, expiry)
        logger.info(
            "  Underlying ≈ %.0f | %d raw contracts",
            underlying_price, len(contracts),
        )

        # 3. Filter strikes
        filtered = filter_strikes(contracts, underlying_price, max_strikes)
        logger.info(
            "  Filtered to %d contracts (±10%% of %.0f, max %d/side)",
            len(filtered), underlying_price, max_strikes,
        )

        if not filtered:
            tracker.mark_done(expiry_key)
            continue

        # 4. Skip underlying close map — will be filled by enrich_options.py
        #    from nifty50_intraday_1min.csv to save API calls
        underlying_map: dict[str, float] = {}
        logger.info("  Underlying close: deferred to enrichment step")

        # 5. Process each contract
        for c_idx, contract in enumerate(filtered, 1):
            ikey = contract["instrument_key"]
            strike = contract["strike_price"]
            otype = contract["option_type"]

            logger.info(
                "    [%d/%d] %s %.0f %s",
                c_idx, len(filtered), otype, strike, ikey,
            )

            candle_count = process_option_contract(
                client=client,
                contract=contract,
                expiry_date=expiry,
                underlying_map=underlying_map,
                tracker=tracker,
                start_date=effective_start,
                margin_estimate=margin_estimate,
            )

            contract_meta_rows.append([
                expiry.isoformat(),
                strike,
                otype,
                ikey,
                candle_count,
            ])

            logger.info("      → %d candles", candle_count)

        tracker.mark_done(expiry_key)

        elapsed = time.time() - start_time
        remaining = len(expiries) - exp_idx
        avg = elapsed / exp_idx
        eta = remaining * avg
        logger.info(
            "  Expiry %s done | ETA for remaining: %s",
            expiry, fmt_duration(eta),
        )

    # Write contracts metadata
    if contract_meta_rows:
        append_rows_to_csv(CONTRACTS_CSV, contract_meta_rows)

    elapsed = time.time() - start_time
    logger.info(
        "=== Done ===  Processed %d expiries in %s\n"
        "  Data      : %s\n"
        "  Contracts : %s",
        len(expiries), fmt_duration(elapsed), CSV_PATH, CONTRACTS_CSV,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect Nifty 50 options 1-min OHLCV from Upstox V2 API.",
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
        "--max-strikes",
        type=int,
        default=DEFAULT_MAX_STRIKES,
        help="Max strikes per side (CE/PE) per expiry (default: 20).",
    )
    parser.add_argument(
        "--lookback-years",
        type=int,
        default=DEFAULT_LOOKBACK_YEARS,
        help="Only fetch the last N years of expiries (default: 2).",
    )
    parser.add_argument(
        "--margin-estimate",
        type=float,
        default=DEFAULT_OPTIONS_MARGIN,
        help="Approximate margin per lot in ₹ (default: 50000).",
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
        ProgressTracker("options").reset()
        logger.info("Progress tracker reset.")
    run(
        from_date=args.from_date,
        to_date=args.to_date,
        max_strikes=args.max_strikes,
        margin_estimate=args.margin_estimate,
        lookback_years=args.lookback_years,
    )


if __name__ == "__main__":
    main()
