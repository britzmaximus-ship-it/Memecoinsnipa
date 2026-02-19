import time
import logging
import json
import requests
from typing import Optional, Dict, Any

from utils import env_str, env_int, env_float

log = logging.getLogger("memecoinsnipa.llm")


class LLMScorer:
    """
    Safe scorer:
    - Returns a numeric score (float) always
    - Never raises up to scanner
    - Groq-first, but gracefully falls back to heuristic score
    """

    def __init__(self):
        self.groq_key = env_str("GROQ_API_KEY", "")
        self.groq_model = env_str("GROQ_MODEL", "llama-3.3-70b-versatile")

        self.groq_cooldown = env_int("GROQ_COOLDOWN_SECONDS", 600)
        self.min_seconds_between = env_int("LLM_MIN_SECONDS_BETWEEN_CALLS", 30)

        # safety: if Groq fails, we use heuristic weights
        self.w_accel = env_float("HEUR_W_ACCEL", 0.7)
        self.w_liq = env_float("HEUR_W_LIQ", 0.2)
        self.w_mc = env_float("HEUR_W_MC", 0.1)

        self.last_call = 0.0
        self.cooldown_until = 0.0

    def _can_call(self) -> bool:
        now = time.time()
        if now < self.cooldown_until:
            return False
        if now - self.last_call < self.min_seconds_between:
            return False
        return True

    def _heuristic_score(self, accel: float, liquidity_usd: float, fdv_usd: float) -> float:
        # Normalize roughly
        liq_norm = min(1.0, max(0.0, liquidity_usd / 20000.0))  # 20k+ considered strong
        mc_norm = 1.0
        if fdv_usd and fdv_usd > 0:
            mc_norm = max(0.0, 1.0 - min(1.0, fdv_usd / 5_000_000.0))  # lower mc = higher score
        accel_norm = min(2.0, max(0.0, accel)) / 2.0

        score = (self.w_accel * accel_norm) + (self.w_liq * liq_norm) + (self.w_mc * mc_norm)
        return float(max(0.0, min(1.0, score)))

    def _call_groq(self, prompt: str) -> Optional[str]:
        if not self.groq_key:
            return None

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.groq_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.groq_model,
            "messages": [
                {"role": "system", "content": "You are a memecoin trading analyst. Output ONLY valid JSON."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 250,
        }

        r = requests.post(url, headers=headers, json=payload, timeout=25)

        if r.status_code == 429:
            self.cooldown_until = time.time() + self.groq_cooldown
            log.warning("Groq rate limited. Cooldown %ss", self.groq_cooldown)
            return None

        if r.status_code != 200:
            log.warning("Groq failed %s: %s", r.status_code, r.text[:200])
            return None

        try:
            return r.json()["choices"][0]["message"]["content"]
        except Exception:
            return None

    def score_token(
        self,
        symbol: str,
        mint: str,
        liquidity_usd: float,
        fdv_usd: float,
        accel: float,
        pair: Dict[str, Any],
    ) -> float:
        """
        Return numeric score in [0..1].
        """
        # default fallback
        fallback = self._heuristic_score(accel, liquidity_usd, fdv_usd)

        if not self._can_call():
            return fallback

        # Keep prompt small + deterministic
        info = {
            "symbol": symbol,
            "mint": mint,
            "liquidity_usd": round(float(liquidity_usd or 0), 2),
            "fdv_usd": round(float(fdv_usd or 0), 2),
            "accel": round(float(accel or 0), 4),
            "priceChange_h1": (pair.get("priceChange") or {}).get("h1"),
            "volume_h1": (pair.get("volume") or {}).get("h1"),
            "txns_h1": (pair.get("txns") or {}).get("h1"),
        }

        prompt = (
            "Score this token for a short-term trade from 0.0 to 1.0.\n"
            "Return ONLY JSON like: {\"score\":0.37}\n\n"
            f"{json.dumps(info)}"
        )

        try:
            out = self._call_groq(prompt)
            if not out:
                return fallback

            # parse JSON safely even if extra text exists
            start = out.find("{")
            end = out.rfind("}")
            if start == -1 or end == -1 or end <= start:
                return fallback

            obj = json.loads(out[start : end + 1])
            s = obj.get("score", None)
            if isinstance(s, (int, float)):
                s = float(s)
                s = max(0.0, min(1.0, s))
                self.last_call = time.time()
                return s

            return fallback
        except Exception as e:
            log.warning("LLM score_token error: %s", e)
            return fallback