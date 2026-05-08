"""ATLAS Equity Strategy Runner — daily execution orchestrator."""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo

from .config import AtlasConfig
from .llm_client import GeminiClient
from .analysts import MarketAnalyst, NewsAnalyst, FundamentalAnalyst
from .trading_agent import CentralTradingAgent
from .adaptive_opro import AdaptiveOPRO
from .portfolio_manager import PortfolioManager
from .data_providers import GoogleNewsProvider, LiveMintNewsProvider, ScreenerProvider, UpstoxMarketData
from .scanner import PreMarketScanner
from .universe import get_universe

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


class AtlasRunner:
    """Orchestrates the daily ATLAS trading workflow."""

    def __init__(self, config: AtlasConfig = None):
        self.config = config or AtlasConfig()

        # LLM
        self.llm = GeminiClient(
            model=self.config.llm_model,
            max_tokens=self.config.llm_max_tokens,
            temperature=self.config.llm_temperature,
        )

        # Agents
        self.market_analyst = MarketAnalyst(self.llm)
        self.news_analyst = NewsAnalyst(self.llm)
        self.fundamental_analyst = FundamentalAnalyst(self.llm)
        self.trading_agent = CentralTradingAgent(self.llm, self.config)
        self.opro = AdaptiveOPRO(self.llm, window_size=self.config.opro_window_size)

        # Portfolio
        self.portfolio_mgr = PortfolioManager(initial_capital=self.config.total_capital)

        # Data providers
        access_token = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
        self.market_data = UpstoxMarketData(access_token)
        self.news_provider = GoogleNewsProvider()
        self.mint_provider = LiveMintNewsProvider()
        self.screener = ScreenerProvider()
        self.scanner = PreMarketScanner(self.market_data)

        # Universe
        self.universe = get_universe(self.config.universe)
        logger.info("ATLAS initialized | Universe: %s (%d stocks) | Capital: ₹%.0f",
                    self.config.universe, len(self.universe), self.config.total_capital)

    def run_daily(self):
        """Execute the full daily trading workflow.

        Steps:
        1. Pre-market scan (filter 500 → 15 candidates)
        2. Analyze current holdings (should we exit?)
        3. Analyze new candidates (should we enter?)
        4. Central agent makes decisions
        5. Execute orders
        6. Update Adaptive-OPRO
        """
        now = datetime.now(IST)
        logger.info("=" * 60)
        logger.info("ATLAS Daily Run | %s | %s", now.strftime("%Y-%m-%d %H:%M IST"), now.strftime("%A"))
        logger.info("=" * 60)

        portfolio = self.portfolio_mgr.get_portfolio()
        logger.info("Portfolio: Cash=₹%.0f | Invested=₹%.0f | P&L=₹%.0f",
                    portfolio.cash, portfolio.total_value - portfolio.cash, portfolio.total_pnl)

        # Step 1: Scan universe for candidates
        logger.info("\n--- STEP 1: Pre-market Scan ---")
        candidates_raw = self.scanner.scan(self.universe, max_candidates=15)
        logger.info("Scanner found %d candidates", len(candidates_raw))

        # Step 2: Analyze current holdings
        logger.info("\n--- STEP 2: Analyze Holdings ---")
        holdings_analysis = self._analyze_holdings()

        # Step 3: Analyze top candidates with LLM agents
        logger.info("\n--- STEP 3: Analyze Candidates ---")
        candidates_analyzed = self._analyze_candidates(candidates_raw[:8])  # top 8 only

        # Step 4: Central agent decision
        logger.info("\n--- STEP 4: Trading Decision ---")
        decisions = self.trading_agent.decide(
            portfolio=portfolio,
            candidates=candidates_analyzed,
            current_holdings_analysis=holdings_analysis,
        )

        if not decisions:
            logger.info("Decision: NO ACTION today (patience)")
        else:
            logger.info("Decision: %d actions", len(decisions))

        # Step 5: Execute
        logger.info("\n--- STEP 5: Execute ---")
        quotes = {}
        for d in decisions:
            if d.symbol:
                q = self.market_data.get_quote(d.symbol)
                if q:
                    quotes[d.symbol] = q

        executed = self.portfolio_mgr.execute_decisions(decisions, quotes)
        logger.info("Executed %d orders", len(executed))

        # Step 6: Update OPRO
        self.portfolio_mgr.update_days_held()
        completed = self.portfolio_mgr.get_completed_trades(last_n=5)
        for trade in completed:
            if trade not in self._opro_recorded:
                self.opro.record_trade(trade)
                self._opro_recorded.add(id(trade))

        if self.opro.should_evolve() and self.config.opro_enabled:
            logger.info("\n--- Adaptive-OPRO: Evolving prompt ---")
            new_prompt = self.opro.evolve(self.trading_agent.get_instruction())
            if new_prompt:
                self.trading_agent.update_instruction(new_prompt)

        # Summary
        portfolio = self.portfolio_mgr.get_portfolio()
        logger.info("\n--- END OF DAY ---")
        logger.info("Portfolio: ₹%.0f | Positions: %d | P&L: ₹%.0f (%.1f%%)",
                    portfolio.total_value, len(portfolio.positions),
                    portfolio.total_pnl, portfolio.total_pnl / self.config.total_capital * 100)
        logger.info("=" * 60)

    def _analyze_holdings(self) -> list[dict]:
        """Run analyst agents on current holdings."""
        portfolio = self.portfolio_mgr.get_portfolio()
        results = []

        for symbol, pos in portfolio.positions.items():
            # Quick market check
            quote = self.market_data.get_quote(symbol)
            if quote:
                pos["current_price"] = quote.get("last_price", pos["avg_price"])

            # News for held stocks
            news = self.news_provider.fetch(symbol, max_items=3)
            news_report = self.news_analyst.analyze(symbol, news)

            results.append({
                "symbol": symbol,
                "market_sentiment": "neutral",  # skip full market analysis for holdings to save API calls
                "news_sentiment": news_report.sentiment,
                "key_insight": news_report.summary[:100] if news_report.summary else "",
            })

            time.sleep(1)  # rate limit

        return results

    def _analyze_candidates(self, candidates: list[dict]) -> list[dict]:
        """Run full analyst pipeline on buy candidates."""
        analyzed = []

        for c in candidates:
            symbol = c["symbol"]
            logger.info("  Analyzing %s...", symbol)

            # Market analysis (use quote data we already have)
            market_data = {
                "close": c.get("price"),
                "open": c.get("price"),
                "high": c.get("price"),
                "low": c.get("price"),
                "volume": c.get("volume"),
            }
            market_report = self.market_analyst.analyze(symbol, market_data)

            # News
            news = self.news_provider.fetch(symbol, max_items=5)
            news_report = self.news_analyst.analyze(symbol, news)

            # Fundamentals (from screener)
            fundamentals = self.screener.fetch(symbol)
            fund_report = self.fundamental_analyst.analyze(symbol, fundamentals)

            analyzed.append({
                "symbol": symbol,
                "price": c.get("price"),
                "market_summary": market_report.summary,
                "market_sentiment": market_report.sentiment,
                "news_summary": news_report.summary,
                "news_sentiment": news_report.sentiment,
                "fundamental_summary": fund_report.summary,
                "fundamental_sentiment": fund_report.sentiment,
            })

            time.sleep(2)  # rate limit between stocks

        return analyzed

    # Track which trades have been sent to OPRO
    _opro_recorded: set = set()


def main():
    """Entry point for ATLAS equity strategy."""
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("atlas_equity.log", mode="a"),
        ],
    )

    # Load env
    from dotenv import load_dotenv
    load_dotenv()

    config = AtlasConfig()
    runner = AtlasRunner(config)
    runner.run_daily()


if __name__ == "__main__":
    main()
