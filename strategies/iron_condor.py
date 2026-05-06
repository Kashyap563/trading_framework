"""Nifty 50 Iron Condor Strategy (weekly expiry).

Sells an OTM call spread + OTM put spread (4 legs) on Nifty 50 weekly options.
Profits when Nifty stays within a range. Uses delta-based strike selection,
VIX proxy filter, and manages positions at 50% of max profit.

Includes 8 layered risk protections:
1. Max total capital at risk (₹1L default)
2. Single position at a time
3. Daily loss limit (₹20K default)
4. Weekly loss limit (₹30K default)
5. Per-trade max loss as % of capital (5% default)
6. Consecutive loss cooldown (3 losses → skip one cycle)
7. No entry in last 2 hours of expiry day
8. Minimum premium filter (₹30/unit default)
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import NamedTuple, Optional
from zoneinfo import ZoneInfo

from trading_framework.base_strategy import BaseStrategy
from trading_framework.models import Candle, Signal, TradeAction

IST = ZoneInfo("Asia/Kolkata")
EOD_CUTOFF = time(15, 15)
EXPIRY_ENTRY_CUTOFF = time(13, 30)
MARKET_OPEN = time(9, 15)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

class OptionRecord(NamedTuple):
    """Lightweight record for a single option contract at a specific minute."""

    timestamp_minute: str       # "2024-08-28T09:15" (truncated for indexing)
    close: float                # Option LTP (close price from CSV)
    strike_price: float         # e.g., 24500.0
    expiry: str                 # "2024-09-05" (date string)
    option_type: str            # "CE" or "PE"
    underlying_close: float     # Nifty 50 spot at this timestamp
    days_to_expiry: float       # e.g., 7.5
    iv: float                   # Implied volatility (annualized)
    delta: float                # Option delta
    instrument_key: str         # e.g., "NSE_FO|58522|03-10-2024"


@dataclass
class SelectedLegs:
    """The 4 chosen strikes for an Iron Condor entry."""

    short_call_strike: float
    short_call_premium: float
    short_call_delta: float
    short_call_instrument_key: str
    long_call_strike: float
    long_call_premium: float
    long_call_delta: float
    long_call_instrument_key: str
    short_put_strike: float
    short_put_premium: float
    short_put_delta: float
    short_put_instrument_key: str
    long_put_strike: float
    long_put_premium: float
    long_put_delta: float
    long_put_instrument_key: str
    expiry: str
    underlying_price: float


@dataclass
class IronCondorPosition:
    """An open Iron Condor position with current state."""

    legs: SelectedLegs
    entry_net_premium: float        # Per unit: (short_call + short_put) - (long_call + long_put)
    max_profit: float               # = entry_net_premium (per unit)
    max_loss: float                 # = spread_width - max_profit (per unit)
    entry_timestamp: datetime
    lot_size: int
    spread_width: int
    # Current premiums (updated each candle)
    current_short_call_premium: float = 0.0
    current_long_call_premium: float = 0.0
    current_short_put_premium: float = 0.0
    current_long_put_premium: float = 0.0


@dataclass
class RiskState:
    """Mutable risk tracking state across trades and days."""

    daily_realized_pnl: float = 0.0
    weekly_realized_pnl: float = 0.0
    consecutive_losses: int = 0
    is_daily_halted: bool = False
    is_weekly_halted: bool = False
    is_cooldown: bool = False
    cooldown_skipped: bool = False
    current_day: Optional[str] = None       # ISO date string
    current_week_start: Optional[str] = None  # Monday ISO date string
    margin_blocked: float = 0.0             # Currently blocked by open positions
    total_realized_pnl: float = 0.0         # Cumulative P&L (affects available capital)


# ---------------------------------------------------------------------------
# Options Data Loader
# ---------------------------------------------------------------------------

def _truncate_timestamp(ts_str: str) -> str:
    """Truncate a timestamp string to minute resolution: 'YYYY-MM-DDTHH:MM'."""
    return ts_str[:16]


class OptionsDataLoader:
    """Load nifty50_options_1min.csv and index for fast lookups.

    Primary index: timestamp_minute → list[OptionRecord]
    Secondary index: (timestamp_minute, expiry, strike, option_type) → OptionRecord
    Expiry index: timestamp_minute → set of expiry date strings
    """

    def __init__(self, csv_path: str) -> None:
        self.csv_path = csv_path
        self._by_timestamp: dict[str, list[OptionRecord]] = {}
        self._by_contract: dict[tuple[str, str, float, str], OptionRecord] = {}
        self._expiries_by_timestamp: dict[str, set[str]] = {}
        self._loaded = False
        self._disabled = False

    def load(self) -> bool:
        """Load and index the CSV. Returns True on success."""
        if not os.path.exists(self.csv_path):
            logger.error("Options CSV not found: %s", self.csv_path)
            self._disabled = True
            return False

        row_count = 0
        skipped = 0

        try:
            with open(self.csv_path, newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    try:
                        ts_raw = row.get("timestamp", "")
                        ts_min = _truncate_timestamp(ts_raw)
                        if len(ts_min) < 16:
                            skipped += 1
                            continue

                        close_val = float(row.get("close", 0))
                        strike = float(row.get("strike_price", 0))
                        expiry = row.get("expiry", "")[:10]
                        opt_type = row.get("option_type", "")
                        underlying = float(row.get("underlying_close", 0))
                        dte = float(row.get("days_to_expiry", 0))
                        iv = float(row.get("iv", 0))
                        delta = float(row.get("delta", 0))
                        ikey = row.get("instrument_key", "")

                        if not opt_type or strike <= 0:
                            skipped += 1
                            continue

                        record = OptionRecord(
                            timestamp_minute=ts_min,
                            close=close_val,
                            strike_price=strike,
                            expiry=expiry,
                            option_type=opt_type,
                            underlying_close=underlying,
                            days_to_expiry=dte,
                            iv=iv,
                            delta=delta,
                            instrument_key=ikey,
                        )

                        # Primary index
                        if ts_min not in self._by_timestamp:
                            self._by_timestamp[ts_min] = []
                        self._by_timestamp[ts_min].append(record)

                        # Secondary index
                        contract_key = (ts_min, expiry, strike, opt_type)
                        self._by_contract[contract_key] = record

                        # Expiry index
                        if ts_min not in self._expiries_by_timestamp:
                            self._expiries_by_timestamp[ts_min] = set()
                        self._expiries_by_timestamp[ts_min].add(expiry)

                        row_count += 1

                    except (ValueError, TypeError) as exc:
                        skipped += 1
                        if skipped <= 10:
                            logger.debug("Skipping malformed row: %s", exc)

        except Exception as exc:
            logger.error("Failed to load options CSV: %s", exc)
            self._disabled = True
            return False

        if row_count == 0:
            logger.error("Options CSV is empty or all rows were invalid")
            self._disabled = True
            return False

        self._loaded = True
        logger.info(
            "Loaded %d option records (%d skipped) from %s | "
            "%d unique timestamps, %d unique contracts",
            row_count, skipped, self.csv_path,
            len(self._by_timestamp), len(self._by_contract),
        )
        return True

    @property
    def is_disabled(self) -> bool:
        return self._disabled

    def get_options_at(
        self, timestamp: datetime, tolerance_seconds: int = 60,
    ) -> list[OptionRecord]:
        """Return all option records at the given timestamp (±tolerance)."""
        if self._disabled:
            return []

        ts_min = _truncate_timestamp(timestamp.isoformat())
        records = self._by_timestamp.get(ts_min, [])
        if records:
            return records

        # Fallback: try ±1 minute
        if tolerance_seconds >= 60:
            for offset in [1, -1]:
                shifted = timestamp + timedelta(minutes=offset)
                ts_shifted = _truncate_timestamp(shifted.isoformat())
                records = self._by_timestamp.get(ts_shifted, [])
                if records:
                    return records

        return []

    def get_option(
        self,
        timestamp: datetime,
        expiry: str,
        strike: float,
        option_type: str,
    ) -> Optional[OptionRecord]:
        """Look up a specific contract at a specific time."""
        if self._disabled:
            return None

        ts_min = _truncate_timestamp(timestamp.isoformat())
        record = self._by_contract.get((ts_min, expiry, strike, option_type))
        if record:
            return record

        # Fallback: ±1 minute
        for offset in [1, -1]:
            shifted = timestamp + timedelta(minutes=offset)
            ts_shifted = _truncate_timestamp(shifted.isoformat())
            record = self._by_contract.get((ts_shifted, expiry, strike, option_type))
            if record:
                return record

        return None

    def get_nearest_expiry(self, timestamp: datetime) -> Optional[str]:
        """Return the nearest future expiry date string from available data."""
        if self._disabled:
            return None

        ts_min = _truncate_timestamp(timestamp.isoformat())
        expiries = self._expiries_by_timestamp.get(ts_min, set())

        if not expiries:
            # Try ±1 minute
            for offset in [1, -1]:
                shifted = timestamp + timedelta(minutes=offset)
                ts_shifted = _truncate_timestamp(shifted.isoformat())
                expiries = self._expiries_by_timestamp.get(ts_shifted, set())
                if expiries:
                    break

        if not expiries:
            return None

        current_date = timestamp.astimezone(IST).date()
        future_expiries = [
            e for e in expiries
            if date.fromisoformat(e) >= current_date
        ]

        if not future_expiries:
            return None

        return min(future_expiries, key=lambda e: date.fromisoformat(e))


# ---------------------------------------------------------------------------
# VIX Filter
# ---------------------------------------------------------------------------

class VIXFilter:
    """Evaluate whether VIX conditions permit entry.

    Uses average ATM implied volatility as a VIX proxy.
    """

    def __init__(self, max_vix: float) -> None:
        self.max_vix = max_vix

    def is_entry_allowed(
        self, options: list[OptionRecord], underlying_close: float,
    ) -> bool:
        """Return True if ATM IV proxy is at or below max_vix.

        ATM = strike nearest to underlying_close.
        VIX proxy = average IV of ATM CE and ATM PE.
        """
        if not options or underlying_close <= 0:
            return False

        # Find the strike nearest to underlying
        unique_strikes = sorted(set(r.strike_price for r in options))
        if not unique_strikes:
            return False

        atm_strike = min(unique_strikes, key=lambda s: abs(s - underlying_close))

        # Get ATM CE and PE IVs
        atm_ivs: list[float] = []
        for r in options:
            if r.strike_price == atm_strike and r.iv > 0:
                atm_ivs.append(r.iv)

        if not atm_ivs:
            return False

        avg_iv = sum(atm_ivs) / len(atm_ivs)
        # IV is annualized (e.g., 0.12 = 12%). Convert to VIX-like scale (multiply by 100).
        vix_proxy = avg_iv * 100.0

        return vix_proxy <= self.max_vix


# ---------------------------------------------------------------------------
# Strike Selector
# ---------------------------------------------------------------------------

class StrikeSelector:
    """Select 4 Iron Condor strikes based on delta values.

    Algorithm:
    1. Filter OTM calls (strike > underlying) for target expiry
    2. Find CE whose |delta| is closest to midpoint of [delta_min, delta_max]
    3. Short call = that CE; Long call = short_call_strike + spread_width
    4. Filter OTM puts (strike < underlying) for target expiry
    5. Find PE whose |delta| is closest to midpoint
    6. Short put = that PE; Long put = short_put_strike - spread_width
    7. Verify all 4 contracts exist
    """

    def __init__(
        self,
        delta_min: float,
        delta_max: float,
        spread_width: int,
    ) -> None:
        self.delta_min = delta_min
        self.delta_max = delta_max
        self.delta_midpoint = (delta_min + delta_max) / 2.0
        self.spread_width = spread_width

    def select(
        self,
        options: list[OptionRecord],
        underlying_close: float,
        expiry: str,
    ) -> Optional[SelectedLegs]:
        """Select the 4 legs. Returns None if valid strikes can't be found."""
        if not options or underlying_close <= 0:
            return None

        # Filter by expiry
        expiry_options = [r for r in options if r.expiry == expiry]
        if not expiry_options:
            logger.debug("No options for expiry %s", expiry)
            return None

        # OTM calls: strike > underlying
        otm_calls = [
            r for r in expiry_options
            if r.option_type == "CE" and r.strike_price > underlying_close and r.close > 0
        ]
        # OTM puts: strike < underlying
        otm_puts = [
            r for r in expiry_options
            if r.option_type == "PE" and r.strike_price < underlying_close and r.close > 0
        ]

        if not otm_calls or not otm_puts:
            logger.debug("Insufficient OTM options: %d calls, %d puts", len(otm_calls), len(otm_puts))
            return None

        # Build sets of available strikes for quick lookup
        ce_strikes = set(r.strike_price for r in expiry_options if r.option_type == "CE" and r.close > 0)
        pe_strikes = set(r.strike_price for r in expiry_options if r.option_type == "PE" and r.close > 0)

        # Sort OTM calls by delta proximity to target
        sorted_calls = sorted(otm_calls, key=lambda r: abs(abs(r.delta) - self.delta_midpoint))
        sorted_puts = sorted(otm_puts, key=lambda r: abs(abs(r.delta) - self.delta_midpoint))

        # Find short call where long call (short + spread_width) also exists
        short_call = None
        long_call = None
        for candidate in sorted_calls:
            long_strike = candidate.strike_price + self.spread_width
            if long_strike in ce_strikes:
                short_call = candidate
                # Find the long call record
                for r in expiry_options:
                    if r.option_type == "CE" and r.strike_price == long_strike and r.close > 0:
                        long_call = r
                        break
                if long_call:
                    break

        if short_call is None or long_call is None:
            logger.warning("No valid call spread found for expiry %s (need short + %d in data)", expiry, self.spread_width)
            return None

        # Find short put where long put (short - spread_width) also exists
        short_put = None
        long_put = None
        for candidate in sorted_puts:
            long_strike = candidate.strike_price - self.spread_width
            if long_strike in pe_strikes:
                short_put = candidate
                for r in expiry_options:
                    if r.option_type == "PE" and r.strike_price == long_strike and r.close > 0:
                        long_put = r
                        break
                if long_put:
                    break

        if short_put is None or long_put is None:
            logger.warning("No valid put spread found for expiry %s (need short - %d in data)", expiry, self.spread_width)
            return None

        return SelectedLegs(
            short_call_strike=short_call.strike_price,
            short_call_premium=short_call.close,
            short_call_delta=short_call.delta,
            short_call_instrument_key=short_call.instrument_key,
            long_call_strike=long_call.strike_price,
            long_call_premium=long_call.close,
            long_call_delta=long_call.delta,
            long_call_instrument_key=long_call.instrument_key,
            short_put_strike=short_put.strike_price,
            short_put_premium=short_put.close,
            short_put_delta=short_put.delta,
            short_put_instrument_key=short_put.instrument_key,
            long_put_strike=long_put.strike_price,
            long_put_premium=long_put.close,
            long_put_delta=long_put.delta,
            long_put_instrument_key=long_put.instrument_key,
            expiry=expiry,
            underlying_price=underlying_close,
        )


