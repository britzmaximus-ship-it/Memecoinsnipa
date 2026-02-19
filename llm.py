"""
llm.py

Safe LLM scoring wrapper for Memecoinsnipa.

Exports:
- class LLMScorer with method:
    score_token(symbol, mint, liquidity_usd, fdv_usd, accel, pair) -> float

Design goals:
- NEVER crash the scanner loop (always returns a float)
- Groq-first, OpenRouter fallback
- Cooldown + min-time-between-calls throttling
- If LLM unavailable, return a heuristic score instead
"""

import os
import time
import json
import logging
from typing import Optional, Dict, Any

import requests

log = logging.getLogger("memecoinsnipa.llm")


def _env_str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else str(v).strip()


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    try:
        return int(str(v).strip()) if v is not None else int(default)
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    try:
        return float(str(v).strip()) if v is not None else float(default)
    except Exception:
        return float(default)


class LLMScorer:
    """
    Produces a numeric score in [0, 1] for each candidate.
    """

    def __init__(self):
        # Keys
        self.groq_key = _env_str("GROQ_API_KEY", "")
        self.openrouter_key = _env_str("OPENROUTER_API_KEY", "")

        # Models
        self.groq_model = _env_str("GROQ_MODEL", "llama-3.3-70b-versatile")
        self.openrouter_model = _env_str("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct")

        # Throttling
        self.groq_cooldown = _env_int("GROQ_COOLDOWN_SECONDS", 600)
        self.min_seconds_between = _env_int("LLM_MIN_SECONDS_BETWEEN_CALLS", 120)

        # Runtime state
        self.last_call = 0.0
        self.cooldown_until = 0.0

        # Hard safety: if Groq/OpenRouter keeps failing, don't spam
        self.fail_streak = 0
        self.fail_backoff_max = _env_int("LLM_FAIL_BACKOFF_MAX_SECONDS", 600)

    def _can_call(self) -> bool:
        now = time.time()
        if now < self.cooldown_until:
            return False
        if now - self.last_call < self.min_seconds_between:
            return False
        # simple backoff after repeated failures
        if self.fail_streak > 0:
            backoff = min(self.fail_backoff_max, 10 * self.fail_streak)
            if now - self.last_call < backoff:
                return False
        return True

    def _heuristic_score(
        self,
        liquidity_usd: float,
        fdv_usd: float,
        accel: float,
        pair: Optional[Dict[str, Any]] = None,
    ) -> float:
        """
        Returns a conservative score in [0,1] based on accel + liquidity + fdv.
        This keeps the bot operating even with no LLM keys.
        """
        try:
            liq = max(0.0, float(liquidity_usd))
            fdv = max(0.0, float(fdv_usd))
            a = max(0.0, float(accel))

            # Normalize accel roughly: 0.9+ is good in your config
            accel_component = min(1.0, a / 2.0)  # accel 2.0 -> 1.0

            # Liquidity: 5k baseline; 50k+ is strong
            liq_component = min(1.0, liq / 50000.0)

            # FDV: lower is better; penalize big fdv (cap effect)
            if fdv <= 0:
                fdv_component = 0.6
            else:
                fdv_component = max(0.0, min(1.0, 1.0 - (fdv / 5_000_000.0)))

            # Volume tilt if present
            vol_h1 = 0.0
            if isinstance(pair, dict):
                vol_h1 = float(((pair.get("volume") or {}).get("h1") or 0.0))
            vol_component = min(1.0, max(0.0, vol_h1 / 50000.0))  # 50k/h1 -> 1.0

            score = (
                0.45 * accel_component +
                0.25 * liq_component +
                0.20 * fdv_component +
                0.10 * vol_component
            )
            return float(max(0.0, min(1.0, score)))
        except Exception:
            return 0.0

    def _extract_score_from_text(self, text: str) -> Optional[float]:
        """
        Accepts outputs like:
        'Score: 0.23'
        '0.23'
        'score=0.23'
        """
        try:
            if not text:
                return None
            t = text.strip()
            # try json first
            if t.startswith("{"):
                obj = json.loads(t)
                v = obj.get("score")
                if isinstance(v, (int, float)):
                    return float(v)

            # find first float-looking token
            import re
            m = re.search(r"(-?\d+(\.\d+)?)", t)
            if not m:
                return None
            v = float(m.group(1))
            # If model returns 0-100 scale, normalize
            if v > 1.5:
                v = v / 100.0
            return float(max(0.0, min(1.0, v)))
        except Exception:
            return None

    def _call_groq(self, prompt: str) -> Optional[str]:
        if not self.groq_key:
            return None
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.groq_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.groq_model,
            "messages": [
                {"role": "system", "content": "Return ONLY a numeric score between 0 and 1."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 60,
        }

        r = requests.post(url, headers=headers, json=payload, timeout=25)

        if r.status_code == 429:
            self.cooldown_until = time.time() + self.groq_cooldown
            log.warning("Groq rate limited. Cooldown %ss", self.groq_cooldown)
            return None

        if r.status_code != 200:
            log.warning("Groq failed %s: %s", r.status_code, (r.text or "")[:200])
            return None

        data = r.json()
        return data["choices"][0]["message"]["content"]

    def _call_openrouter(self, prompt: str) -> Optional[str]:
        if not self.openrouter_key:
            return None
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.openrouter_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.openrouter_model,
            "messages": [
                {"role": "system", "content": "Return ONLY a numeric score between 0 and 1."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 60,
        }

        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code == 429:
            log.warning("OpenRouter rate limited.")
            return None
        if r.status_code != 200:
            log.warning("OpenRouter failed %s: %s", r.status_code, (r.text or "")[:200])
            return None

        data = r.json()
        return data["choices"][0]["message"]["content"]

    def score_token(
        self,
        symbol: str,
        mint: str,
        liquidity_usd: float,
        fdv_usd: float,
        accel: float,
        pair: Optional[Dict[str, Any]] = None,
    ) -> float:
        """
        Always returns a float score (0..1). Never raises.
        """
        try:
            # If we can't call LLM, return heuristic
            if not self._can_call():
                return self._heuristic_score(liquidity_usd, fdv_usd, accel, pair)

            payload = {
                "symbol": symbol,
                "mint": mint,
                "liquidity_usd": liquidity_usd,
                "fdv_usd": fdv_usd,
                "accel": accel,
                "price_usd": (pair or {}).get("priceUsd"),
                "priceChange": (pair or {}).get("priceChange", {}),
                "volume": (pair or {}).get("volume", {}),
                "txns": (pair or {}).get("txns", {}),
            }

            prompt = (
                "Given this Solana memecoin pair snapshot, output a SINGLE numeric score 0..1.\n"
                "Higher = better short-term trade candidate.\n\n"
                f"{json.dumps(payload, ensure_ascii=False)}"
            )

            # Groq first
            result = self._call_groq(prompt)
            if result:
                self.last_call = time.time()
                score = self._extract_score_from_text(result)
                if score is not None:
                    self.fail_streak = 0
                    return score

            # OpenRouter fallback
            result = self._call_openrouter(prompt)
            if result:
                self.last_call = time.time()
                score = self._extract_score_from_text(result)
                if score is not None:
                    self.fail_streak = 0
                    return score

            # If both fail, mark fail and return heuristic
            self.last_call = time.time()
            self.fail_streak += 1
            return self._heuristic_score(liquidity_usd, fdv_usd, accel, pair)

        except Exception as e:
            # Hard fail-safe: never crash scanner
            log.exception("LLMScorer.score_token error: %s", e)
            return self._heuristic_score(liquidity_usd, fdv_usd, accel, pair)