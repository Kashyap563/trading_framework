"""Specialized analyst agents for ATLAS equity strategy."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .llm_client import GeminiClient

logger = logging.getLogger(__name__)


@dataclass
class AnalystReport:
    """Structured output from an analyst agent."""
    agent: str
    symbol: str
    summary: str
    sentiment: str  # bullish, bearish, neutral
    confidence: float  # 0-1
    key_points: list[str]


class MarketAnalyst:
    """Analyzes price action, technicals, and chart patterns."""

    SYSTEM_PROMPT = """You are an elite market analyst specializing in Indian equities (NSE).
Your role is to analyze technical data and provide objective market structure assessment.
Focus on: trend direction, support/resistance, momentum, volume patterns, and key levels.
Be concise — 3-5 sentences max. End with sentiment (bullish/bearish/neutral) and confidence (0-1)."""

    def __init__(self, llm: GeminiClient):
        self.llm = llm

    def analyze(self, symbol: str, market_data: dict) -> AnalystReport:
        """Analyze a stock's technical setup."""
        prompt = f"""Analyze {symbol} (NSE) technical setup:

Price: ₹{market_data.get('close', 'N/A')} | Open: ₹{market_data.get('open', 'N/A')} | High: ₹{market_data.get('high', 'N/A')} | Low: ₹{market_data.get('low', 'N/A')}
Volume: {market_data.get('volume', 'N/A')}
52W High: ₹{market_data.get('high_52w', 'N/A')} | 52W Low: ₹{market_data.get('low_52w', 'N/A')}
SMA20: ₹{market_data.get('sma20', 'N/A')} | SMA50: ₹{market_data.get('sma50', 'N/A')} | SMA200: ₹{market_data.get('sma200', 'N/A')}
RSI(14): {market_data.get('rsi', 'N/A')}
MACD: {market_data.get('macd', 'N/A')} | Signal: {market_data.get('macd_signal', 'N/A')}
ADX: {market_data.get('adx', 'N/A')}
Change 1W: {market_data.get('change_1w', 'N/A')}% | 1M: {market_data.get('change_1m', 'N/A')}%

Respond as JSON:
{{"summary": "...", "sentiment": "bullish|bearish|neutral", "confidence": 0.0-1.0, "key_points": ["...", "..."]}}"""

        result = self.llm.generate_json(prompt, self.SYSTEM_PROMPT)
        return AnalystReport(
            agent="market",
            symbol=symbol,
            summary=result.get("summary", ""),
            sentiment=result.get("sentiment", "neutral"),
            confidence=result.get("confidence", 0.5),
            key_points=result.get("key_points", []),
        )


class NewsAnalyst:
    """Analyzes financial news and sentiment for stocks."""

    SYSTEM_PROMPT = """You are an elite financial news analyst covering Indian markets (NSE).
Your role is to assess news sentiment, identify catalysts, and evaluate source reliability.
Focus on: earnings, management changes, sector trends, regulatory impacts, FII/DII activity.
Be concise — 3-5 sentences max. End with sentiment and confidence."""

    def __init__(self, llm: GeminiClient):
        self.llm = llm

    def analyze(self, symbol: str, news_items: list[dict]) -> AnalystReport:
        """Analyze news for a stock."""
        if not news_items:
            return AnalystReport(
                agent="news", symbol=symbol,
                summary="No recent news available.",
                sentiment="neutral", confidence=0.3, key_points=[],
            )

        news_text = "\n".join(
            f"- [{item.get('date', '')}] {item.get('title', '')} ({item.get('source', '')})"
            for item in news_items[:10]
        )

        prompt = f"""Analyze recent news for {symbol} (NSE):

{news_text}

Assess: overall sentiment, key catalysts, any red flags, and trading relevance.
Respond as JSON:
{{"summary": "...", "sentiment": "bullish|bearish|neutral", "confidence": 0.0-1.0, "key_points": ["...", "..."]}}"""

        result = self.llm.generate_json(prompt, self.SYSTEM_PROMPT)
        return AnalystReport(
            agent="news",
            symbol=symbol,
            summary=result.get("summary", ""),
            sentiment=result.get("sentiment", "neutral"),
            confidence=result.get("confidence", 0.5),
            key_points=result.get("key_points", []),
        )


class FundamentalAnalyst:
    """Analyzes financial fundamentals from screener data."""

    SYSTEM_PROMPT = """You are an elite fundamental analyst covering Indian equities (NSE).
Your role is to assess financial health, valuation, growth trajectory, and quality.
Focus on: PE ratio, revenue/profit growth, ROE, debt levels, promoter holding, cash flow.
Be concise — 3-5 sentences max. End with sentiment and confidence."""

    def __init__(self, llm: GeminiClient):
        self.llm = llm

    def analyze(self, symbol: str, fundamentals: dict) -> AnalystReport:
        """Analyze fundamentals for a stock."""
        if not fundamentals:
            return AnalystReport(
                agent="fundamental", symbol=symbol,
                summary="No fundamental data available.",
                sentiment="neutral", confidence=0.3, key_points=[],
            )

        prompt = f"""Analyze fundamentals for {symbol} (NSE):

Market Cap: ₹{fundamentals.get('market_cap', 'N/A')} Cr
PE Ratio: {fundamentals.get('pe', 'N/A')} | Industry PE: {fundamentals.get('industry_pe', 'N/A')}
ROE: {fundamentals.get('roe', 'N/A')}% | ROCE: {fundamentals.get('roce', 'N/A')}%
Revenue Growth (3Y): {fundamentals.get('revenue_growth_3y', 'N/A')}%
Profit Growth (3Y): {fundamentals.get('profit_growth_3y', 'N/A')}%
Debt/Equity: {fundamentals.get('debt_equity', 'N/A')}
Promoter Holding: {fundamentals.get('promoter_holding', 'N/A')}%
Promoter Pledge: {fundamentals.get('promoter_pledge', 'N/A')}%
Dividend Yield: {fundamentals.get('dividend_yield', 'N/A')}%
Book Value: ₹{fundamentals.get('book_value', 'N/A')}
EPS (TTM): ₹{fundamentals.get('eps', 'N/A')}

Respond as JSON:
{{"summary": "...", "sentiment": "bullish|bearish|neutral", "confidence": 0.0-1.0, "key_points": ["...", "..."]}}"""

        result = self.llm.generate_json(prompt, self.SYSTEM_PROMPT)
        return AnalystReport(
            agent="fundamental",
            symbol=symbol,
            summary=result.get("summary", ""),
            sentiment=result.get("sentiment", "neutral"),
            confidence=result.get("confidence", 0.5),
            key_points=result.get("key_points", []),
        )