# ---------------------------------------------------------------------------
# Position Manager
# ---------------------------------------------------------------------------

class PositionManager:
    """Track the 4-leg Iron Condor as a single composite position."""

    def __init__(self, lot_size: int) -> None:
        self.lot_size = lot_size
        self._position: Optional[IronCondorPosition] = None

    @property
    def is_open(self) -> bool:
        return self._position is not None

    @property
    def position(self) -> Optional[IronCondorPosition]:
        return self._position

    def open_position(
        self, legs: SelectedLegs, timestamp: datetime, spread_width: int,
    ) -> IronCondorPosition:
        """Record a new Iron Condor entry."""
        entry_net = (
            (legs.short_call_premium + legs.short_put_premium)
            - (legs.long_call_premium + legs.long_put_premium)
        )
        pos = IronCondorPosition(
            legs=legs,
            entry_net_premium=entry_net,
            max_profit=entry_net,
            max_loss=spread_width - entry_net,
            entry_timestamp=timestamp,
            lot_size=self.lot_size,
            spread_width=spread_width,
            current_short_call_premium=legs.short_call_premium,
            current_long_call_premium=legs.long_call_premium,
            current_short_put_premium=legs.short_put_premium,
            current_long_put_premium=legs.long_put_premium,
        )
        self._position = pos
        return pos

    def update_premiums(
        self, timestamp: datetime, data_loader: OptionsDataLoader,
    ) -> None:
        """Update current premiums for all 4 legs from latest data."""
        pos = self._position
        if pos is None:
            return

        legs = pos.legs
        expiry = legs.expiry

        for attr_prefix, strike, opt_type in [
            ("current_short_call_premium", legs.short_call_strike, "CE"),
            ("current_long_call_premium", legs.long_call_strike, "CE"),
            ("current_short_put_premium", legs.short_put_strike, "PE"),
            ("current_long_put_premium", legs.long_put_strike, "PE"),
        ]:
            record = data_loader.get_option(timestamp, expiry, strike, opt_type)
            if record and record.close > 0:
                setattr(pos, attr_prefix, record.close)
            else:
                logger.debug(
                    "No data for %s %.0f %s at %s — using last known",
                    opt_type, strike, expiry, timestamp,
                )

    def get_current_net_premium(self) -> float:
        """Current net premium per unit."""
        pos = self._position
        if pos is None:
            return 0.0
        return (
            (pos.current_short_call_premium + pos.current_short_put_premium)
            - (pos.current_long_call_premium + pos.current_long_put_premium)
        )

    def get_current_pnl(self) -> float:
        """Combined P&L in rupees."""
        pos = self._position
        if pos is None:
            return 0.0
        pnl_per_unit = pos.entry_net_premium - self.get_current_net_premium()
        return pnl_per_unit * pos.lot_size

    def get_pnl_pct_of_max_profit(self) -> float:
        """Current P&L as percentage of max_profit."""
        pos = self._position
        if pos is None or pos.max_profit <= 0:
            return 0.0
        pnl_per_unit = pos.entry_net_premium - self.get_current_net_premium()
        return (pnl_per_unit / pos.max_profit) * 100.0

    def close_position(self, reason: str, timestamp: datetime) -> dict:
        """Close the position and return exit metadata."""
        pos = self._position
        if pos is None:
            return {}

        pnl_per_unit = pos.entry_net_premium - self.get_current_net_premium()
        pnl_rupees = pnl_per_unit * pos.lot_size
        pnl_pct = (pnl_per_unit / pos.max_profit * 100.0) if pos.max_profit > 0 else 0.0

        entry_ts = pos.entry_timestamp
        if isinstance(entry_ts, datetime):
            days_held = (timestamp - entry_ts).total_seconds() / 86400.0
        else:
            days_held = 0.0

        metadata = {
            "reason": reason,
            "pnl_pct": round(pnl_pct, 2),
            "pnl_rupees": round(pnl_rupees, 2),
            "days_held": round(days_held, 2),
            "legs": [
                {"strike": pos.legs.short_call_strike, "option_type": "CE",
                 "action": "buy", "premium": pos.current_short_call_premium,
                 "instrument_key": pos.legs.short_call_instrument_key},
                {"strike": pos.legs.long_call_strike, "option_type": "CE",
                 "action": "sell", "premium": pos.current_long_call_premium,
                 "instrument_key": pos.legs.long_call_instrument_key},
                {"strike": pos.legs.short_put_strike, "option_type": "PE",
                 "action": "buy", "premium": pos.current_short_put_premium,
                 "instrument_key": pos.legs.short_put_instrument_key},
                {"strike": pos.legs.long_put_strike, "option_type": "PE",
                 "action": "sell", "premium": pos.current_long_put_premium,
                 "instrument_key": pos.legs.long_put_instrument_key},
            ],
        }

        self._position = None
        return metadata

    def restore_position(self, pos: IronCondorPosition) -> None:
        """Restore a position from persisted state."""
        self._position = pos


