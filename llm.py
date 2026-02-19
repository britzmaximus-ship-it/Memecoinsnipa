import time
import logging
import requests
import re
from typing import Optional, Dict, Any

# If you have utils.py, we use it. If not, we fallback safely.
try:
    from utils import env_str, env_int
except Exception:
    import os

    def env_str(name: str, default: str = "") -> str:
        v = os.getenv(name)
        return default if v is None else str(v).strip()

    def env_int(name: str, default: int = 0) -> int:
        try:
            return int(env_str(name, str(default)))
        except Exception:
            return default


log = logging.getLogger("memecoinsnipa.llm")


class LLM:
    """
    Groq-first with cooldown protection.
    (Optionally supports OpenRouter if you set OPENROUTER_API_KEY)

    Exports: class LLM
    """

    def __init__(self):
        self.groq_key = env_str("GROQ_API_KEY", "")
        self.openrouter_key = env_str("OPENROUTER_API_KEY", "")

        self.groq_model = env_str("GROQ_MODEL", "llama-3.3-70b-versatile")
        self.openrouter_model = env_str("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct")

        self.groq_cooldown = env_int("GROQ_COOLDOWN_SECONDS", 600)
        self.min_seconds_between = env_int("LLM_MIN_SECONDS_BETWEEN_CALLS", 300)

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
            log.warning("Groq rate limited. Cooldown %ss", self.groq_cooldown)
            return None

        if r.status_code != 200:
            log.warning("Groq failed %s: %s", r.status_code, r.text[:200])
            return None

        return r.json()["choices"][0]["message"]["content"]

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

        return r.json()["choices"][0]["message"]["content"]

    def analyze(self, payload: Dict[str, Any]) -> Optional[str]:
        """
        Payload is the scan summary we pass to the LLM.
        """
        if not self._can_call():
            return None

        prompt = (
            "Analyze these candidates and pick the best 1-3 with short reasoning.\n\n"
            f"{payload}\n\n"
            "Return your answer in plain text."
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
    Backwards-compatible wrapper that many versions of your scanner expect.

    Exports: class LLMScorer with:
      - score_token(...)
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
        pair: Optional[Dict[str, Any]] = None,
    ) -> float:
        """
        Returns a numeric score in [0,1]ish.
        - If LLM not available, returns a simple heuristic score.
        - NEVER raises (so scanner won't crash).
        """
        try:
            payload = {
                "symbol": symbol,
                "mint": mint,
                "liquidity_usd": liquidity_usd,
                "fdv_usd": fdv_usd,
                "accel": accel,
                "pair_url": (pair or {}).get("url") if isinstance(pair, dict) else None,
            }

            text = self.llm.analyze(payload)
            if not text:
                return self._heuristic(liquidity_usd, fdv_usd, accel)

            # Try to extract a number like "score: 0.18" or "0.22"
            m = re.search(r"score[^0-9]*([0-9]*\.?[0-9]+)", text, re.IGNORECASE)
            if not m:
                m = re.search(r"([0-9]*\.?[0-9]+)", text)
            if not m:
                return self._heuristic(liquidity_usd, fdv_usd, accel)

            val = float(m.group(1))
            # clamp to sane range
            if val < 0:
                val = 0.0
            if val > 1.0:
                # some LLMs might output 1-10, normalize roughly
                val = min(1.0, val / 10.0)
            return val
        except Exception:
            return self._heuristic(liquidity_usd, fdv_usd, accel)

    @staticmethod
    def _heuristic(liquidity_usd: float, fdv_usd: float, accel: float) -> float:
        # Simple, stable fallback that won't crash:
        liq_score = min(1.0, max(0.0, liquidity_usd / 50000.0))  # 50k liq ~ 1.0
        fdv_score = 1.0 if fdv_usd <= 0 else min(1.0, max(0.0, 1.0 - (fdv_usd / 5_000_000.0)))  # under 5M better
        accel_score = min(1.0, max(0.0, accel / 2.0))  # accel 2.0 ~ 1.0
        return 0.45 * accel_score + 0.35 * liq_score + 0.20 * fdv_score