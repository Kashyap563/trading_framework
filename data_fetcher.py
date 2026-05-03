"""Generic data fetcher for historical candle and option chain data from Upstox API.

Supports:
- 1-minute candles (month-by-month fetching, up to 2 years = 24 API calls)
- 5-minute aggregation from 1-minute data
- 30-minute candles (native from API, 1 year range)
- Day candles (native from API, 1 year range)
- Option chain fetching (contracts + historical OHLC for options)
- Expired option contract candles

Upstox Historical Candle API:
- Endpoint: GET /v2/historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}
- 1-minute data: limited to last 1 month from to_date
- Candles returned in REVERSE chronological order (newest first)
"""

from __future__ import annotations

import csv
import logging
import time as time_mod
import urllib.parse
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from trading_framework.models import Candle, OptionData

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

logger = logging.getLogger(__name__)


def _encode_instrument_key(key: str) -> str:
    """URL-encode an instrument key for use in API paths."""
    return urllib.parse.quote(key, safe="")


class UpstoxDataFetcher:
    """Fetches candle and option data from the Upstox API."""

    BASE_URL = "https://api.upstox.com/v2"

    def __init__(self, access_token: str, instrument_key: str = "NSE_INDEX|Nifty 50") -> None:
        """Initialize with an Upstox API access token.

        Args:
            access_token: Bearer token for Upstox API authentication.
            instrument_key: Default instrument key (e.g., "NSE_INDEX|Nifty 50").
        """
        self.access_token = access_token
        self.instrument_key = instrument_key
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        })

    # ------------------------------------------------------------------
    # Core candle fetching
    # ------------------------------------------------------------------

    def _fetch_candles_raw(
        self,
        instrument_key: str,
        interval: str,
        from_date: date,
        to_date: date,
        *,
        expired: bool = False,
    ) -> list[Candle]:
        """Fetch candles for a single API call window.

        Args:
            instrument_key: Upstox instrument key.
            interval: One of "1minute", "3minute", "5minute", "15minute",
                      "30minute", "day".
            from_date: Start date (inclusive).
            to_date: End date (inclusive).
            expired: If True, use the expired-instruments endpoint.

        Returns:
            List of Candle objects sorted chronologically.
        """
        encoded_key = _encode_instrument_key(instrument_key)
        if expired:
            url = (
                f"{self.BASE_URL}/expired-instruments/historical-candle/"
                f"{encoded_key}/{interval}/{to_date.isoformat()}/{from_date.isoformat()}"
            )
        else:
            url = (
                f"{self.BASE_URL}/historical-candle/"
                f"{encoded_key}/{interval}/{to_date.isoformat()}/{from_date.isoformat()}"
            )

        logger.info("Fetching %s candles: %s to %s", interval, from_date, to_date)

        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.error("API request failed for %s to %s: %s", from_date, to_date, exc)
            return []

        data = response.json()
        if data.get("status") != "success":
            logger.error("API returned non-success status: %s", data)
            return []

        raw_candles = data.get("data", {}).get("candles", [])
        if not raw_candles:
            logger.warning("No candles returned for %s to %s", from_date, to_date)
            return []

        candles: list[Candle] = []
        for c in raw_candles:
            # Each candle: [timestamp, open, high, low, close, volume, oi]
            try:
                ts = datetime.fromisoformat(c[0])
                candles.append(Candle(
                    timestamp=ts,
                    open=float(c[1]),
                    high=float(c[2]),
                    low=float(c[3]),
                    close=float(c[4]),
                    volume=int(c[5]) if len(c) > 5 else 0,
                ))
            except (IndexError, ValueError, TypeError) as exc:
                logger.warning("Skipping malformed candle %s: %s", c, exc)

        # API returns newest first; sort chronologically
        candles.sort(key=lambda x: x.timestamp)
        return candles

    # ------------------------------------------------------------------
    # 1-minute candles (month-by-month, up to 2 years)
    # ------------------------------------------------------------------

    def fetch_1min_candles(
        self,
        from_date: date,
        to_date: date,
        instrument_key: Optional[str] = None,
    ) -> list[Candle]:
        """Fetch 1-minute candles for a date range, month by month.

        The Upstox API limits 1-minute data to ~1 month per request,
        so this method iterates month by month across the range.

        Args:
            from_date: Start date of the range.
            to_date: End date of the range.
            instrument_key: Override the default instrument key.

        Returns:
            List of Candle objects sorted chronologically.
        """
        key = instrument_key or self.instrument_key
        all_candles: list[Candle] = []
        current_start = from_date

        while current_start < to_date:
            current_end = min(current_start + timedelta(days=30), to_date)
            chunk = self._fetch_candles_raw(key, "1minute", current_start, current_end)
            all_candles.extend(chunk)

            logger.info(
                "Fetched %d candles for %s to %s (total so far: %d)",
                len(chunk), current_start, current_end, len(all_candles),
            )

            current_start = current_end + timedelta(days=1)
            time_mod.sleep(0.5)  # Rate-limit politeness

        # De-duplicate by timestamp
        seen: set[datetime] = set()
        unique: list[Candle] = []
        for candle in all_candles:
            if candle.timestamp not in seen:
                seen.add(candle.timestamp)
                unique.append(candle)

        unique.sort(key=lambda x: x.timestamp)
        logger.info("Total unique 1-min candles fetched: %d", len(unique))
        return unique

    # ------------------------------------------------------------------
    # 5-minute aggregation from 1-minute data
    # ------------------------------------------------------------------

    def aggregate_to_5min(self, candles_1min: list[Candle]) -> list[Candle]:
        """Aggregate 1-minute candles into 5-minute candles.

        Groups candles into 5-minute windows aligned to market open
        (9:15-9:20, 9:20-9:25, etc.).

        Args:
            candles_1min: List of 1-minute Candle objects, sorted chronologically.

        Returns:
            List of 5-minute Candle objects, sorted chronologically.
        """
        if not candles_1min:
            return []

        def _get_5min_bucket(ts: datetime) -> datetime:
            ist_ts = ts.astimezone(IST)
            total_minutes = ist_ts.hour * 60 + ist_ts.minute
            market_open_minutes = 9 * 60 + 15
            minutes_since_open = total_minutes - market_open_minutes
            bucket_offset = (minutes_since_open // 5) * 5
            bucket_minute = market_open_minutes + bucket_offset
            bucket_hour = bucket_minute // 60
            bucket_min = bucket_minute % 60
            return ist_ts.replace(
                hour=bucket_hour, minute=bucket_min, second=0, microsecond=0,
            )

        buckets: dict[tuple[date, datetime], list[Candle]] = {}
        for candle in candles_1min:
            ist_ts = candle.timestamp.astimezone(IST)
            trading_date = ist_ts.date()
            bucket_start = _get_5min_bucket(candle.timestamp)
            key = (trading_date, bucket_start)
            buckets.setdefault(key, []).append(candle)

        candles_5min: list[Candle] = []
        for _key, group in sorted(buckets.items()):
            if not group:
                continue
            group.sort(key=lambda x: x.timestamp)
            candles_5min.append(Candle(
                timestamp=group[0].timestamp,
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
            ))

        candles_5min.sort(key=lambda x: x.timestamp)
        logger.info(
            "Aggregated %d 1-min candles into %d 5-min candles",
            len(candles_1min), len(candles_5min),
        )
        return candles_5min

    # ------------------------------------------------------------------
    # Native interval candles (30min, day)
    # ------------------------------------------------------------------

    def fetch_30min_candles(
        self,
        from_date: date,
        to_date: date,
        instrument_key: Optional[str] = None,
    ) -> list[Candle]:
        """Fetch 30-minute candles natively from the API (up to 1 year range).

        Args:
            from_date: Start date.
            to_date: End date.
            instrument_key: Override the default instrument key.

        Returns:
            List of Candle objects sorted chronologically.
        """
        key = instrument_key or self.instrument_key
        return self._fetch_candles_raw(key, "30minute", from_date, to_date)

    def fetch_day_candles(
        self,
        from_date: date,
        to_date: date,
        instrument_key: Optional[str] = None,
    ) -> list[Candle]:
        """Fetch daily candles natively from the API (up to 1 year range).

        Args:
            from_date: Start date.
            to_date: End date.
            instrument_key: Override the default instrument key.

        Returns:
            List of Candle objects sorted chronologically.
        """
        key = instrument_key or self.instrument_key
        return self._fetch_candles_raw(key, "day", from_date, to_date)

    # ------------------------------------------------------------------
    # Convenience: fetch candles by interval string
    # ------------------------------------------------------------------

    def fetch_candles(
        self,
        interval: str,
        months_back: int = 24,
        instrument_key: Optional[str] = None,
    ) -> list[Candle]:
        """Fetch candles for the given interval and lookback period.

        Args:
            interval: One of "1min", "5min", "30min", "day".
            months_back: Number of months of historical data.
            instrument_key: Override the default instrument key.

        Returns:
            List of Candle objects sorted chronologically.
        """
        to_date = date.today()
        from_date = to_date - timedelta(days=months_back * 30)

        logger.info(
            "Fetching %s candles for %d months: %s to %s",
            interval, months_back, from_date, to_date,
        )

        if interval == "1min":
            return self.fetch_1min_candles(from_date, to_date, instrument_key)
        elif interval == "5min":
            candles_1min = self.fetch_1min_candles(from_date, to_date, instrument_key)
            return self.aggregate_to_5min(candles_1min)
        elif interval == "30min":
            return self.fetch_30min_candles(from_date, to_date, instrument_key)
        elif interval == "day":
            return self.fetch_day_candles(from_date, to_date, instrument_key)
        else:
            raise ValueError(f"Unsupported interval: {interval!r}. Use 1min, 5min, 30min, or day.")

    # ------------------------------------------------------------------
    # Option chain support
    # ------------------------------------------------------------------

    def fetch_option_chain(
        self,
        instrument_key: Optional[str] = None,
        expiry_date: Optional[str] = None,
    ) -> list[OptionData]:
        """Fetch option contracts from the Upstox Option Contracts API.

        Args:
            instrument_key: Underlying instrument key (e.g., "NSE_INDEX|Nifty 50").
            expiry_date: Expiry date string in YYYY-MM-DD format. If None, fetches
                         the nearest expiry.

        Returns:
            List of OptionData objects.
        """
        key = instrument_key or self.instrument_key
        encoded_key = _encode_instrument_key(key)
        url = f"{self.BASE_URL}/option/contract?instrument_key={encoded_key}"
        if expiry_date:
            url += f"&expiry_date={expiry_date}"

        logger.info("Fetching option chain for %s (expiry=%s)", key, expiry_date)

        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Option chain fetch failed: %s", exc)
            return []

        data = response.json()
        if data.get("status") != "success":
            logger.error("Option chain API returned non-success: %s", data)
            return []

        contracts = data.get("data", [])
        options: list[OptionData] = []
        for c in contracts:
            try:
                options.append(OptionData(
                    instrument_key=c.get("instrument_key", ""),
                    underlying=c.get("underlying_key", key),
                    expiry=datetime.fromisoformat(c["expiry"]) if c.get("expiry") else datetime.now(),
                    strike_price=float(c.get("strike_price", 0)),
                    option_type=c.get("option_type", ""),
                    ltp=float(c.get("ltp", 0)),
                    open_interest=int(c.get("open_interest", 0)),
                    volume=int(c.get("volume", 0)),
                ))
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("Skipping malformed option contract: %s", exc)

        logger.info("Fetched %d option contracts", len(options))
        return options

    def fetch_option_candles(
        self,
        instrument_key: str,
        interval: str,
        from_date: date,
        to_date: date,
    ) -> list[Candle]:
        """Fetch historical OHLC candles for an option contract.

        Args:
            instrument_key: The specific option contract instrument key.
            interval: One of "1minute", "3minute", "5minute", "15minute",
                      "30minute", "day".
            from_date: Start date.
            to_date: End date.

        Returns:
            List of Candle objects sorted chronologically.
        """
        return self._fetch_candles_raw(instrument_key, interval, from_date, to_date)

    def fetch_expired_option_candles(
        self,
        instrument_key: str,
        interval: str,
        from_date: date,
        to_date: date,
    ) -> list[Candle]:
        """Fetch historical OHLC candles for an expired option contract.

        Uses the expired-instruments endpoint.

        Args:
            instrument_key: The expired option contract instrument key.
            interval: One of "1minute", "3minute", "5minute", "15minute",
                      "30minute", "day".
            from_date: Start date.
            to_date: End date.

        Returns:
            List of Candle objects sorted chronologically.
        """
        return self._fetch_candles_raw(
            instrument_key, interval, from_date, to_date, expired=True,
        )


# ------------------------------------------------------------------
# CSV persistence (module-level functions)
# ------------------------------------------------------------------

def save_to_csv(candles: list[Candle], filepath: str) -> None:
    """Save Candle objects to a CSV file.

    CSV columns: timestamp, open, high, low, close, volume

    Args:
        candles: List of Candle objects.
        filepath: Path to the output CSV file.
    """
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for candle in candles:
            ts_str = (
                candle.timestamp.isoformat()
                if isinstance(candle.timestamp, datetime)
                else str(candle.timestamp)
            )
            writer.writerow([
                ts_str,
                candle.open,
                candle.high,
                candle.low,
                candle.close,
                candle.volume,
            ])
    logger.info("Saved %d candles to %s", len(candles), filepath)


def load_from_csv(filepath: str) -> list[Candle]:
    """Load Candle objects from a CSV file.

    Expects columns: timestamp, open, high, low, close, volume

    Args:
        filepath: Path to the CSV file.

    Returns:
        List of Candle objects with IST-aware timestamps.
    """
    candles: list[Candle] = []
    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = datetime.fromisoformat(row["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            candles.append(Candle(
                timestamp=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(float(row.get("volume", 0))),
            ))
    candles.sort(key=lambda x: x.timestamp)
    logger.info("Loaded %d candles from %s", len(candles), filepath)
    return candles
