"""Adaptive-OPRO: Prompt optimization based on trading performance."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from .llm_client import GeminiClient

logger = logging.getLogger(__name__)


@dataclass
class PromptVersion:
    """A versioned prompt with its performance score."""
    version: int
    prompt: str
    score: float = 0.0
    trades_evaluated: int = 0
    roi_pct: float = 0.0


class AdaptiveOPRO:
    """Evolves the trading agent's prompt based on realized P&L.

    Every `window_size` completed trades, it:
    1. Scores the current prompt based on ROI
    2. Asks a meta-optimizer LLM to improve the prompt
    3. Replaces the prompt if the new version is valid
    """

    META_PROMPT = """You are a trading prompt optimizer. Your job is to improve a trading agent's
system prompt based on its recent performance.

## PROMPT HISTORY (version → score)
{history}

## CURRENT PROMPT (version {current_version}, score: {current_score})
{current_prompt}

## RECENT TRADE OUTCOMES
{trade_outcomes}

## YOUR TASK
Analyze what's working and what's not. Then produce an improved prompt.

RULES:
- Keep the core structure (portfolio rules, analyst integration, JSON output format)
- Improve decision-making guidance based on observed patterns
- If the agent is exiting too early/late, adjust guidance
- If it's picking wrong sectors, add sector awareness
- If position sizing is off, refine sizing guidance
- Keep it concise — long prompts hurt performance

Respond as JSON:
{{
  "analysis": "What's working and what needs improvement",
  "improved_prompt": "The full improved system prompt text",
  "changes_made": ["list of specific changes"]
}}"""

    def __init__(self, llm: GeminiClient, window_size: int = 5):
        self.llm = llm
        self.window_size = window_size
        self.history: list[PromptVersion] = []
        self.pending_trades: list[dict] = []
        self._current_version = 0

    def record_trade(self, trade: dict):
        """Record a completed trade for evaluation."""
        self.pending_trades.append(trade)
        logger.debug("OPRO: Recorded trade %d/%d", len(self.pending_trades), self.window_size)

    def should_evolve(self) -> bool:
        """Check if we have enough trades to trigger evolution."""
        return len(self.pending_trades) >= self.window_size

    def evolve(self, current_prompt: str) -> Optional[str]:
        """Attempt to evolve the prompt based on recent performance.

        Returns:
            New prompt if evolution successful, None otherwise.
        """
        if not self.should_evolve():
            return None

        # Calculate score for current window
        trades = self.pending_trades[:self.window_size]
        self.pending_trades = self.pending_trades[self.window_size:]

        total_pnl = sum(t.get("pnl", 0) for t in trades)
        capital = 500_000.0
        roi = total_pnl / capital * 100

        # Score: map ROI to 0-100 scale (-20% → 0, 0% → 50, +20% → 100)
        score = max(0, min(100, 50 + 250 * (roi / 100)))

        # Record current version
        self._current_version += 1
        self.history.append(PromptVersion(
            version=self._current_version,
            prompt=current_prompt[:200] + "...",  # truncate for history
            score=score,
            trades_evaluated=len(trades),
            roi_pct=roi,
        ))

        logger.info(
            "OPRO: Window %d complete — %d trades, ROI=%.2f%%, Score=%.1f",
            self._current_version, len(trades), roi, score,
        )

        # Build history text
        history_text = "\n".join(
            f"  v{h.version}: score={h.score:.1f} (ROI={h.roi_pct:+.2f}%, {h.trades_evaluated} trades)"
            for h in self.history[-5:]  # last 5 versions
        )

        # Build trade outcomes text
        outcomes_text = "\n".join(
            f"  {t.get('symbol','?')}: {t.get('action','?')} → P&L ₹{t.get('pnl',0):,.0f} "
            f"({t.get('pnl_pct',0):+.1f}%) held {t.get('days_held',0)}d | reason: {t.get('exit_reason','?')}"
            for t in trades
        )

        # Ask meta-optimizer
        prompt = self.META_PROMPT.format(
            history=history_text,
            current_version=self._current_version,
            current_score=score,
            current_prompt=current_prompt,
            trade_outcomes=outcomes_text,
        )

        try:
            result = self.llm.generate_json(prompt)
            if isinstance(result, dict) and "improved_prompt" in result:
                new_prompt = result["improved_prompt"]
                changes = result.get("changes_made", [])
                analysis = result.get("analysis", "")

                logger.info("OPRO: Prompt evolved — %s", analysis[:100])
                for change in changes[:3]:
                    logger.info("OPRO:   → %s", change)

                return new_prompt
        except Exception as e:
            logger.warning("OPRO: Evolution failed: %s", e)

        return None

    def get_stats(self) -> dict:
        """Get optimization statistics."""
        return {
            "current_version": self._current_version,
            "total_evolutions": len(self.history),
            "pending_trades": len(self.pending_trades),
            "best_score": max((h.score for h in self.history), default=0),
            "latest_score": self.history[-1].score if self.history else 0,
        }