# ---------------------------------------------------------------------------
# Risk Protection (8-gate pipeline)
# ---------------------------------------------------------------------------

class RiskProtection:
    """Ordered pipeline of 8 risk protection checks."""

    def __init__(
        self,
        max_total_capital: float,
        max_daily_loss: float,
        max_weekly_loss: float,
        max_loss_pct_of_capital: float,
        max_consecutive_losses: int,
        min_premium_per_unit: float,
        max_capital_per_position: float,
        lot_size: int,
    ) -> None:
        self.max_total_capital = max_total_capital
        self.max_daily_loss = max_daily_loss
        self.max_weekly_loss = max_weekly_loss
        self.max_loss_pct_of_capital = max_loss_pct_of_capital
        self.max_consecutive_losses = max_consecutive_losses
        self.min_premium_per_unit = min_premium_per_unit
        self.max_capital_per_position = max_capital_per_position
        self.lot_size = lot_size
        self.state = RiskState()

    def check_entry_allowed(
        self,
        proposed_max_loss: float,
        net_premium_per_unit: float,
        is_expiry_day: bool,
        current_time_ist: time,
        position_is_open: bool,
        call_spread_margin_per_unit: float = 0.0,
        put_spread_margin_per_unit: float = 0.0,
        actual_num_lots: int = 0,
    ) -> tuple[bool, str]:
        """Run all 8 risk checks in order. Returns (allowed, reason).

        Margin is computed as: max(call_spread_margin, put_spread_margin) × quantity
        Available capital = max_total_capital + total_realized_pnl - margin_blocked
        """

        # Use actual dynamic lot count if provided, else fall back to default
        effective_lots = actual_num_lots if actual_num_lots > 0 else 1
        effective_quantity = effective_lots * self.lot_size

        # Compute estimated margin for this trade
        margin_per_unit = max(call_spread_margin_per_unit, put_spread_margin_per_unit)
        if margin_per_unit <= 0:
            # Fallback: use max_loss as margin estimate
            margin_per_unit = proposed_max_loss
        estimated_margin = margin_per_unit * effective_quantity

        # Available capital = starting capital + cumulative P&L - already blocked margin
        available_capital = (
            self.max_total_capital
            + self.state.total_realized_pnl
            - self.state.margin_blocked
        )

        # 1. Check if margin fits within available capital
        if estimated_margin > available_capital:
            return False, (
                f"Insufficient capital: need ₹{estimated_margin:.0f} margin, "
                f"available ₹{available_capital:.0f} "
                f"(₹{self.max_total_capital:.0f} + P&L ₹{self.state.total_realized_pnl:.0f} "
                f"- blocked ₹{self.state.margin_blocked:.0f})"
            )

        position_risk = proposed_max_loss * effective_quantity

        # 2. Single position at a time
        if position_is_open:
            return False, "Position already open"

        # 3. Daily loss limit
        if self.state.is_daily_halted:
            return False, f"Daily halt: P&L ₹{self.state.daily_realized_pnl:.0f} < -₹{self.max_daily_loss:.0f}"

        # 4. Weekly loss limit
        if self.state.is_weekly_halted:
            return False, f"Weekly halt: P&L ₹{self.state.weekly_realized_pnl:.0f} < -₹{self.max_weekly_loss:.0f}"

        # 5. Per-trade max loss as % of capital
        max_allowed_loss = (self.max_loss_pct_of_capital / 100.0) * self.max_total_capital
        if position_risk > max_allowed_loss:
            return False, (
                f"Per-trade limit: ₹{position_risk:.0f} > "
                f"{self.max_loss_pct_of_capital}% of ₹{self.max_total_capital:.0f} (₹{max_allowed_loss:.0f})"
            )

        # 6. Consecutive loss cooldown
        if self.state.is_cooldown and not self.state.cooldown_skipped:
            self.state.cooldown_skipped = True
            return False, f"Cooldown: {self.state.consecutive_losses} consecutive losses, skipping one cycle"

        # 7. No entry in last 2 hours of expiry day
        if is_expiry_day and current_time_ist >= EXPIRY_ENTRY_CUTOFF:
            return False, f"Expiry day after {EXPIRY_ENTRY_CUTOFF} — no new entries"

        # 8. Minimum premium filter — per unit check
        if net_premium_per_unit < self.min_premium_per_unit:
            return False, (
                f"Premium too low: ₹{net_premium_per_unit:.1f}/unit < ₹{self.min_premium_per_unit:.1f}/unit"
            )

        return True, "OK"

    def on_new_day(self, current_date: date) -> None:
        """Reset daily state."""
        date_str = current_date.isoformat()
        if self.state.current_day != date_str:
            self.state.current_day = date_str
            self.state.daily_realized_pnl = 0.0
            self.state.is_daily_halted = False

    def on_new_week(self, current_date: date) -> None:
        """Reset weekly state when a new expiry cycle starts.

        An expiry cycle resets after the previous expiry day has passed,
        not on calendar Monday.
        """
        # This is now called with the expiry date — resets when expiry changes
        expiry_str = current_date.isoformat()
        if self.state.current_week_start != expiry_str:
            self.state.current_week_start = expiry_str
            self.state.weekly_realized_pnl = 0.0
            self.state.is_weekly_halted = False

    def record_trade_result(self, pnl_rupees: float, margin_released: float = 0.0) -> None:
        """Update P&L tracking and consecutive loss counter after a trade closes."""
        self.state.daily_realized_pnl += pnl_rupees
        self.state.weekly_realized_pnl += pnl_rupees
        self.state.total_realized_pnl += pnl_rupees

        # Release margin blocked by the closed position
        self.state.margin_blocked = max(0.0, self.state.margin_blocked - margin_released)

        if pnl_rupees < 0:
            self.state.consecutive_losses += 1
            if self.state.consecutive_losses >= self.max_consecutive_losses:
                self.state.is_cooldown = True
                self.state.cooldown_skipped = False
        else:
            self.state.consecutive_losses = 0
            self.state.is_cooldown = False
            self.state.cooldown_skipped = False

        # Check daily halt
        if self.state.daily_realized_pnl <= -self.max_daily_loss:
            self.state.is_daily_halted = True
            logger.warning(
                "🛑 DAILY HALT: P&L ₹%.0f exceeded -₹%.0f limit",
                self.state.daily_realized_pnl, self.max_daily_loss,
            )

        # Check weekly halt
        if self.state.weekly_realized_pnl <= -self.max_weekly_loss:
            self.state.is_weekly_halted = True
            logger.warning(
                "🛑 WEEKLY HALT: P&L ₹%.0f exceeded -₹%.0f limit",
                self.state.weekly_realized_pnl, self.max_weekly_loss,
            )

    def should_force_close(self) -> bool:
        """Return True if daily or weekly halt was just triggered."""
        return self.state.is_daily_halted or self.state.is_weekly_halted

    def block_margin(self, margin_amount: float) -> None:
        """Block margin when opening a new position."""
        self.state.margin_blocked += margin_amount

    def get_available_capital(self) -> float:
        """Return current available capital for new trades.

        Capital is capped at max_total_capital — profits don't increase it,
        only losses reduce it.
        """
        effective_capital = min(
            self.max_total_capital,
            self.max_total_capital + self.state.total_realized_pnl,
        )
        return max(0.0, effective_capital - self.state.margin_blocked)


