#!/usr/bin/env python3
"""Enrich the historical options CSV with underlying close, implied volatility,
Greeks (delta, gamma, theta, vega), and moneyness.

Reads:
    data/nifty50_intraday_1min.csv   — timestamp → underlying close lookup
    data/nifty50_options_1min.csv     — raw options OHLCV

Writes:
    data/nifty50_options_1min.csv     — enriched (via temp file + rename)

New columns appended: iv, delta, gamma, theta, vega, moneyness

Usage:
    python -m trading_framework.data_collectors.enrich_options
    nohup python -m trading_framework.data_collectors.enrich_options > enrich.log 2>&1 &
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from typing import Optional

try:
    from scipy.stats import norm
except ImportError:
    print("Error: scipy is required. Install with: pip install scipy")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(os.path.dirname(_SCRIPT_DIR), "data")

INTRADAY_CSV = os.path.join(_DATA_DIR, "nifty50_intraday_1min.csv")
OPTIONS_CSV = os.path.join(_DATA_DIR, "nifty50_options_1min.csv")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RISK_FREE_RATE = 0.065  # 6.5% RBI repo rate
MAX_IV = 2.0            # Cap IV at 200%

# Input CSV columns (16 columns)
INPUT_FIELDS = [
    "timestamp", "open", "high", "low", "close", "volume", "oi",
    "strike_price", "expiry", "option_type", "instrument_key", "lot_size",
    "underlying_close", "days_to_expiry", "spread_estimate", "margin_estimate",
]

# New columns appended by this script
NEW_FIELDS = ["iv", "delta", "gamma", "theta", "vega", "moneyness"]

OUTPUT_FIELDS = INPUT_FIELDS + NEW_FIELDS


# ---------------------------------------------------------------------------
# Black-Scholes pricing
# ---------------------------------------------------------------------------

def bs_call_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes call option price."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)


def bs_put_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes put option price."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def implied_volatility(
    option_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: str,
    max_iter: int = 50,
    tol: float = 1e-5,
) -> float:
    """Compute IV using Newton-Raphson method."""
    if option_price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return 0.0

    # Initial guess
    sigma = 0.2

    for _ in range(max_iter):
        if option_type == "CE":
            price = bs_call_price(S, K, T, r, sigma)
        else:
            price = bs_put_price(S, K, T, r, sigma)

        # Vega (same formula for call and put)
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        vega = S * norm.pdf(d1) * math.sqrt(T)

        if vega < 1e-10:
            break

        diff = price - option_price
        sigma = sigma - diff / vega

        if sigma <= 0:
            sigma = 0.001
        if sigma > MAX_IV:
            sigma = MAX_IV
        if abs(diff) < tol:
            break

    return max(min(sigma, MAX_IV), 0.0)


def compute_greeks(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str,
) -> tuple[float, float, float, float]:
    """Compute delta, gamma, theta, vega."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0, 0.0, 0.0, 0.0

    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    gamma = norm.pdf(d1) / (S * sigma * math.sqrt(T))
    vega = S * norm.pdf(d1) * math.sqrt(T) / 100  # per 1% IV change

    if option_type == "CE":
        delta = norm.cdf(d1)
        theta = (
            -(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
            - r * K * math.exp(-r * T) * norm.cdf(d2)
        ) / 365  # per day
    else:
        delta = norm.cdf(d1) - 1
        theta = (
            -(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
            + r * K * math.exp(-r * T) * norm.cdf(-d2)
        ) / 365  # per day

    return delta, gamma, theta, vega


def get_moneyness(S: float, K: float, option_type: str) -> str:
    """Determine ITM / ATM / OTM.

    ATM if underlying is within ±1% of strike.
    """
    if S <= 0 or K <= 0:
        return "UNKNOWN"
    pct_diff = abs(S - K) / K
    if pct_diff <= 0.01:
        return "ATM"
    if option_type == "CE":
        return "ITM" if S > K else "OTM"
    else:  # PE
        return "ITM" if S < K else "OTM"


# ---------------------------------------------------------------------------
# Intraday lookup builder
# ---------------------------------------------------------------------------

def _parse_timestamp_key(ts_str: str) -> str:
    """Normalise a timestamp string to 'YYYY-MM-DDTHH:MM' for dict lookup.

    Handles both '+05:30' offset and plain formats.
    """
    # Take first 16 chars: '2024-08-28T15:19'
    return ts_str[:16]


def _offset_keys(ts_key: str, offsets: list[int]) -> list[str]:
    """Generate nearby minute-offset keys for fallback matching."""
    try:
        base = datetime.strptime(ts_key, "%Y-%m-%dT%H:%M")
    except ValueError:
        return []
    keys: list[str] = []
    for off in offsets:
        shifted = base + timedelta(minutes=off)
        keys.append(shifted.strftime("%Y-%m-%dT%H:%M"))
    return keys


def load_intraday_lookup(filepath: str) -> dict[str, float]:
    """Load intraday CSV into a {timestamp_key: close} dict.

    Reads the file in streaming fashion to keep memory predictable.
    """
    print(f"Loading intraday index data from {filepath} ...")
    lookup: dict[str, float] = {}
    with open(filepath, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            ts_key = _parse_timestamp_key(row["timestamp"])
            try:
                close_val = float(row["close"])
            except (ValueError, KeyError):
                continue
            lookup[ts_key] = close_val
    print(f"Loaded {len(lookup):,} timestamps into lookup")
    return lookup


def resolve_underlying(
    ts_str: str,
    existing_val: float,
    lookup: dict[str, float],
) -> tuple[float, bool]:
    """Return (underlying_close, was_filled).

    Priority:
      1. If existing_val > 0, keep it.
      2. Exact timestamp match in lookup.
      3. Nearest ±1 min, ±2 min fallback.
    """
    if existing_val > 0:
        return existing_val, False

    ts_key = _parse_timestamp_key(ts_str)

    # Exact match
    val = lookup.get(ts_key)
    if val is not None and val > 0:
        return val, True

    # Fallback: ±1 min, ±2 min
    for offset in [1, -1, 2, -2]:
        for fallback_key in _offset_keys(ts_key, [offset]):
            val = lookup.get(fallback_key)
            if val is not None and val > 0:
                return val, True

    return 0.0, False


# ---------------------------------------------------------------------------
# Main enrichment loop
# ---------------------------------------------------------------------------

def enrich(risk_free_rate: float, batch_size: int) -> None:
    """Stream-process the options CSV, enriching each row."""

    if not os.path.exists(INTRADAY_CSV):
        print(f"Error: Intraday CSV not found at {INTRADAY_CSV}")
        sys.exit(1)
    if not os.path.exists(OPTIONS_CSV):
        print(f"Error: Options CSV not found at {OPTIONS_CSV}")
        sys.exit(1)

    # Step 1 — build underlying lookup
    lookup = load_intraday_lookup(INTRADAY_CSV)

    # Step 2 — count total rows for progress reporting
    print("Counting rows in options CSV ...")
    total_rows = 0
    with open(OPTIONS_CSV, newline="") as fh:
        next(fh)  # skip header
        for _ in fh:
            total_rows += 1
    print(f"Processing options CSV ({total_rows:,} rows) ...")

    # Step 3 — stream-process into a temp file
    tmp_fd, tmp_path = tempfile.mkstemp(
        suffix=".csv", prefix="options_enriched_", dir=_DATA_DIR,
    )
    os.close(tmp_fd)

    # Stats
    rows_processed = 0
    underlying_filled = 0
    iv_computed = 0
    moneyness_counts: dict[str, int] = {"ITM": 0, "ATM": 0, "OTM": 0, "UNKNOWN": 0}
    t_start = time.time()

    try:
        with (
            open(OPTIONS_CSV, newline="") as in_fh,
            open(tmp_path, "w", newline="") as out_fh,
        ):
            reader = csv.DictReader(in_fh)
            writer = csv.writer(out_fh)

            # Write header
            writer.writerow(OUTPUT_FIELDS)

            for row in reader:
                rows_processed += 1

                # --- Parse numeric fields safely ---
                ts_str = row.get("timestamp", "")
                try:
                    option_close = float(row.get("close", 0))
                except (ValueError, TypeError):
                    option_close = 0.0
                try:
                    strike = float(row.get("strike_price", 0))
                except (ValueError, TypeError):
                    strike = 0.0
                try:
                    existing_underlying = float(row.get("underlying_close", 0))
                except (ValueError, TypeError):
                    existing_underlying = 0.0
                try:
                    dte_raw = float(row.get("days_to_expiry", 0))
                except (ValueError, TypeError):
                    dte_raw = 0.0

                option_type = row.get("option_type", "CE")

                # --- Resolve underlying close ---
                underlying, was_filled = resolve_underlying(
                    ts_str, existing_underlying, lookup,
                )
                if was_filled:
                    underlying_filled += 1

                # --- Time to expiry in years (minimum 1/365 to avoid div-by-zero) ---
                T = max(dte_raw / 365.0, 1.0 / 365.0) if dte_raw >= 0 else 1.0 / 365.0

                # --- Compute IV and Greeks ---
                iv_val = 0.0
                delta_val = 0.0
                gamma_val = 0.0
                theta_val = 0.0
                vega_val = 0.0

                if option_close > 0 and underlying > 0 and strike > 0:
                    iv_val = implied_volatility(
                        option_close, underlying, strike, T,
                        risk_free_rate, option_type,
                    )
                    if iv_val > 0:
                        iv_computed += 1
                        delta_val, gamma_val, theta_val, vega_val = compute_greeks(
                            underlying, strike, T, risk_free_rate, iv_val, option_type,
                        )

                # --- Moneyness ---
                money = get_moneyness(underlying, strike, option_type)
                moneyness_counts[money] = moneyness_counts.get(money, 0) + 1

                # --- Update underlying_close in the row ---
                row["underlying_close"] = underlying

                # --- Build output row in field order ---
                out_row = [row.get(f, "") for f in INPUT_FIELDS]
                out_row.extend([
                    f"{iv_val:.6f}",
                    f"{delta_val:.6f}",
                    f"{gamma_val:.8f}",
                    f"{theta_val:.4f}",
                    f"{vega_val:.4f}",
                    money,
                ])
                writer.writerow(out_row)

                # --- Progress ---
                if rows_processed % batch_size == 0:
                    elapsed = time.time() - t_start
                    rate = rows_processed / elapsed if elapsed > 0 else 0
                    remaining = (total_rows - rows_processed) / rate if rate > 0 else 0
                    pct = rows_processed / total_rows * 100
                    print(
                        f"[{rows_processed // 1000}K/{total_rows // 1000}K] "
                        f"{pct:.1f}% done | "
                        f"{rate:,.0f} rows/sec | "
                        f"ETA: {remaining / 60:.1f} min | "
                        f"underlying_filled: {underlying_filled:,}/{rows_processed:,} "
                        f"({underlying_filled / rows_processed * 100:.1f}%) | "
                        f"iv_computed: {iv_computed:,}/{rows_processed:,} "
                        f"({iv_computed / rows_processed * 100:.1f}%)"
                    )

        # Step 4 — replace original with enriched file
        os.replace(tmp_path, OPTIONS_CSV)

    except BaseException:
        # Clean up temp file on any failure
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    # --- Final summary ---
    elapsed = time.time() - t_start
    rate = rows_processed / elapsed if elapsed > 0 else 0
    print(f"\nDone! Enriched CSV written to {OPTIONS_CSV}")
    print(f"  Time elapsed:       {elapsed / 60:.1f} min ({rate:,.0f} rows/sec)")
    print(f"  Rows processed:     {rows_processed:,}")
    print(
        f"  Underlying filled:  {underlying_filled:,} "
        f"({underlying_filled / max(rows_processed, 1) * 100:.1f}%)"
    )
    print(
        f"  IV computed:        {iv_computed:,} "
        f"({iv_computed / max(rows_processed, 1) * 100:.1f}%)"
    )
    itm = moneyness_counts.get("ITM", 0)
    atm = moneyness_counts.get("ATM", 0)
    otm = moneyness_counts.get("OTM", 0)
    unk = moneyness_counts.get("UNKNOWN", 0)
    print(f"  Moneyness:          ITM={itm:,}, ATM={atm:,}, OTM={otm:,}, UNKNOWN={unk:,}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich options CSV with underlying close, IV, and Greeks",
    )
    parser.add_argument(
        "--risk-free-rate",
        type=float,
        default=RISK_FREE_RATE,
        help=f"Risk-free rate (default: {RISK_FREE_RATE})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100_000,
        help="Progress report interval (default: 100000)",
    )
    args = parser.parse_args()

    enrich(risk_free_rate=args.risk_free_rate, batch_size=args.batch_size)


if __name__ == "__main__":
    main()
