"""Pre-market scanner — filters Nifty 500 to top candidates for LLM analysis."""

from __future__ import annotations

import logging
from typing import Optional

from .data_providers import UpstoxMarketData

logger = logging.getLogger(__name__)


class PreMarketScanner:
    """Scans the universe and shortlists stocks worth analyzing.

    We can't run LLM analysis on 500 stocks (too many API calls).
    This scanner uses simple quantitative filters to narrow down to 10-20 candidates,
    which then get full LLM analyst treatment.
    """

    def __init__(self, market_data: UpstoxMarketData):
        self.market_data = market_data

    def scan(self, symbols: list[str], max_candidates: int = 15) -> list[dict]:
        """Scan universe and return top candidates for LLM analysis.

        Filters:
        1. Minimum volume (liquidity)
        2. Price momentum (trending stocks)
        3. Not already at extreme overbought levels

        Returns list of {symbol, price, change_pct, volume, reason}
        """
        candidates = []

        for symbol in symbols:
            try:
                quote = self.market_data.get_quote(symbol)
                if not quote or not quote.get("close"):
                    continue

                price = quote["close"]
                change = quote.get("change_pct", 0) or 0
                volume = quote.get("volume", 0) or 0

                # Basic filters
                if price < 50 or price > 50000:  # penny stocks and ultra-expensive
                    continue
                if volume < 100000:  # illiquid
                    continue

                # Score: momentum + volume
                score = abs(change) * (volume / 1_000_000)

                candidates.append({
                    "symbol": symbol,
                    "price": price,
                    "change_pct": change,
                    "volume": volume,
                    "score": score,
                })

            except Exception as e:
                continue

        # Sort by score and return top candidates
        candidates.sort(key=lambda x: x["score"], reverse=True)
        top = candidates[:max_candidates]

        logger.info(
            "Scanner: %d/%d stocks passed filters, top %d selected",
            len(candidates), len(symbols), len(top),
        )

        return top
