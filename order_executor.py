"""Order executors for paper, sandbox, and live trading modes.

Paper mode: logs trades locally, no API calls.
Sandbox mode: uses Upstox sandbox API (no real money).
Live mode: uses Upstox live API (DANGEROUS — requires explicit confirmation).

All executors work with the framework's TradeAction and Trade models.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import asdict
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from trading_framework.models import Signal, Trade, TradeAction

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

DEFAULT_BROKERAGE = 500.0  # ₹500 per round-trip trade


class OrderExecutorBase(ABC):
    """Abstract base for order execution."""

    def __init__(self, lot_size: int = 25, brokerage: float = DEFAULT_BROKERAGE) -> None:
        self.lot_size = lot_size
        self.brokerage = brokerage
        self.trades: list[Trade] = []
        self._trade_counter = 0
        self._open_trade: Trade | None = None

    @abstractmethod
    def execute(self, action: TradeAction) -> Trade | None:
        """Execute a trade action. Returns the Trade record or None."""
        ...

    def get_open_trade(self) -> Trade | None:
        """Return the currently open trade, if any."""
        return self._open_trade

    def get_summary(self) -> dict:
        """Return a summary of all closed trades."""
        closed = [t for t in self.trades if t.exit_time is not None]
        total_pnl = sum(t.pnl_rupees for t in closed)
        total_brokerage = sum(t.brokerage for t in closed)
        net_pnl = total_pnl - total_brokerage
        wins = [t for t in closed if t.pnl_points > 0]
        losses = [t for t in closed if t.pnl_points < 0]
        return {
            "total_trades": len(closed),
            "winning": len(wins),
            "losing": len(losses),
            "win_rate": (len(wins) / len(closed) * 100) if closed else 0,
            "total_pnl_rupees": total_pnl,
            "total_brokerage": total_brokerage,
            "net_pnl_rupees": net_pnl,
        }

    def _open_new_trade(self, action: TradeAction) -> Trade:
        """Create and record a new open trade from a TradeAction."""
        self._trade_counter += 1
        qty = action.quantity if action.quantity > 0 else self.lot_size
        direction = "long" if action.signal == Signal.BUY else "short"
        trade = Trade(
            trade_id=self._trade_counter,
            entry_time=action.timestamp,
            entry_price=action.price,
            direction=direction,
            instrument=action.instrument,
            quantity=qty,
            brokerage=self.brokerage,
            metadata=dict(action.metadata),
        )
        self.trades.append(trade)
        self._open_trade = trade
        return trade

    def _close_current_trade(self, action: TradeAction) -> Trade | None:
        """Close the currently open trade using the given TradeAction."""
        trade = self._open_trade
        if trade is None:
            logger.warning("No open trade to close")
            return None

        trade.exit_time = action.timestamp
        trade.exit_price = action.price
        trade.exit_reason = action.metadata.get("reason", "signal")

        if trade.direction == "long":
            trade.pnl_points = action.price - trade.entry_price
        else:
            trade.pnl_points = trade.entry_price - action.price

        trade.pnl_rupees = trade.pnl_points * trade.quantity
        trade.net_pnl = trade.pnl_rupees - trade.brokerage

        # Merge exit metadata (reason, days_held, pnl_pct) into trade metadata
        if trade.metadata is None:
            trade.metadata = {}
        trade.metadata["reason"] = trade.exit_reason
        if trade.entry_time and trade.exit_time:
            days = (trade.exit_time - trade.entry_time).total_seconds() / 86400.0
            trade.metadata["days_held"] = round(days, 2)
        # Merge any extra exit metadata from the action
        for key in ("pnl_pct", "pnl_rupees"):
            if key in action.metadata and key not in trade.metadata:
                trade.metadata[key] = action.metadata[key]

        self._open_trade = None
        return trade


class PaperOrderExecutor(OrderExecutorBase):
    """Paper trading — logs trades locally, no API calls."""

    def __init__(
        self,
        lot_size: int = 25,
        brokerage: float = DEFAULT_BROKERAGE,
        trade_log_path: str = "paper_trades.json",
    ) -> None:
        super().__init__(lot_size, brokerage)
        self.trade_log_path = trade_log_path
        self._load_existing_trades()

    def _load_existing_trades(self) -> None:
        if os.path.exists(self.trade_log_path):
            try:
                with open(self.trade_log_path) as f:
                    data = json.load(f)
                    for t in data:
                        # Convert ISO strings back to datetime
                        for key in ("entry_time", "exit_time"):
                            if t.get(key) and isinstance(t[key], str):
                                t[key] = datetime.fromisoformat(t[key])
                        self.trades.append(Trade(**t))
                    if self.trades:
                        self._trade_counter = max(t.trade_id for t in self.trades)
                    logger.info("Loaded %d existing paper trades", len(self.trades))
            except Exception as e:
                logger.warning("Could not load existing trades: %s", e)

    def _save_trades(self) -> None:
        serializable = []
        for t in self.trades:
            d = asdict(t)
            for key in ("entry_time", "exit_time"):
                if isinstance(d.get(key), datetime):
                    d[key] = d[key].isoformat()
            serializable.append(d)
        with open(self.trade_log_path, "w") as f:
            json.dump(serializable, f, indent=2)

    def execute(self, action: TradeAction) -> Trade | None:
        signal = action.signal

        if signal in (Signal.BUY, Signal.SELL):
            if self._open_trade is not None:
                logger.warning("Already in a trade, ignoring %s", signal)
                return None
            trade = self._open_new_trade(action)
            self._save_trades()
            logger.info(
                "📝 PAPER %s #%d: price=%.2f at %s",
                signal.value.upper(), trade.trade_id, action.price,
                action.timestamp.astimezone(IST).isoformat(),
            )
            return trade

        elif signal in (Signal.EXIT, Signal.EXIT_LONG, Signal.EXIT_SHORT):
            trade = self._close_current_trade(action)
            if trade:
                self._save_trades()
                emoji = "✅" if trade.pnl_points > 0 else "❌"
                logger.info(
                    "%s PAPER EXIT #%d: price=%.2f, P&L=%.2f pts (₹%.2f), net=₹%.2f [%s]",
                    emoji, trade.trade_id, action.price, trade.pnl_points,
                    trade.pnl_rupees, trade.net_pnl, trade.exit_reason,
                )
            return trade

        return None


class SandboxOrderExecutor(OrderExecutorBase):
    """Sandbox trading — uses Upstox sandbox API. No real money."""

    def __init__(
        self,
        access_token: str,
        instrument_token: str,
        lot_size: int = 25,
        brokerage: float = DEFAULT_BROKERAGE,
        trade_log_path: str = "sandbox_trades.json",
    ) -> None:
        super().__init__(lot_size, brokerage)
        self.access_token = access_token
        self.instrument_token = instrument_token
        self.trade_log_path = trade_log_path
        self.base_url = "https://api.upstox.com/v2/order/place"
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        })
        self._load_existing_trades()

    def _load_existing_trades(self) -> None:
        if os.path.exists(self.trade_log_path):
            try:
                with open(self.trade_log_path) as f:
                    data = json.load(f)
                    for t in data:
                        for key in ("entry_time", "exit_time"):
                            if t.get(key) and isinstance(t[key], str):
                                t[key] = datetime.fromisoformat(t[key])
                        self.trades.append(Trade(**t))
                    if self.trades:
                        self._trade_counter = max(t.trade_id for t in self.trades)
                    logger.info("Loaded %d existing sandbox trades", len(self.trades))
            except Exception as e:
                logger.warning("Could not load existing sandbox trades: %s", e)

    def _save_trades(self) -> None:
        serializable = []
        for t in self.trades:
            d = asdict(t)
            for key in ("entry_time", "exit_time"):
                if isinstance(d.get(key), datetime):
                    d[key] = d[key].isoformat()
            serializable.append(d)
        with open(self.trade_log_path, "w") as f:
            json.dump(serializable, f, indent=2)

    def _place_upstox_order(self, transaction_type: str, price: float) -> dict:
        payload = {
            "quantity": self.lot_size,
            "product": "I",
            "validity": "DAY",
            "price": 0,
            "instrument_token": self.instrument_token,
            "order_type": "MARKET",
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        try:
            response = self.session.post(self.base_url, json=payload, timeout=10)
            result = response.json()
            logger.info("Upstox sandbox order response: %s", result)
            return result
        except Exception as e:
            logger.error("Sandbox order failed: %s", e)
            return {"status": "error", "message": str(e)}

    def execute(self, action: TradeAction) -> Trade | None:
        signal = action.signal

        if signal in (Signal.BUY, Signal.SELL):
            if self._open_trade is not None:
                return None
            tx_type = "BUY" if signal == Signal.BUY else "SELL"
            self._place_upstox_order(tx_type, action.price)
            trade = self._open_new_trade(action)
            self._save_trades()
            logger.info("🔵 SANDBOX %s #%d: price=%.2f", tx_type, trade.trade_id, action.price)
            return trade

        elif signal in (Signal.EXIT, Signal.EXIT_LONG, Signal.EXIT_SHORT):
            if self._open_trade is None:
                return None
            tx_type = "SELL" if self._open_trade.direction == "long" else "BUY"
            self._place_upstox_order(tx_type, action.price)
            trade = self._close_current_trade(action)
            if trade:
                self._save_trades()
                emoji = "✅" if trade.pnl_points > 0 else "❌"
                logger.info(
                    "%s SANDBOX EXIT #%d: P&L=%.2f pts (₹%.2f), net=₹%.2f [%s]",
                    emoji, trade.trade_id, trade.pnl_points,
                    trade.pnl_rupees, trade.net_pnl, trade.exit_reason,
                )
            return trade

        return None


class LiveOrderExecutor(OrderExecutorBase):
    """LIVE trading — places REAL orders with REAL money via Upstox API.

    SAFETY: Requires confirm_live=True to instantiate.
    """

    def __init__(
        self,
        access_token: str,
        instrument_token: str,
        confirm_live: bool = False,
        lot_size: int = 25,
        brokerage: float = DEFAULT_BROKERAGE,
        trade_log_path: str = "live_trades.json",
    ) -> None:
        if not confirm_live:
            raise RuntimeError(
                "SAFETY LOCK: Live trading requires confirm_live=True. "
                "This will place REAL orders with REAL money."
            )
        super().__init__(lot_size, brokerage)
        self.access_token = access_token
        self.instrument_token = instrument_token
        self.trade_log_path = trade_log_path
        self.base_url = "https://api.upstox.com/v2/order/place"
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        })

        logger.warning("=" * 60)
        logger.warning("⚠️  LIVE TRADING MODE ACTIVE — REAL MONEY AT RISK")
        logger.warning("⚠️  Instrument: %s | Lot size: %d", instrument_token, lot_size)
        logger.warning("=" * 60)

    def _place_upstox_order(self, transaction_type: str, price: float) -> dict:
        payload = {
            "quantity": self.lot_size,
            "product": "I",
            "validity": "DAY",
            "price": 0,
            "instrument_token": self.instrument_token,
            "order_type": "MARKET",
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        try:
            logger.warning(
                "🔴 LIVE ORDER: %s %d x %s @ market (≈%.2f)",
                transaction_type, self.lot_size, self.instrument_token, price,
            )
            response = self.session.post(self.base_url, json=payload, timeout=10)
            result = response.json()
            logger.info("Upstox live order response: %s", result)
            return result
        except Exception as e:
            logger.error("Live order failed: %s", e)
            return {"status": "error", "message": str(e)}

    def _save_trades(self) -> None:
        serializable = []
        for t in self.trades:
            d = asdict(t)
            for key in ("entry_time", "exit_time"):
                if isinstance(d.get(key), datetime):
                    d[key] = d[key].isoformat()
            serializable.append(d)
        with open(self.trade_log_path, "w") as f:
            json.dump(serializable, f, indent=2)

    def execute(self, action: TradeAction) -> Trade | None:
        signal = action.signal

        if signal in (Signal.BUY, Signal.SELL):
            if self._open_trade is not None:
                return None
            tx_type = "BUY" if signal == Signal.BUY else "SELL"
            self._place_upstox_order(tx_type, action.price)
            trade = self._open_new_trade(action)
            self._save_trades()
            logger.warning("🔴 LIVE %s #%d: price=%.2f", tx_type, trade.trade_id, action.price)
            return trade

        elif signal in (Signal.EXIT, Signal.EXIT_LONG, Signal.EXIT_SHORT):
            if self._open_trade is None:
                return None
            tx_type = "SELL" if self._open_trade.direction == "long" else "BUY"
            self._place_upstox_order(tx_type, action.price)
            trade = self._close_current_trade(action)
            if trade:
                self._save_trades()
                emoji = "✅" if trade.pnl_points > 0 else "❌"
                logger.warning(
                    "%s LIVE EXIT #%d: P&L=%.2f pts (₹%.2f), net=₹%.2f [%s]",
                    emoji, trade.trade_id, trade.pnl_points,
                    trade.pnl_rupees, trade.net_pnl, trade.exit_reason,
                )
            return trade

        return None
