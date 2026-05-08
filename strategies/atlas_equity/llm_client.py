"""Gemini LLM client for ATLAS strategy — free tier only."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger(__name__)

# Free tier limits: 15 RPM, 1500 RPD
_RATE_LIMIT_DELAY = 4.5  # seconds between calls to stay under 15 RPM


class GeminiClient:
    """Minimal Gemini API client using only urllib (no extra deps)."""

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, model: str = "gemini-2.0-flash", max_tokens: int = 4096, temperature: float = 0.3):
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY not set in environment")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._last_call_time = 0.0

    def _rate_limit(self):
        """Ensure we don't exceed free tier RPM."""
        elapsed = time.time() - self._last_call_time
        if elapsed < _RATE_LIMIT_DELAY:
            time.sleep(_RATE_LIMIT_DELAY - elapsed)
        self._last_call_time = time.time()

    def generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """Call Gemini and return text response.

        Args:
            prompt: User message
            system_prompt: Optional system instruction

        Returns:
            Generated text response
        """
        self._rate_limit()

        url = f"{self.BASE_URL}/models/{self.model}:generateContent?key={self.api_key}"

        contents = []
        if system_prompt:
            contents.append({"role": "user", "parts": [{"text": system_prompt}]})
            contents.append({"role": "model", "parts": [{"text": "Understood. I will follow these instructions."}]})

        contents.append({"role": "user", "parts": [{"text": prompt}]})

        payload = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": self.max_tokens,
                "temperature": self.temperature,
            },
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

        try:
            resp = urllib.request.urlopen(req, timeout=60)
            result = json.loads(resp.read())
            text = result["candidates"][0]["content"]["parts"][0]["text"]
            return text.strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:500]
            logger.error("Gemini API error %d: %s", e.code, body)
            if e.code == 429:
                logger.warning("Rate limited — waiting 60s and retrying")
                time.sleep(60)
                return self.generate(prompt, system_prompt)
            raise
        except Exception as e:
            logger.error("Gemini call failed: %s", e)
            raise

    def generate_json(self, prompt: str, system_prompt: Optional[str] = None) -> dict | list:
        """Call Gemini and parse JSON from response."""
        full_prompt = prompt + "\n\nRespond with valid JSON only. No markdown, no explanation."
        text = self.generate(full_prompt, system_prompt)

        # Strip markdown code fences if present
        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.error("Failed to parse LLM JSON response: %s", text[:200])
            return {}
