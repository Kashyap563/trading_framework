"""Central Trading Agent — the decision-making core of ATLAS."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .analysts import AnalystReport
from .llm_client import GeminiClient

logger = logging.getLogger(__name__)


@dataclass
class TradeDecision:
    """A single trade decision from the agent."""
    action: str  # BUY, SELL, HOLD
    symbol: str
    quantity: int = 0
    order_type: str = "LIMIT"  # MARKET, LIMIT
    price: Optional[float] = None
    reasoning: str = ""
    confidence: float = 0.0


@dataclass
class Portfolio:
    """Current portfolio state."""
    cash: float = 500_000.0
    positions: dict = field(default_factory=dict)  # symbol -> {qty, avg_price, entry_date}
    total_value: float = 500_000.0
    daily_pnl: float = 0.0
    total_pnl: float = 0.0


class CentralTradingAgent:
    """The main decision-making agent that synthesizes analyst inputs."""

    SYSTEM_PROMPT = """You are an elite equity trader managing a concentrated long-only portfolio on NSE (India).
Your capital is ₹5,00,000. You make ONE decision per day.

Your goal: maximize risk-adjusted returns through high-conviction stock picks.

RULES:
- Long only (BUY and SELL only, no shorting)
- Total invested must never exceed available cash
- Maximum 20% of capital in any single stock
- Exit positions when thesis breaks or target achieved
- Patience is edge — don't trade for the sake of trading
- Consider transaction costs (brokerage ~₹20 per order + STT)

You receive analysis from 3 specialist agents:
1. Market Analyst — technicals, price action, momentum
2. News Analyst — sentiment, catalysts, events
3. Fundamental Analyst — valuation, growth, quality

Synthesize all inputs and make decisions. Quality over quantity."""

    def __init__(self, llm: GeminiClient, config=None):
        self.llm = llm
        self.config = config
        self._instruction_prompt = self.SYSTEM_PROMPT
        self._trade_history: list[dict] = []

    def decide(
        self,
        portfolio: Portfolio,
        candidates: list[dict],
        current_holdings_analysis: list[dict],
    ) -> list[TradeDecision]:
        """Make trading decisions for today.

        Args:
            portfolio: Current portfolio state
            candidates: List of {symbol, market_report, news_report, fundamental_report}
            current_holdings_analysis: Analysis of currently held stocks

        Returns:
            List of trade decisions
        """
        # Build the decision prompt
        prompt = self._build_decision_prompt(portfolio, candidates, current_holdings_analysis)

        # Get LLM decision
        response = self.llm.generate_json(prompt, self._instruction_prompt)

        # Parse decisions
        decisions = []
        if isinstance(response, list):
            for item in response:
                decisions.append(TradeDecision(
                    action=item.get("action", "HOLD"),
                    symbol=item.get("symbol", ""),
                    quantity=item.get("quantity", 0),
                    order_type=item.get("order_type", "LIMIT"),
                    price=item.get("price"),
                    reasoning=item.get("reasoning", ""),
                    confidence=item.get("confidence", 0.5),
                ))
        elif isinstance(response, dict) and response.get("action"):
            decisions.append(TradeDecision(
                action=response.get("action", "HOLD"),
                symbol=response.get("symbol", ""),
                quantity=response.get("quantity", 0),
                order_type=response.get("order_type", "LIMIT"),
                price=response.get("price"),
                reasoning=response.get("reasoning", ""),
                confidence=response.get("confidence", 0.5),
            ))

        # Log decisions
        for d in decisions:
            if d.action != "HOLD":
                logger.info(
                    "DECISION: %s %s x%d @ %s | Confidence: %.0f%% | %s",
                    d.action, d.symbol, d.quantity,
                    f"₹{d.price}" if d.price else "MARKET",
                    d.confidence * 100, d.reasoning[:100],
                )

        return decisions

    def _build_decision_prompt(
        self,
        portfolio: Portfolio,
        candidates: list[dict],
        holdings_analysis: list[dict],
    ) -> str:
        """Build the full decision prompt with all context."""
        parts = []

        # Portfolio state
        parts.append(f"""## PORTFOLIO STATUS
Cash: ₹{portfolio.cash:,.0f} | Invested: ₹{portfolio.total_value - portfolio.cash:,.0f} | Total: ₹{portfolio.total_value:,.0f}
Daily P&L: ₹{portfolio.daily_pnl:,.0f} | Total P&L: ₹{portfolio.total_pnl:,.0f}
Positions: {len(portfolio.positions)}""")

        # Current holdings
        if portfolio.positions:
            parts.append("\n## CURRENT HOLDINGS (review for HOLD/SELL)")
            for pos_symbol, pos_data in portfolio.positions.items():
                pnl_pct = ((pos_data.get("current_price", pos_data["avg_price"]) - pos_data["avg_price"])
                           / pos_data["avg_price"] * 100)
                parts.append(
                    f"- {pos_symbol}: {pos_data['qty']} shares @ ₹{pos_data['avg_price']:.1f} "
                    f"| Current: ₹{pos_data.get('current_price', 'N/A')} | P&L: {pnl_pct:+.1f}% "
                    f"| Held: {pos_data.get('days_held', '?')} days"
                )

            # Holdings analysis from analysts
            if holdings_analysis:
                parts.append("\n### Analyst Views on Holdings:")
                for ha in holdings_analysis:
                    parts.append(f"**{ha['symbol']}**: Market={ha.get('market_sentiment','?')} | "
                                 f"News={ha.get('news_sentiment','?')} | "
                                 f"Key: {ha.get('key_insight','')}")

        # New candidates
        if candidates:
            parts.append(f"\n## BUY CANDIDATES (top picks from Nifty 500 scan)")
            for c in candidates[:8]:  # limit to top 8 to save tokens
                parts.append(f"""
**{c['symbol']}** — ₹{c.get('price', 'N/A')}
- Market: {c.get('market_summary', 'N/A')} [{c.get('market_sentiment', '?')}]
- News: {c.get('news_summary', 'N/A')} [{c.get('news_sentiment', '?')}]
- Fundamentals: {c.get('fundamental_summary', 'N/A')} [{c.get('fundamental_sentiment', '?')}]""")

        # Decision instruction
        parts.append(f"""
## YOUR DECISION
Available cash for new buys: ₹{portfolio.cash:,.0f}
Max per stock: ₹{portfolio.total_value * 0.20:,.0f} (20% of portfolio)

Decide: Which stocks to BUY, which to SELL, which to HOLD.
Only act on high-conviction ideas. [] is valid if nothing compelling.

Respond as JSON array:
[
  {{"action": "BUY|SELL|HOLD", "symbol": "SYMBOL", "quantity": int, "order_type": "LIMIT|MARKET", "price": float_or_null, "reasoning": "brief reason", "confidence": 0.0-1.0}}
]
Return [] if no action today.""")

        return "\n".join(parts)

    def update_instruction(self, new_prompt: str):
        """Update the system instruction (used by Adaptive-OPRO)."""
        self._instruction_prompt = new_prompt
        logger.info("Trading agent instruction updated via Adaptive-OPRO")

    def get_instruction(self) -> str:
        """Get current instruction prompt."""
        return self._instruction_prompt
