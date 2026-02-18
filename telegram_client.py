import requests
import logging
from typing import Optional
from utils import jitter_sleep

log = logging.getLogger("memecoinsnipa.telegram")

class TelegramClient:
    def __init__(self, bot_token: str, user_id: str):
        self.bot_token = bot_token
        self.user_id = user_id

    def send(self, text: str, parse_mode: Optional[str] = None, retries: int = 3) -> bool:
        if not self.bot_token or not self.user_id:
            log.warning("Telegram not configured (missing token/user_id).")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.user_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode

        for attempt in range(1, retries + 1):
            try:
                r = requests.post(url, json=payload, timeout=15)
                if r.status_code == 200:
                    return True

                log.warning(
                    f"Telegram send failed (attempt {attempt}/{retries}) "
                    f"HTTP {r.status_code}: {r.text[:200]}"
                )

                # Retry on rate limits / transient server errors
                if r.status_code in (429, 500, 502, 503, 504):
                    jitter_sleep(2 * attempt, 0.3)
                    continue

                return False

            except Exception as e:
                log.warning(f"Telegram exception (attempt {attempt}/{retries}): {e}")
                jitter_sleep(2 * attempt, 0.3)

        return False
