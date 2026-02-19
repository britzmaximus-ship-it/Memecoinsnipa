import time
import logging
import requests
import re
from typing import Optional, Dict, Any

from utils import env_str, env_int

log = logging.getLogger("memecoinsnipa.llm")


# ============================================================
# CORE LLM ENGINE
# ============================================================

class LLM:
    """
    Groq-first with cooldown protection.
    Optional OpenRouter fallback.
    Never raises — always returns None on failure.
    """

    def __init__(self):
        self.groq_key = env_str("GROQ_API_KEY", "")
        self.openrouter_key = env_str("OPENROUTER_API_KEY", "")

        self.groq_model = env_str("GROQ_MODEL", "llama-3.3-70b-versatile")
        self.openrouter_model = env_str("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct")

        self.groq_cooldown = env_int("GROQ_COOLDOWN_SECONDS", 600)
        self.min_seconds_between = env_int("LLM_MIN_SECONDS_BETWEEN_CALLS", 300)

        self.last_call = 0
        self.cooldown_until = 0

    # --------------------------------------------------------

    def _can_call(self) -> bool:
        now = time.time()
        if now < self.cooldown_until:
            return False
        if now - self.last_call < self.min_seconds_between:
            return False
        return True

    # --------------------------------------------------------

    def _call_groq(self, prompt: str) -> Optional[str]:
        if not self.groq_key:
            return None

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.groq_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.groq_model,
            "messages": [
                {"role": "system", "content": "You are a memecoin trading analyst. Respond with a numeric score between 0 and 1 only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 50,
        }

        try:
            r = requests.post(url, headers=headers, json=payload, timeout=20)

            if r.status_code == 429:
                self.cooldown_until = time.time() + self.groq_cooldown
                log.warning(f"Groq rate limited. Cooldown {self.groq_cooldown}s")
                return None

            if r.status_code != 200:
                log.warning(f"Groq failed {r.status_code}: {r.text[:200]}")
                return None

            return r.json()["choices"][0]["message"]["content"]

        except Exception as e:
            log.warning(f"Groq exception: {e}")
            return None

    # --------------------------------------------------------

    def _call_openrouter(self, prompt: str) -> Optional[str]:
        if not self.openrouter_key:
            return None

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.openrouter_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.openrouter_model,
            "messages": [
                {"role": "system", "content": "You are a memecoin trading analyst. Respond with a numeric score between 0 and 1 only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 50,
        }

        try:
            r = requests.post(url, headers=headers, json=payload, timeout=25)

            if r.status_code == 429:
                log.warning("OpenRouter rate limited.")
                return None

            if r.status_code != 200:
                log.warning(f"OpenRouter failed {r.status_code}: {r.text[:200]}")
                return None

            return r.json()["choices"][0]["message"]["content"]

        except Exception as e:
            log.warning(f"OpenRouter exception: {e}")
            return None

    # --------------------------------------------------------

    def analyze(self, payload: Dict[str, Any]) -> Optional[str]:
        if not self._can_call():
            return None

        prompt = f"Token data:\n{payload}\n\nReturn only a score between 0 and 1."

        # Try Groq
        result = self._call_groq(prompt)
        if result:
            self.last_call = time.time()
            return result

        # Fallback
        result = self._call_openrouter(prompt)
        if result:
            self.last_call = time.time()
            return result

        return None


# ============================================================
# COMPATIBILITY SCORER (Scanner expects this)
# ============================================================

class LLMScorer:
    """
    Wrapper so scanner.py can call:
        score = llm.score_token(...)
    Always returns float.
    Never crashes.
    """

    def __init__(self):
        self.llm = LLM()

    def score_token(
        self,
        symbol: str,
        mint: str,
        liquidity_usd: float,
        fdv_usd: float,
        accel: float,
        pair: dict,
    ) -> float:

        payload = {
            "symbol": symbol,
            "liquidity_usd": liquidity_usd,
            "fdv_usd": fdv_usd,
            "accel": accel,
        }

        try:
            text = self.llm.analyze(payload)

            if not text:
                return 0.0

            # Extract first numeric value from response
            nums = re.findall(r"(\d+(?:\.\d+)?)", text)
            if not nums:
                return 0.0

            val = float(nums[0])

            # Normalize common formats
            if val > 10:
                val = val / 100.0
            elif val > 1.5:
                val = val / 10.0

            return max(0.0, min(val, 1.0))

        except Exception as e:
            log.warning(f"LLM scoring error: {e}")
            return 0.0