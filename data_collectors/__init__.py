"""Data collectors for fetching and saving Nifty 50 historical data from Upstox APIs.

Scripts:
    collect_intraday  — Nifty 50 index 1-min OHLCV (V3 API)
    collect_futures   — Nifty 50 futures 1-min OHLCV (V2 expired-instruments API)
    collect_options   — Nifty 50 options 1-min OHLCV (V2 expired-instruments API)

Shared utilities live in _common.py (API client, resume tracker, rate limiting).
"""