# ---------------------------------------------------------------------------
# Live Options Provider (fetches real-time option chain + VIX from Upstox)
# ---------------------------------------------------------------------------

class LiveOptionsProvider:
    """Fetches live option chain and VIX data from Upstox API.

    Used in paper/sandbox/live modes. Converts Upstox API responses
    into OptionRecord objects compatible with the strategy's internal format.
    """

    VIX_INSTRUMENT_KEY = "NSE_INDEX|India VIX"

    def __init__(self, fetcher) -> None:
        self.fetcher = fetcher
        self._last_vix: float = 0.0
        self._last_option_records: list[OptionRecord] = []
        self._last_fetch_minute: str = ""
        self._cached_expiries: set[str] = set()

    def fetch_option_chain_as_records(
        self,
        timestamp: datetime,
        underlying_close: float,
        expiry_date: Optional[str] = None,
    ) -> list[OptionRecord]:
        """Fetch live option chain and convert to OptionRecord format."""
        ts_min = _truncate_timestamp(timestamp.isoformat())
        if ts_min == self._last_fetch_minute and self._last_option_records:
            return self._last_option_records

        try:
            option_data_list = self.fetcher.fetch_option_chain(
                expiry_date=expiry_date,
            )
        except Exception as exc:
            logger.error("Failed to fetch live option chain: %s", exc)
            return self._last_option_records

        if not option_data_list:
            logger.warning("Empty option chain from API")
            return self._last_option_records

        records: list[OptionRecord] = []
        self._cached_expiries.clear()
        for od in option_data_list:
            try:
                expiry_str = (
                    od.expiry.strftime("%Y-%m-%d")
                    if isinstance(od.expiry, datetime)
                    else str(od.expiry)[:10]
                )
                expiry_dt = date.fromisoformat(expiry_str)
                dte = (expiry_dt - timestamp.astimezone(IST).date()).days
                self._cached_expiries.add(expiry_str)

                records.append(OptionRecord(
                    timestamp_minute=ts_min,
                    close=od.ltp,
                    strike_price=od.strike_price,
                    expiry=expiry_str,
                    option_type=od.option_type,
                    underlying_close=underlying_close,
                    days_to_expiry=float(dte),
                    iv=od.implied_volatility,
                    delta=od.delta,
                    instrument_key=od.instrument_key,
                ))
            except (ValueError, TypeError, AttributeError) as exc:
                logger.debug("Skipping malformed live option: %s", exc)

        if records:
            self._last_option_records = records
            self._last_fetch_minute = ts_min
            logger.info("Fetched %d live option records", len(records))

        return records

    def fetch_vix(self, timestamp: datetime) -> float:
        """Fetch current India VIX value."""
        today = timestamp.astimezone(IST).date()
        try:
            # Use intraday endpoint for today's VIX (historical doesn't have today)
            candles = self.fetcher.fetch_1min_candles(
                today, today, instrument_key=self.VIX_INSTRUMENT_KEY,
            )
            if candles:
                self._last_vix = candles[-1].close
                return self._last_vix
        except Exception as exc:
            logger.warning("Failed to fetch VIX: %s", exc)
        return self._last_vix

    def get_nearest_expiry(self, timestamp: datetime) -> Optional[str]:
        """Return nearest future expiry from cached option chain data."""
        current_date = timestamp.astimezone(IST).date()
        future = [e for e in self._cached_expiries if date.fromisoformat(e) >= current_date]
        return min(future, key=lambda e: date.fromisoformat(e)) if future else None

    def get_option_premium(
        self, instrument_key: str, timestamp: datetime,
    ) -> Optional[float]:
        """Get current premium for a specific option contract."""
        for r in self._last_option_records:
            if r.instrument_key == instrument_key and r.close > 0:
                return r.close
        # Fallback: fetch 1-min candle
        today = timestamp.astimezone(IST).date()
        try:
            candles = self.fetcher.fetch_option_candles(
                instrument_key, "1minute", today, today,
            )
            if candles:
                return candles[-1].close
        except Exception as exc:
            logger.debug("Failed to fetch premium for %s: %s", instrument_key, exc)
        return None


