"""Portfolio manager — tracks positions, P&L, and executes orders."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional

from .trading_agent import TradeDecision, Portfolio

logger = logging.getLogger(__name__)

_STATE_FILE = os.path.join(os.path.dirname(__file__), "atlas_portfolio_state.json")


class PortfolioManager:
    """Manages portfolio state, position tracking, and order execution."""

    def __init__(self, initial_capital: float = 500_000.0, state_file: str = _STATE_FILE):
        self.initial_capital = initial_capital
        self.state_file = state_file
        self.portfolio = Portfolio(cash=initial_capital, total_value=initial_capital)
        self.trade_log: list[dict] = []
        self._load_state()

    def get_portfolio(self) -> Portfolio:
        """Get current portfolio state."""
        return self.portfolio

    def execute_decisions(self, decisions: list[TradeDecision], quotes: dict[str, dict]) -> list[dict]:
        """Execute trade decisions and update portfolio.

        Args:
            decisions: List of trade decisions from the agent
            quotes: Current market quotes {symbol: {price, ...}}

        Returns:
            List of executed trade records
        """
        executed = []

        for decision in decisions:
            if decision.action == "HOLD" or decision.quantity <= 0:
                continue

            symbol = decision.symbol
            current_price = decision.price or quotes.get(symbol, {}).get("last_price", 0)

            if not current_price:
                logger.warning("No price available for %s, skipping", symbol)
                continue

            if decision.action == "BUY":
                trade = self._execute_buy(symbol, decision.quantity, current_price, decision.reasoning)
            elif decision.action == "SELL":
                trade = self._execute_sell(symbol, decision.quantity, current_price, decision.reasoning)
            else:
                continue

            if trade:
                executed.append(trade)

        self._update_portfolio_value(quotes)
        self._save_state()
        return executed

    def _execute_buy(self, symbol: str, quantity: int, price: float, reasoning: str) -> Optional[dict]:
        """Execute a buy order."""
        cost = quantity * price
        brokerage = 20.0  # flat per order

        # Check cash
        if cost + brokerage > self.portfolio.cash:
            # Reduce quantity to fit
            max_qty = int((self.portfolio.cash - brokerage) / price)
            if max_qty <= 0:
                logger.warning("BUY %s: insufficient cash (need ₹%.0f, have ₹%.0f)",
                               symbol, cost, self.portfolio.cash)
                return None
            quantity = max_qty
            cost = quantity * price

        # Check max position size (20% of total)
        max_position = self.portfolio.total_value * 0.20
        existing_value = 0
        if symbol in self.portfolio.positions:
            existing_value = self.portfolio.positions[symbol]["qty"] * price
        if existing_value + cost > max_position:
            max_qty = int((max_position - existing_value) / price)
            if max_qty <= 0:
                logger.warning("BUY %s: would exceed 20%% position limit", symbol)
                return None
            quantity = max_qty
            cost = quantity * price

        # Execute
        self.portfolio.cash -= (cost + brokerage)

        if symbol in self.portfolio.positions:
            pos = self.portfolio.positions[symbol]
            total_qty = pos["qty"] + quantity
            pos["avg_price"] = (pos["avg_price"] * pos["qty"] + price * quantity) / total_qty
            pos["qty"] = total_qty
        else:
            self.portfolio.positions[symbol] = {
                "qty": quantity,
                "avg_price": price,
                "entry_date": datetime.now().isoformat(),
                "current_price": price,
                "days_held": 0,
            }

        trade = {
            "action": "BUY",
            "symbol": symbol,
            "quantity": quantity,
            "price": price,
            "cost": cost + brokerage,
            "timestamp": datetime.now().isoformat(),
            "reasoning": reasoning,
        }
        self.trade_log.append(trade)

        logger.info("EXECUTED BUY: %s x%d @ ₹%.1f (cost ₹%.0f)", symbol, quantity, price, cost + brokerage)
        return trade

    def _execute_sell(self, symbol: str, quantity: int, price: float, reasoning: str) -> Optional[dict]:
        """Execute a sell order."""
        if symbol not in self.portfolio.positions:
            logger.warning("SELL %s: no position to sell", symbol)
            return None

        pos = self.portfolio.positions[symbol]
        quantity = min(quantity, pos["qty"])  # can't sell more than held

        proceeds = quantity * price
        brokerage = 20.0

        # Calculate P&L
        pnl = (price - pos["avg_price"]) * quantity - brokerage
        pnl_pct = (price - pos["avg_price"]) / pos["avg_price"] * 100

        # Update position
        pos["qty"] -= quantity
        if pos["qty"] <= 0:
            del self.portfolio.positions[symbol]

        self.portfolio.cash += (proceeds - brokerage)

        trade = {
            "action": "SELL",
            "symbol": symbol,
            "quantity": quantity,
            "price": price,
            "proceeds": proceeds - brokerage,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "days_held": pos.get("days_held", 0),
            "timestamp": datetime.now().isoformat(),
            "reasoning": reasoning,
        }
        self.trade_log.append(trade)

        logger.info(
            "EXECUTED SELL: %s x%d @ ₹%.1f | P&L: ₹%.0f (%+.1f%%)",
            symbol, quantity, price, pnl, pnl_pct,
        )
        return trade

    def _update_portfolio_value(self, quotes: dict[str, dict]):
        """Update portfolio total value with current prices."""
        invested_value = 0.0
        for symbol, pos in self.portfolio.positions.items():
            current_price = quotes.get(symbol, {}).get("last_price", pos["avg_price"])
            pos["current_price"] = current_price
            invested_value += pos["qty"] * current_price

        self.portfolio.total_value = self.portfolio.cash + invested_value
        self.portfolio.total_pnl = self.portfolio.total_value - self.initial_capital

    def update_days_held(self):
        """Increment days held for all positions (call daily)."""
        for pos in self.portfolio.positions.values():
            pos["days_held"] = pos.get("days_held", 0) + 1

    def get_completed_trades(self, last_n: int = 10) -> list[dict]:
        """Get last N completed (SELL) trades for OPRO evaluation."""
        sells = [t for t in self.trade_log if t["action"] == "SELL"]
        return sells[-last_n:]

    def _save_state(self):
        """Persist portfolio state to disk."""
        state = {
            "cash": self.portfolio.cash,
            "positions": self.portfolio.positions,
            "total_value": self.portfolio.total_value,
            "total_pnl": self.portfolio.total_pnl,
            "trade_log": self.trade_log[-100:],  # keep last 100
            "saved_at": datetime.now().isoformat(),
        }
        try:
            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error("Failed to save portfolio state: %s", e)

    def _load_state(self):
        """Load portfolio state from disk."""
        if not os.path.exists(self.state_file):
            return

        try:
            with open(self.state_file) as f:
                state = json.load(f)
            self.portfolio.cash = state.get("cash", self.initial_capital)
            self.portfolio.positions = state.get("positions", {})
            self.portfolio.total_value = state.get("total_value", self.initial_capital)
            self.portfolio.total_pnl = state.get("total_pnl", 0)
            self.trade_log = state.get("trade_log", [])
            logger.info(
                "Loaded portfolio state: cash=₹%.0f, positions=%d, P&L=₹%.0f",
                self.portfolio.cash, len(self.portfolio.positions), self.portfolio.total_pnl,
            )
        except Exception as e:
            logger.error("Failed to load portfolio state: %s", e)
