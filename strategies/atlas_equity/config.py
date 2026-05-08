"""Configuration for ATLAS Equity Strategy."""

from dataclasses import dataclass, field


@dataclass
class AtlasConfig:
    """All configurable parameters for the ATLAS equity strategy."""

    # Capital
    total_capital: float = 500_000.0
    max_position_pct: float = 20.0  # max 20% of capital per stock
    max_positions: int = 10

    # Universe
    universe: str = "NIFTY500"  # NIFTY50, NIFTY200, NIFTY500

    # Trading style
    allow_intraday: bool = True  # MIS orders
    allow_delivery: bool = True  # CNC orders

    # Risk
    max_loss_per_trade_pct: float = 8.0  # hard stop loss
    trailing_stop_pct: float = 5.0

    # LLM
    llm_model: str = "gemini-2.0-flash"
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.3

    # Adaptive-OPRO
    opro_window_size: int = 5  # trades before prompt evolution
    opro_enabled: bool = True

    # Scheduling
    scan_time: str = "09:00"  # IST - pre-market scan
    decision_time: str = "09:20"  # IST - after market open

    # Data sources
    news_sources: list = field(default_factory=lambda: [
        "google_news",
        "livemint",
    ])

    # Screener
    screener_enabled: bool = True