# ---------------------------------------------------------------------------
# Iron Condor Strategy (main class)
# ---------------------------------------------------------------------------

_DEFAULT_OPTIONS_CSV = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "nifty50_options_1min.csv",
)
_DEFAULT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "iron_condor_state.json",
)


class IronCondorStrategy(BaseStrategy):
    """Nifty 50 Iron Condor (weekly expiry).

    Sells an OTM call spread + OTM put spread (4 legs) on Nifty 50 weekly
    options. Profits when Nifty stays within a range. Uses delta-based strike
    selection, VIX proxy filter, and manages positions at 50% of max profit.

    Usage:
        python -m trading_framework.run --strategy iron_condor --mode backtest
        python -m trading_framework.run --strategy iron_condor --mode paper
        python -m trading_framework.run --strategy iron_condor --mode sandbox
        python -m trading_framework.run --strategy iron_condor --mode live
    """

    name = "iron_condor"
    description = "Nifty 50 Iron Condor (weekly expiry)"
    default_instrument = "NSE_INDEX|Nifty 50"
    default_lot_size = 25
    default_candle_interval = "1min"
    requires_option_data = True
    # Brokerage: ₹700 per round-trip (flat fee regardless of lot count)
    # Used for reporting/metrics only — broker deducts actual charges separately
    brokerage_per_trade = 700.0

    def __init__(
        self,
        *,
        max_vix: float = 25.0,
        short_strike_delta_min: float = 0.20,
        short_strike_delta_max: float = 0.25,
        spread_width: int = 50,
        profit_target_pct: float = 50.0,
        stop_loss_multiplier: float = 2.0,
        entry_days_before_expiry_min: int = 0,
        entry_days_before_expiry_max: int = 10,
        max_total_capital: float = 100_000.0,
        max_daily_loss: float = 20_000.0,
        max_weekly_loss: float = 30_000.0,
        max_loss_pct_of_capital: float = 20.0,
        max_consecutive_losses: int = 3,
        min_premium_per_unit: float = 10.0,
        max_capital_per_position: float = 12_000.0,
        options_csv_path: str = _DEFAULT_OPTIONS_CSV,
        state_file_path: str = _DEFAULT_STATE_FILE,
    ) -> None:
        # Validate parameters
        if spread_width <= 0:
            raise ValueError(f"spread_width must be positive, got {spread_width}")
        if not (1 <= profit_target_pct <= 100):
            raise ValueError(f"profit_target_pct must be in [1, 100], got {profit_target_pct}")
        if stop_loss_multiplier <= 0:
            raise ValueError(f"stop_loss_multiplier must be > 0, got {stop_loss_multiplier}")
        if short_strike_delta_min >= short_strike_delta_max:
            raise ValueError(
                f"delta_min ({short_strike_delta_min}) must be < delta_max ({short_strike_delta_max})"
            )
        if max_total_capital <= 0:
            raise ValueError(f"max_total_capital must be > 0, got {max_total_capital}")
        if max_daily_loss <= 0:
            raise ValueError(f"max_daily_loss must be > 0, got {max_daily_loss}")
        if not (1 <= max_loss_pct_of_capital <= 100):
            raise ValueError(f"max_loss_pct_of_capital must be in [1, 100], got {max_loss_pct_of_capital}")

        self.spread_width = spread_width
        self.profit_target_pct = profit_target_pct
        self.stop_loss_multiplier = stop_loss_multiplier
        self.entry_dte_min = entry_days_before_expiry_min
        self.entry_dte_max = entry_days_before_expiry_max
        self.options_csv_path = options_csv_path
        self.state_file_path = state_file_path

        # Components (initialized in on_start)
        self._data_loader: Optional[OptionsDataLoader] = None
        self._vix_filter: Optional[VIXFilter] = None
        self._strike_selector: Optional[StrikeSelector] = None
        self._position_mgr: Optional[PositionManager] = None
        self._risk: Optional[RiskProtection] = None

        # Store params for component creation
        self._max_vix = max_vix
        self._delta_min = short_strike_delta_min
        self._delta_max = short_strike_delta_max
        self._max_total_capital = max_total_capital
        self._max_daily_loss = max_daily_loss
        self._max_weekly_loss = max_weekly_loss
        self._max_loss_pct = max_loss_pct_of_capital
        self._max_consec_losses = max_consecutive_losses
        self._min_premium = min_premium_per_unit
        self._max_cap_per_pos = max_capital_per_position

        self._disabled = False
        self._entry_attempted_this_candle = False

        # Live mode provider (set externally by LiveTrader)
        self._live_provider: Optional[LiveOptionsProvider] = None
        self._is_live_mode = False

    def set_live_provider(self, provider: LiveOptionsProvider) -> None:
        """Set the live options provider for paper/sandbox/live modes.

        Called by LiveTrader when the strategy requires option data.
        """
        self._live_provider = provider
        self._is_live_mode = True

    def on_start(self) -> None:
        """Initialize all components and load data."""
        # In live mode, skip CSV loading — data comes from LiveOptionsProvider
        if self._is_live_mode:
            self._data_loader = None
        else:
            self._data_loader = OptionsDataLoader(self.options_csv_path)
            if not self._data_loader.load():
                self._disabled = True
                logger.error("Iron Condor strategy disabled — options data not available")
                return

        self._vix_filter = VIXFilter(self._max_vix)
        self._strike_selector = StrikeSelector(
            self._delta_min, self._delta_max, self.spread_width,
        )
        self._position_mgr = PositionManager(self.default_lot_size)
        self._risk = RiskProtection(
            max_total_capital=self._max_total_capital,
            max_daily_loss=self._max_daily_loss,
            max_weekly_loss=self._max_weekly_loss,
            max_loss_pct_of_capital=self._max_loss_pct,
            max_consecutive_losses=self._max_consec_losses,
            min_premium_per_unit=self._min_premium,
            max_capital_per_position=self._max_cap_per_pos,
            lot_size=self.default_lot_size,
        )

        # Restore persisted state if available (live/paper/sandbox only)
        if self._is_live_mode:
            self._load_state()

        logger.info(
            "🔷 Iron Condor strategy initialized | "
            "VIX<%s | Delta %.2f-%.2f | Spread %d | "
            "Profit target %d%% | Stop-loss %dx | "
            "Capital ₹%.0f | Daily limit ₹%.0f",
            self._max_vix, self._delta_min, self._delta_max,
            self.spread_width, self.profit_target_pct,
            self.stop_loss_multiplier, self._max_total_capital,
            self._max_daily_loss,
        )

    def on_candle(self, candle: Candle) -> Optional[TradeAction]:
        """Process each candle — manage open position or evaluate entry."""
        if self._disabled:
            return None

        ts = candle.timestamp
        ist_ts = ts.astimezone(IST)
        current_date = ist_ts.date()
        current_time = ist_ts.time()

        # Day/week boundary resets
        self._risk.on_new_day(current_date)

        # Expiry-cycle reset: detect when a new expiry cycle starts
        if self._is_live_mode and self._live_provider:
            nearest_expiry = self._live_provider.get_nearest_expiry(ts)
        elif self._data_loader:
            nearest_expiry = self._data_loader.get_nearest_expiry(ts)
        else:
            nearest_expiry = None

        if nearest_expiry:
            self._risk.on_new_week(date.fromisoformat(nearest_expiry))

        # --- EXIT PATH: manage open position ---
        if self._position_mgr.is_open:
            return self._evaluate_exit(candle, ts, ist_ts, current_date, current_time)

        # --- ENTRY PATH: evaluate new position ---
        return self._evaluate_entry(candle, ts, ist_ts, current_date, current_time)

    def _evaluate_exit(
        self, candle: Candle, ts: datetime, ist_ts: datetime,
        current_date: date, current_time: time,
    ) -> Optional[TradeAction]:
        """Check exit conditions for the open position."""
        pos = self._position_mgr.position
        if pos is None:
            return None

        # Update leg premiums — from CSV (backtest) or live API
        if self._is_live_mode and self._live_provider:
            self._update_premiums_live(ts)
        else:
            self._position_mgr.update_premiums(ts, self._data_loader)

        pnl_pct = self._position_mgr.get_pnl_pct_of_max_profit()
        pnl_rupees = self._position_mgr.get_current_pnl()

        logger.debug(
            "📈 Position monitor | Nifty=%.2f | P&L=%.1f%% (₹%.0f) | "
            "Target=%d%% | Expiry=%s | Time=%s",
            candle.close, pnl_pct, pnl_rupees,
            self.profit_target_pct, pos.legs.expiry,
            current_time.strftime("%H:%M"),
        )

        # 1. Profit target
        if pnl_pct >= self.profit_target_pct:
            return self._close_position("profit_target", ts, candle.close)

        # 2. Stop-loss: trigger when loss exceeds multiplier × max_profit
        loss_threshold = pos.max_profit * self.stop_loss_multiplier
        pnl_per_unit = pos.entry_net_premium - self._position_mgr.get_current_net_premium()
        if pnl_per_unit <= -loss_threshold:
            return self._close_position("stop_loss", ts, candle.close)

        # 3. Expiry day EOD
        expiry_date = date.fromisoformat(pos.legs.expiry)
        if current_date == expiry_date and current_time >= EOD_CUTOFF:
            return self._close_position("expiry_eod", ts, candle.close)

        # 4. Force close on daily/weekly halt
        if self._risk.should_force_close():
            reason = "daily_loss_limit" if self._risk.state.is_daily_halted else "weekly_loss_limit"
            return self._close_position(reason, ts, candle.close)

        return None

    def _evaluate_entry(
        self, candle: Candle, ts: datetime, ist_ts: datetime,
        current_date: date, current_time: time,
    ) -> Optional[TradeAction]:
        """Check entry conditions and open a new position if all gates pass.

        Evaluates ALL available expiries within the DTE range, scores each
        candidate by (premium/max_loss) × (premium/DTE), and picks the best.
        """
        if current_time < MARKET_OPEN:
            return None

        # Get options data — from CSV (backtest) or live API (paper/sandbox/live)
        underlying_close = candle.close

        if self._is_live_mode and self._live_provider:
            options = self._live_provider.fetch_option_chain_as_records(
                ts, underlying_close,
            )

            # VIX check using live India VIX
            live_vix = self._live_provider.fetch_vix(ts)
            logger.info("📊 VIX: %.2f | Max allowed: %.1f | Nifty: %.2f", live_vix, self._max_vix, underlying_close)
            if live_vix > 0 and live_vix > self._max_vix:
                logger.info("⛔ VIX %.1f > %.1f — skipping entry", live_vix, self._max_vix)
                return None

            # Get all available expiries from cached option chain
            all_expiries = sorted(self._live_provider._cached_expiries)
        else:
            options = self._data_loader.get_options_at(ts) if self._data_loader else []

            if options and options[0].underlying_close > 0:
                underlying_close = options[0].underlying_close

            # Get all expiries available at this timestamp
            if self._data_loader:
                ts_min = _truncate_timestamp(ts.isoformat())
                all_expiries = sorted(
                    self._data_loader._expiries_by_timestamp.get(ts_min, set())
                )
            else:
                all_expiries = []

        if not options:
            return None

        if not all_expiries:
            return None

        # VIX filter (backtest mode — uses ATM IV proxy)
        if not self._is_live_mode:
            if not self._vix_filter.is_entry_allowed(options, underlying_close):
                return None

        # --- Evaluate all expiries within DTE range ---
        candidates = []
        for expiry_str in all_expiries:
            expiry_date = date.fromisoformat(expiry_str)
            dte = (expiry_date - current_date).days
            if not (self.entry_dte_min <= dte <= self.entry_dte_max):
                continue

            # Try strike selection for this expiry
            legs = self._strike_selector.select(options, underlying_close, expiry_str)
            if legs is None:
                continue

            # Compute net premium and max loss
            net_premium = (
                (legs.short_call_premium + legs.short_put_premium)
                - (legs.long_call_premium + legs.long_put_premium)
            )
            max_loss_per_unit = self.spread_width - net_premium

            if net_premium <= 0 or max_loss_per_unit <= 0:
                continue

            # Check margin fits within available capital
            call_spread_net = legs.short_call_premium - legs.long_call_premium
            put_spread_net = legs.short_put_premium - legs.long_put_premium
            call_spread_margin = self.spread_width - call_spread_net
            put_spread_margin = self.spread_width - put_spread_net
            estimated_margin_per_unit = max(call_spread_margin, put_spread_margin)
            margin_per_lot = estimated_margin_per_unit * self.default_lot_size

            available = self._risk.get_available_capital()
            if margin_per_lot > available:
                continue

            # Score: (premium/max_loss) × (premium/DTE) — higher = quicker profit, lower risk
            reward_risk = net_premium / max_loss_per_unit
            daily_theta = net_premium / max(dte, 1)
            score = reward_risk * daily_theta

            candidates.append({
                "expiry": expiry_str,
                "dte": dte,
                "legs": legs,
                "net_premium": net_premium,
                "max_loss_per_unit": max_loss_per_unit,
                "margin_per_unit": estimated_margin_per_unit,
                "score": score,
                "reward_risk": reward_risk,
                "daily_theta": daily_theta,
            })

        if not candidates:
            return None

        # Log all candidates
        candidates.sort(key=lambda c: c["score"], reverse=True)
        logger.info(
            "📋 %d expiry candidates found | Best: %s (DTE=%d, score=%.3f) | "
            "All: %s",
            len(candidates),
            candidates[0]["expiry"], candidates[0]["dte"], candidates[0]["score"],
            ", ".join(
                f"{c['expiry']}(DTE={c['dte']},₹{c['net_premium']:.1f},score={c['score']:.3f})"
                for c in candidates
            ),
        )

        # Pick the best candidate
        best = candidates[0]
        legs = best["legs"]
        net_premium = best["net_premium"]
        max_loss_per_unit = best["max_loss_per_unit"]
        nearest_expiry = best["expiry"]
        dte = best["dte"]
        estimated_margin_per_unit = best["margin_per_unit"]

        # --- Dynamic lot sizing ---
        daily_loss_so_far = abs(min(0.0, self._risk.state.daily_realized_pnl))
        remaining_daily_budget = max(0.0, self._max_daily_loss - daily_loss_so_far)

        if remaining_daily_budget <= 0:
            logger.debug("No daily loss budget remaining")
            return None

        max_loss_per_lot = max_loss_per_unit * self.default_lot_size

        # Constraint 1: max loss for this trade ≤ remaining daily budget
        lots_by_risk = int(remaining_daily_budget / max_loss_per_lot) if max_loss_per_lot > 0 else 0

        # Constraint 2: margin must fit within available capital
        margin_per_lot = estimated_margin_per_unit * self.default_lot_size
        available = self._risk.get_available_capital()
        if self._is_live_mode and self._live_provider:
            try:
                upstox_available = self._live_provider.fetcher.fetch_available_margin()
                if upstox_available > 0:
                    available = min(available, upstox_available)
                    logger.info(
                        "💰 Capital: internal=₹%.0f | Upstox=₹%.0f | Using=₹%.0f",
                        self._risk.get_available_capital(), upstox_available, available,
                    )
            except Exception as exc:
                logger.warning("Could not fetch Upstox margin: %s — using internal", exc)

        lots_by_capital = int(available / margin_per_lot) if margin_per_lot > 0 else 0

        # Take the minimum, at least 1 lot
        num_lots = min(lots_by_risk, lots_by_capital)
        if num_lots < 1:
            logger.debug(
                "Cannot size position: lots_by_risk=%d (budget ₹%.0f), "
                "lots_by_capital=%d (available ₹%.0f), margin/lot ₹%.0f",
                lots_by_risk, remaining_daily_budget,
                lots_by_capital, available, margin_per_lot,
            )
            return None

        total_quantity = num_lots * self.default_lot_size
        estimated_margin_total = margin_per_lot * num_lots

        # Brokerage profitability check
        profit_at_target = net_premium * (self.profit_target_pct / 100.0) * total_quantity
        if profit_at_target <= self.brokerage_per_trade:
            logger.debug(
                "Not profitable after brokerage: target profit ₹%.0f "
                "≤ brokerage ₹%.0f on %d units — skipping",
                profit_at_target, self.brokerage_per_trade, total_quantity,
            )
            return None

        # Check if it's expiry day
        expiry_date = date.fromisoformat(nearest_expiry)
        is_expiry_day = (current_date == expiry_date)

        # Run risk gates
        call_spread_margin = self.spread_width - (legs.short_call_premium - legs.long_call_premium)
        put_spread_margin = self.spread_width - (legs.short_put_premium - legs.long_put_premium)

        allowed, reason = self._risk.check_entry_allowed(
            proposed_max_loss=max_loss_per_unit,
            net_premium_per_unit=net_premium,
            is_expiry_day=is_expiry_day,
            current_time_ist=current_time,
            position_is_open=self._position_mgr.is_open,
            call_spread_margin_per_unit=call_spread_margin,
            put_spread_margin_per_unit=put_spread_margin,
            actual_num_lots=num_lots,
        )

        if not allowed:
            logger.debug("Entry blocked: %s", reason)
            return None

        # Open position
        pos = self._position_mgr.open_position(legs, ts, self.spread_width)
        pos.lot_size = total_quantity

        # Block margin
        self._risk.block_margin(estimated_margin_total)

        # Save state (live/paper/sandbox only)
        if self._is_live_mode:
            self._save_state()

        logger.info(
            "🔷 IRON CONDOR ENTRY | Expiry %s (DTE %d) | %d lots (%d units) | "
            "Call: Sell %.0f/Buy %.0f | Put: Sell %.0f/Buy %.0f | "
            "Premium ₹%.1f/unit (₹%.0f total) | Max loss ₹%.0f | "
            "Margin ₹%.0f | Score %.3f (R/R=%.2f, θ/day=%.2f) | "
            "Picked from %d candidates",
            nearest_expiry, dte, num_lots, total_quantity,
            legs.short_call_strike, legs.long_call_strike,
            legs.short_put_strike, legs.long_put_strike,
            net_premium, net_premium * total_quantity,
            max_loss_per_unit * total_quantity,
            estimated_margin_total,
            best["score"], best["reward_risk"], best["daily_theta"],
            len(candidates),
        )

        # Build entry metadata
        metadata = {
            "legs": [
                {"strike": legs.short_call_strike, "option_type": "CE", "action": "sell",
                 "premium": legs.short_call_premium, "delta": legs.short_call_delta,
                 "instrument_key": legs.short_call_instrument_key},
                {"strike": legs.long_call_strike, "option_type": "CE", "action": "buy",
                 "premium": legs.long_call_premium, "delta": legs.long_call_delta,
                 "instrument_key": legs.long_call_instrument_key},
                {"strike": legs.short_put_strike, "option_type": "PE", "action": "sell",
                 "premium": legs.short_put_premium, "delta": legs.short_put_delta,
                 "instrument_key": legs.short_put_instrument_key},
                {"strike": legs.long_put_strike, "option_type": "PE", "action": "buy",
                 "premium": legs.long_put_premium, "delta": legs.long_put_delta,
                 "instrument_key": legs.long_put_instrument_key},
            ],
            "expiry": nearest_expiry,
            "max_profit": pos.max_profit,
            "max_loss": pos.max_loss,
            "spread_width": self.spread_width,
            "underlying_price": underlying_close,
        }

        return TradeAction(
            signal=Signal.SELL,
            price=net_premium,
            timestamp=ts,
            instrument=self.default_instrument,
            quantity=total_quantity,
            metadata=metadata,
        )

    def _update_premiums_live(self, ts: datetime) -> None:
        """Update leg premiums from live API data."""
        pos = self._position_mgr.position
        if pos is None or self._live_provider is None:
            return

        # Refresh option chain each candle to get current premiums
        self._live_provider.fetch_option_chain_as_records(
            ts, pos.legs.underlying_price,
        )

        legs = pos.legs
        for attr, ikey in [
            ("current_short_call_premium", legs.short_call_instrument_key),
            ("current_long_call_premium", legs.long_call_instrument_key),
            ("current_short_put_premium", legs.short_put_instrument_key),
            ("current_long_put_premium", legs.long_put_instrument_key),
        ]:
            premium = self._live_provider.get_option_premium(ikey, ts)
            if premium is not None and premium > 0:
                setattr(pos, attr, premium)
            else:
                logger.debug("No live premium for %s — using last known", ikey)

    def _close_position(
        self, reason: str, ts: datetime, underlying_price: float,
    ) -> TradeAction:
        """Close the open position and return an EXIT TradeAction."""
        # Record result in risk tracking (release margin)
        pos = self._position_mgr.position
        margin_to_release = 0.0
        if pos:
            call_net = pos.legs.short_call_premium - pos.legs.long_call_premium
            put_net = pos.legs.short_put_premium - pos.legs.long_put_premium
            margin_per_unit = max(pos.spread_width - call_net, pos.spread_width - put_net)
            margin_to_release = margin_per_unit * pos.lot_size

        pnl_rupees = self._position_mgr.get_current_pnl()
        current_net = self._position_mgr.get_current_net_premium()
        metadata = self._position_mgr.close_position(reason, ts)

        self._risk.record_trade_result(pnl_rupees, margin_released=margin_to_release)

        # Save state after exit (live/paper/sandbox only)
        if self._is_live_mode:
            self._save_state()

        emoji = "✅" if pnl_rupees > 0 else "❌"
        logger.info(
            "%s IRON CONDOR EXIT [%s] | P&L ₹%.0f (%s%% of max) | Days held %.1f",
            emoji, reason, pnl_rupees,
            metadata.get("pnl_pct", 0), metadata.get("days_held", 0),
        )

        return TradeAction(
            signal=Signal.EXIT,
            price=current_net,
            timestamp=ts,
            instrument=self.default_instrument,
            quantity=pos.lot_size if pos else self.default_lot_size,
            metadata=metadata,
        )

    def on_end(self) -> None:
        """Save state when strategy ends (market close or backtest complete)."""
        # Only persist state in live/paper/sandbox — backtest must not touch state file
        if self._is_live_mode:
            self._save_state()
        if self._risk:
            state = self._risk.state
            logger.info(
                "🔷 Iron Condor session end | "
                "Daily P&L ₹%.0f | Weekly P&L ₹%.0f | "
                "Consecutive losses: %d | Position open: %s",
                state.daily_realized_pnl, state.weekly_realized_pnl,
                state.consecutive_losses,
                "yes" if self._position_mgr and self._position_mgr.is_open else "no",
            )

    def get_position(self) -> str:
        if self._position_mgr and self._position_mgr.is_open:
            return "short"
        return "flat"

    # --- State Persistence ---

    def _save_state(self) -> None:
        """Save strategy state to JSON for multi-day survival."""
        if not self._risk or not self._position_mgr:
            return

        state_data: dict = {
            "risk_state": asdict(self._risk.state) if self._risk else {},
            "position": None,
        }

        pos = self._position_mgr.position
        if pos is not None:
            state_data["position"] = {
                "legs": asdict(pos.legs),
                "entry_net_premium": pos.entry_net_premium,
                "max_profit": pos.max_profit,
                "max_loss": pos.max_loss,
                "entry_timestamp": pos.entry_timestamp.isoformat(),
                "lot_size": pos.lot_size,
                "spread_width": pos.spread_width,
                "current_short_call_premium": pos.current_short_call_premium,
                "current_long_call_premium": pos.current_long_call_premium,
                "current_short_put_premium": pos.current_short_put_premium,
                "current_long_put_premium": pos.current_long_put_premium,
            }

        try:
            with open(self.state_file_path, "w") as f:
                json.dump(state_data, f, indent=2)
        except Exception as exc:
            logger.warning("Failed to save state: %s", exc)

    def _load_state(self) -> None:
        """Load strategy state from JSON for multi-day resume."""
        if not os.path.exists(self.state_file_path):
            return

        try:
            with open(self.state_file_path) as f:
                state_data = json.load(f)
        except Exception as exc:
            logger.warning("Failed to load state: %s", exc)
            return

        # Restore risk state
        risk_data = state_data.get("risk_state", {})
        if risk_data and self._risk:
            rs = self._risk.state
            rs.daily_realized_pnl = risk_data.get("daily_realized_pnl", 0.0)
            rs.weekly_realized_pnl = risk_data.get("weekly_realized_pnl", 0.0)
            rs.consecutive_losses = risk_data.get("consecutive_losses", 0)
            rs.is_daily_halted = risk_data.get("is_daily_halted", False)
            rs.is_weekly_halted = risk_data.get("is_weekly_halted", False)
            rs.is_cooldown = risk_data.get("is_cooldown", False)
            rs.cooldown_skipped = risk_data.get("cooldown_skipped", False)
            rs.current_day = risk_data.get("current_day")
            rs.current_week_start = risk_data.get("current_week_start")
            rs.margin_blocked = risk_data.get("margin_blocked", 0.0)
            rs.total_realized_pnl = risk_data.get("total_realized_pnl", 0.0)

        # Restore open position
        pos_data = state_data.get("position")
        if pos_data and self._position_mgr:
            legs_data = pos_data["legs"]
            legs = SelectedLegs(**legs_data)
            pos = IronCondorPosition(
                legs=legs,
                entry_net_premium=pos_data["entry_net_premium"],
                max_profit=pos_data["max_profit"],
                max_loss=pos_data["max_loss"],
                entry_timestamp=datetime.fromisoformat(pos_data["entry_timestamp"]),
                lot_size=pos_data["lot_size"],
                spread_width=pos_data["spread_width"],
                current_short_call_premium=pos_data.get("current_short_call_premium", 0),
                current_long_call_premium=pos_data.get("current_long_call_premium", 0),
                current_short_put_premium=pos_data.get("current_short_put_premium", 0),
                current_long_put_premium=pos_data.get("current_long_put_premium", 0),
            )
            self._position_mgr.restore_position(pos)
            logger.info(
                "🔷 Restored open position: %s expiry, entered at %s",
                legs.expiry, pos_data["entry_timestamp"],
            )


