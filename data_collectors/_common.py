"""Shared utilities for data collectors: API client, resume support, rate limiting."""

from __future__ import annotations

import csv
import json
import logging
import os
import time
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
PROGRESS_DIR = os.path.join(DATA_DIR, ".progress")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Upstox API client with rate limiting
# ---------------------------------------------------------------------------

class UpstoxClient:
    """Rate-limited Upstox API client."""

    BASE_V3 = "https://api.upstox.com/v3"
    BASE_V2 = "https://api.upstox.com/v2"

    def __init__(self, access_token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        })
        self._request_count = 0
        self._last_request_time = 0.0

    def get(self, url: str, timeout: int = 30) -> dict:
        """Make a rate-limited GET request.

        Enforces ~40 req/sec (buffer from the 50/sec hard limit) and pauses
        for 30 s every 400 requests to stay under the 2 000/30 min ceiling.
        """
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < 0.025:  # ~40 req/sec
            time.sleep(0.025 - elapsed)

        self._last_request_time = time.time()
        self._request_count += 1

        # Every 400 requests, pause for 30 seconds (stay under 2000/30min)
        if self._request_count % 400 == 0:
            logger.info(
                "Rate-limit pause (30 s) after %d requests …",
                self._request_count,
            )
            time.sleep(30)

        try:
            response = self.session.get(url, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.error("API request failed: %s — %s", url, exc)
            return {"status": "error", "message": str(exc)}

    @staticmethod
    def encode_key(key: str) -> str:
        """URL-encode an instrument key for use in API path segments."""
        return urllib.parse.quote(key, safe="")


# ---------------------------------------------------------------------------
# Resume / progress tracker
# ---------------------------------------------------------------------------

class ProgressTracker:
    """Tracks which date ranges / contracts have been fetched so the script
    can resume after a crash or manual stop."""

    def __init__(self, name: str):
        os.makedirs(PROGRESS_DIR, exist_ok=True)
        self.filepath = os.path.join(PROGRESS_DIR, f"{name}_progress.json")
        self.completed: set[str] = set()
        self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            with open(self.filepath) as fh:
                self.completed = set(json.load(fh))
            logger.info(
                "Resumed: %d chunks already completed for %s",
                len(self.completed),
                self.filepath,
            )

    def _save(self):
        with open(self.filepath, "w") as fh:
            json.dump(sorted(self.completed), fh)

    def is_done(self, key: str) -> bool:
        return key in self.completed

    def mark_done(self, key: str):
        self.completed.add(key)
        self._save()

    def reset(self):
        """Clear all progress (useful for a full re-download)."""
        self.completed.clear()
        self._save()


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def load_env() -> dict[str, str]:
    """Load key=value pairs from ``trading_framework/.env``."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    env: dict[str, str] = {}
    try:
        with open(env_path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def get_access_token() -> str:
    """Return the Upstox access token from the environment or .env file."""
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        env = load_env()
        token = env.get("UPSTOX_ACCESS_TOKEN", "")
    if not token or token == "your_access_token_here":
        # Fall back to sandbox token
        if not token or token == "your_access_token_here":
            env = load_env()
            token = env.get("UPSTOX_SANDBOX_TOKEN", "")
    if not token:
        raise RuntimeError(
            "No Upstox access token found. Set UPSTOX_ACCESS_TOKEN in the "
            "environment or in trading_framework/.env"
        )
    return token


# ---------------------------------------------------------------------------
# Date-range helpers
# ---------------------------------------------------------------------------

def get_month_ranges(
    start_date: date,
    end_date: date,
    max_days: int = 29,
) -> list[tuple[date, date]]:
    """Split a date range into chunks of *max_days* (default 29 for the
    1-month API limit on minute-level data)."""
    ranges: list[tuple[date, date]] = []
    current = start_date
    while current <= end_date:
        chunk_end = min(current + timedelta(days=max_days), end_date)
        ranges.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return ranges


def chunk_key(from_d: date, to_d: date) -> str:
    """Deterministic string key for a date-range chunk."""
    return f"{from_d.isoformat()}_{to_d.isoformat()}"


# ---------------------------------------------------------------------------
# CSV helpers (append-mode, low memory)
# ---------------------------------------------------------------------------

def ensure_csv_header(filepath: str, fieldnames: list[str]) -> None:
    """Create the CSV file with a header row if it does not already exist."""
    if not os.path.exists(filepath):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(fieldnames)


def append_rows_to_csv(filepath: str, rows: list[list]) -> None:
    """Append rows to an existing CSV (no header)."""
    with open(filepath, "a", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Time / display helpers
# ---------------------------------------------------------------------------

def is_trading_hour(ts: datetime) -> bool:
    """Return True if *ts* falls within NSE trading hours (09:15–15:30 IST)."""
    ist_ts = ts.astimezone(IST) if ts.tzinfo else ts.replace(tzinfo=IST)
    t = ist_ts.time()
    return time_in_range(t, datetime.strptime("09:15", "%H:%M").time(),
                         datetime.strptime("15:30", "%H:%M").time())


def time_in_range(t, start, end) -> bool:
    return start <= t <= end


def fmt_duration(seconds: float) -> str:
    """Human-readable duration string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def setup_logging(name: str, level: int = logging.INFO) -> logging.Logger:
    """Configure a logger that writes to both stderr and a log file."""
    log = logging.getLogger(name)
    log.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    log.addHandler(ch)
    # File handler (in data dir)
    os.makedirs(DATA_DIR, exist_ok=True)
    fh = logging.FileHandler(os.path.join(DATA_DIR, f"{name}.log"))
    fh.setFormatter(fmt)
    log.addHandler(fh)
    return log


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"
NIFTY_LOT_SIZE = 25
DEFAULT_TRANSACTION_COST = 500.0        # ₹ per round trip
DEFAULT_FUTURES_MARGIN = 120_000.0      # ₹ per lot (approximate)
DEFAULT_OPTIONS_MARGIN = 50_000.0       # ₹ per lot (approximate, buyer side)

MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"

# NSE trading holidays (approximate — scripts simply get empty data for these)
NSE_HOLIDAYS_2022_2026: set[date] = set()
