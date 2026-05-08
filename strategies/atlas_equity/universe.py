"""Stock universe management — Nifty 500 constituents."""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Nifty 50 symbols (always available as fallback)
NIFTY_50 = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR",
    "SBIN", "BHARTIARTL", "ITC", "KOTAKBANK", "LT", "AXISBANK",
    "ASIANPAINT", "MARUTI", "TITAN", "SUNPHARMA", "BAJFINANCE",
    "WIPRO", "HCLTECH", "ULTRACEMCO", "NTPC", "POWERGRID", "TATAMOTORS",
    "M&M", "NESTLEIND", "JSWSTEEL", "TATASTEEL", "ADANIENT", "ADANIPORTS",
    "TECHM", "INDUSINDBK", "BAJAJFINSV", "HINDALCO", "ONGC", "COALINDIA",
    "GRASIM", "CIPLA", "DRREDDY", "APOLLOHOSP", "EICHERMOT", "HEROMOTOCO",
    "DIVISLAB", "BPCL", "TATACONSUM", "BRITANNIA", "SBILIFE", "HDFCLIFE",
    "BAJAJ-AUTO", "SHRIRAMFIN", "LTIM",
]

# Cache file for full Nifty 500 list
_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".nifty500_cache.json")


def get_nifty500_symbols() -> list[str]:
    """Get Nifty 500 constituent symbols.

    Uses cached list if available and recent (< 7 days old).
    Falls back to Nifty 50 if fetch fails.
    """
    # Check cache
    if os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE) as f:
                cache = json.load(f)
            cached_date = datetime.fromisoformat(cache["date"])
            if datetime.now() - cached_date < timedelta(days=7):
                return cache["symbols"]
        except Exception:
            pass

    # Try to fetch from NSE (may fail due to rate limiting)
    symbols = _fetch_nifty500_from_nse()
    if symbols:
        # Cache it
        try:
            with open(_CACHE_FILE, "w") as f:
                json.dump({"date": datetime.now().isoformat(), "symbols": symbols}, f)
        except Exception:
            pass
        return symbols

    # Fallback to Nifty 50
    logger.warning("Could not fetch Nifty 500 list, using Nifty 50 fallback")
    return NIFTY_50


def _fetch_nifty500_from_nse() -> list[str]:
    """Attempt to fetch Nifty 500 list from NSE website."""
    url = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/csv",
        })
        resp = urllib.request.urlopen(req, timeout=15)
        csv_data = resp.read().decode("utf-8")

        symbols = []
        for line in csv_data.strip().split("\n")[1:]:  # skip header
            parts = line.split(",")
            if len(parts) >= 3:
                symbol = parts[2].strip().strip('"')
                if symbol and symbol != "Symbol":
                    symbols.append(symbol)

        if len(symbols) > 100:
            logger.info("Fetched %d Nifty 500 symbols from NSE", len(symbols))
            return symbols
    except Exception as e:
        logger.debug("NSE fetch failed: %s", e)

    return []


def get_universe(name: str = "NIFTY500") -> list[str]:
    """Get stock universe by name."""
    if name == "NIFTY50":
        return NIFTY_50
    elif name == "NIFTY500":
        return get_nifty500_symbols()
    else:
        return NIFTY_50