# ---------------------------------------------------------------------------
# Iron Condor Backtest Metrics Helper
# ---------------------------------------------------------------------------

def compute_iron_condor_metrics(trades: list) -> dict:
    """Extract Iron Condor-specific metrics from Trade.metadata.

    Args:
        trades: List of Trade objects from BacktestResult.

    Returns:
        Dict with IC-specific metrics for reporting.
    """
    if not trades:
        return {}

    premiums: list[float] = []
    days_held: list[float] = []
    exit_reasons: dict[str, int] = {}

    for trade in trades:
        meta = getattr(trade, "metadata", {}) or {}

        # Entry premium from entry metadata
        if "max_profit" in meta:
            premiums.append(meta["max_profit"])

        # Exit info
        reason = meta.get("reason", "unknown")
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

        dh = meta.get("days_held", 0)
        if dh > 0:
            days_held.append(dh)

    total = len(trades)
    metrics = {
        "total_trades": total,
        "avg_premium_per_unit": sum(premiums) / len(premiums) if premiums else 0,
        "avg_days_held": sum(days_held) / len(days_held) if days_held else 0,
        "exit_reasons": exit_reasons,
    }

    for reason, count in exit_reasons.items():
        metrics[f"pct_{reason}"] = (count / total * 100) if total > 0 else 0

    return metrics
