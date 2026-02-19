"""
llm.py

Safe LLM wrapper + scorer for Memecoinsnipa.

Exports:
- class LLM         (analyze(payload) -> text or None)
- class LLMScorer   (score_token(...) -> float 0..1)

Design goals:
- NEVER crash the scanner loop (always return something)
- Groq-first, OpenRouter fallback
- Cooldown + min-time-between-calls throttling
- If LLM unavailable, return a heuristic score
"""

import os
import re
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
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


class LLM:
    """
    Groq-first with cooldown protection.
    Optionally supports OpenRouter if OPENROUTER_API_KEY is set.
    """

    def __init__(self):
        self.groq_key = _env_str("GROQ_API_KEY", "")
        self.openrouter_key = _env_str("OPENROUTER_API_KEY", "")

        self.groq_model = _env_str("GROQ_MODEL", "llama-3.3-70b-versatile")
        self.openrouter_model = _env_str("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct")

        self.groq_cooldown = _env_int("GROQ_COOLDOWN_SECONDS", 600)
        self.min_seconds_between = _env_int("LLM_MIN_SECONDS_BETWEEN_CALLS", 300)

        self.last_call = 0.0
        self.cooldown_until = 0.0

    def _can_call(self) -> bool:
        now = time.time()
        if now < self.cooldown_until:
            return False
        if now - self.last_call < self.min_seconds_between:
            return False
        return True

    def _call_groq(self, prompt: str) -> Optional[str]:
        if not self.groq_key:
            return None

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.groq_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.groq_model,
            "messages": [
                {"role": "system", "content": "You are a memecoin trading analyst. Be concise."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
            "max_tokens": 400,
        }

        r = requests.post(url, headers=headers, json=payload, timeout=20)

        if r.status_code == 429:
            self.cooldown_until = time.time() + self.groq_cooldown
            log.warning("Groq rate limited. Cooldown=%ss", self.groq_cooldown)
            return None

        if r.status_code != 200:
            log.warning("Groq failed %s: %s", r.status_code, r.text[:200])
            return None

        try:
            return r.json()["choices"][0]["message"]["content"]
        except Exception:
            return None

    def _call_openrouter(self, prompt: str) -> Optional[str]:
        if not self.openrouter_key:
            return None

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.openrouter_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.openrouter_model,
            "messages": [
                {"role": "system", "content": "You are a memecoin trading analyst. Be concise."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
            "max_tokens": 400,
        }

        r = requests.post(url, headers=headers, json=payload, timeout=25)

        if r.status_code == 429:
            log.warning("OpenRouter rate limited.")
            return None

        if r.status_code != 200:
            log.warning("OpenRouter failed %s: %s", r.status_code, r.text[:200])
            return None

        try:
            return r.json()["choices"][0]["message"]["content"]
        except Exception:
            return None

    def analyze(self, payload: Dict[str, Any]) -> Optional[str]:
        """
        Payload is the scan summary we pass to the LLM.
        """
        if not self._can_call():
            return None

        prompt = (
            "Analyze these candidates and pick the best 1-3 with short reasoning.\n\n"
            f"{json.dumps(payload, ensure_ascii=False)}\n\n"
            "Also include a single line: SCORE=<number between 0 and 1> for the best candidate."
        )

        # Groq first
        result = self._call_groq(prompt)
        if result:
            self.last_call = time.time()
            return result

        # fallback
        result = self._call_openrouter(prompt)
        if result:
            self.last_call = time.time()
            return result

        return None


class LLMScorer:
    """
    Safe scorer wrapper. Always returns a float score (0..1).
    If LLM unavailable, returns a heuristic score.
    """

    def __init__(self):
        self.llm = LLM()

        # heuristic knobs
        self.heur_liq_ref = _env_float("HEUR_LIQ_REF", 15000.0)
        self.heur_fdv_ref = _env_float("HEUR_FDV_REF", 1000000.0)

    def _parse_score_0_1(self, text: str) -> Optional[float]:
        if not text:
            return None

        # Look for explicit SCORE=
        m = re.search(r"SCORE\s*=\s*([01](?:\.\d+)?)", text, re.IGNORECASE)
        if m:
            try:
                v = float(m.group(1))
                return max(0.0, min(1.0, v))
            except Exception:
                pass

        # fallback: first float between 0 and 1 in text
        m2 = re.search(r"\b0\.\d+\b|\b1\.0+\b|\b1\b", text)
        if m2:
            try:
                v = float(m2.group(0))
                return max(0.0, min(1.0, v))
            except Exception:
                pass

        return None

    def _heuristic(self, liquidity_usd: float, fdv_usd: float, accel: float) -> float:
        # simple bounded heuristic
        liq_part = min(1.0, max(0.0, liquidity_usd / max(self.heur_liq_ref, 1.0)))
        fdv_part = 1.0 - min(1.0, max(0.0, fdv_usd / max(self.heur_fdv_ref, 1.0))) if fdv_usd > 0 else 0.6
        accel_part = min(1.0, max(0.0, accel / 2.0))
        score = (0.45 * accel_part) + (0.35 * liq_part) + (0.20 * fdv_part)
        return max(0.0, min(1.0, score))

    def score_token(
        self,
        symbol: str,
        mint: str,
        liquidity_usd: float,
        fdv_usd: float,
        accel: float,
        pair: Dict[str, Any],
    ) -> float:
        try:
            payload = {
                "symbol": symbol,
                "mint": mint,
                "liquidity_usd": liquidity_usd,
                "fdv_usd": fdv_usd,
                "accel": accel,
                "pair_hint": {
                    "url": pair.get("url"),
                    "priceChange_h1": (pair.get("priceChange") or {}).get("h1"),
                    "volume_h1": (pair.get("volume") or {}).get("h1"),
                    "buys_h1": (pair.get("txns") or {}).get("h1", {}).get("buys"),
                    "sells_h1": (pair.get("txns") or {}).get("h1", {}).get("sells"),
                },
            }

            text = self.llm.analyze(payload)
            parsed = self._parse_score_0_1(text or "")
            if parsed is not None:
                return parsed

            return self._heuristic(liquidity_usd, fdv_usd, accel)

        except Exception as e:
            log.warning("LLMScorer error: %s", e)
            return self._heuristic(liquidity_usd, fdv_usd, accel)