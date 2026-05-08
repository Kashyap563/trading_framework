"""Data providers for ATLAS — news, fundamentals, market data."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional
from xml.etree import ElementTree

logger = logging.getLogger(__name__)


class GoogleNewsProvider:
    """Fetch news from Google News RSS (free, no API key)."""

    RSS_URL = "https://news.google.com/rss/search?q={query}+stock+NSE&hl=en-IN&gl=IN&ceid=IN:en"

    def fetch(self, symbol: str, company_name: str = "", max_items: int = 5) -> list[dict]:
        """Fetch recent news for a stock symbol."""
        query = urllib.parse.quote(f"{company_name or symbol} share")
        url = self.RSS_URL.format(query=query)

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=10)
            xml_data = resp.read().decode("utf-8")

            root = ElementTree.fromstring(xml_data)
            items = []
            for item in root.findall(".//item")[:max_items]:
                title = item.findtext("title", "")
                pub_date = item.findtext("pubDate", "")
                source = item.findtext("source", "")
                items.append({
                    "title": title,
                    "date": pub_date,
                    "source": source,
                })

            logger.debug("Fetched %d news items for %s", len(items), symbol)
            return items

        except Exception as e:
            logger.warning("Failed to fetch news for %s: %s", symbol, e)
            return []


class LiveMintNewsProvider:
    """Fetch news from LiveMint RSS (free)."""

    RSS_URL = "https://www.livemint.com/rss/markets"

    def fetch_market_news(self, max_items: int = 10) -> list[dict]:
        """Fetch general market news."""
        try:
            req = urllib.request.Request(self.RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=10)
            xml_data = resp.read().decode("utf-8")

            root = ElementTree.fromstring(xml_data)
            items = []
            for item in root.findall(".//item")[:max_items]:
                title = item.findtext("title", "")
                pub_date = item.findtext("pubDate", "")
                items.append({"title": title, "date": pub_date, "source": "LiveMint"})

            return items
        except Exception as e:
            logger.warning("Failed to fetch LiveMint news: %s", e)
            return []


class ScreenerProvider:
    """Fetch fundamental data from Screener.in (scraping, no API key)."""

    BASE_URL = "https://www.screener.in/company/{symbol}/consolidated/"

    def fetch(self, symbol: str) -> dict:
        """Fetch key fundamentals for a stock from Screener.in."""
        url = self.BASE_URL.format(symbol=symbol)

        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Accept": "text/html",
            })
            resp = urllib.request.urlopen(req, timeout=15)
            html = resp.read().decode("utf-8")

            data = {}
            # Extract key ratios from the page
            data["market_cap"] = self._extract_value(html, "Market Cap")
            data["pe"] = self._extract_value(html, "Stock P/E")
            data["roe"] = self._extract_value(html, "ROE")
            data["roce"] = self._extract_value(html, "ROCE")
            data["debt_equity"] = self._extract_value(html, "Debt to equity")
            data["book_value"] = self._extract_value(html, "Book Value")
            data["dividend_yield"] = self._extract_value(html, "Dividend Yield")
            data["industry_pe"] = self._extract_value(html, "Industry PE")
            data["promoter_holding"] = self._extract_value(html, "Promoter holding")
            data["eps"] = self._extract_value(html, "EPS")

            logger.debug("Fetched fundamentals for %s: %s", symbol, data)
            return data

        except Exception as e:
            logger.warning("Failed to fetch screener data for %s: %s", symbol, e)
            return {}

    def _extract_value(self, html: str, label: str) -> Optional[str]:
        """Extract a numeric value following a label in screener HTML."""
        pattern = rf'{re.escape(label)}\s*</span>\s*<span[^>]*>\s*<span[^>]*>([\d,\.]+)'
        match = re.search(pattern, html)
        if match:
            return match.group(1).replace(",", "")

        # Fallback: simpler pattern
        pattern2 = rf'{re.escape(label)}[^<]*<[^>]*>([\d,\.]+)'
        match2 = re.search(pattern2, html)
        if match2:
            return match2.group(1).replace(",", "")

        return None


class UpstoxMarketData:
    """Fetch market data from Upstox API."""

    BASE_URL = "https://api.upstox.com/v2"

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

    def get_quote(self, symbol: str) -> dict:
        """Get current quote for a stock."""
        instrument_key = f"NSE_EQ|{symbol}"
        url = f"{self.BASE_URL}/market-quote/quotes?instrument_key={urllib.parse.quote(instrument_key)}"

        try:
            req = urllib.request.Request(url, headers=self.headers)
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())

            if data.get("status") == "success":
                quote = data.get("data", {}).get(instrument_key, {})
                ohlc = quote.get("ohlc", {})
                return {
                    "open": ohlc.get("open"),
                    "high": ohlc.get("high"),
                    "low": ohlc.get("low"),
                    "close": ohlc.get("close"),
                    "volume": quote.get("volume"),
                    "last_price": quote.get("last_price"),
                    "change_pct": quote.get("net_change"),
                }
            return {}
        except Exception as e:
            logger.warning("Failed to get quote for %s: %s", symbol, e)
            return {}

    def get_historical(self, symbol: str, interval: str = "day", days: int = 60) -> list[dict]:
        """Get historical candles for a stock."""
        instrument_key = f"NSE_EQ|{symbol}"
        to_date = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        url = (
            f"{self.BASE_URL}/historical-candle/{urllib.parse.quote(instrument_key)}"
            f"/{interval}/{to_date}/{from_date}"
        )

        try:
            req = urllib.request.Request(url, headers=self.headers)
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())

            if data.get("status") == "success":
                candles = data.get("data", {}).get("candles", [])
                return [
                    {
                        "timestamp": c[0],
                        "open": c[1],
                        "high": c[2],
                        "low": c[3],
                        "close": c[4],
                        "volume": c[5],
                    }
                    for c in candles
                ]
            return []
        except Exception as e:
            logger.warning("Failed to get historical for %s: %s", symbol, e)
            return []
