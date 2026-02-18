import time
import requests
import logging
from typing import Dict, List

log = logging.getLogger("memecoinsnipa.datasource")

DEX_API = "https://api.dexscreener.com/latest/dex/tokens/"

class DexScreenerClient:
    def __init__(self, cache_seconds: int = 120):
        self.cache_seconds = cache_seconds
        self.cache: Dict[str, Dict] = {}
        self.cache_time: Dict[str, float] = {}

    def get_token(self, token_address: str) -> Dict:
        now = time.time()

        # Return cached if fresh
        if token_address in self.cache:
            if now - self.cache_time[token_address] < self.cache_seconds:
                return self.cache[token_address]

        try:
            r = requests.get(f"{DEX_API}{token_address}", timeout=15)
            if r.status_code != 200:
                log.warning(f"Dex API error {r.status_code} for {token_address}")
                return {}

            data = r.json()
            pairs = data.get("pairs", [])
            if not pairs:
                return {}

            # Use highest liquidity pair
            best = max(
                pairs,
                key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0)
            )

            result = {
                "price": float(best.get("priceUsd") or 0),
                "market_cap": float(best.get("fdv") or 0),
                "liquidity": float(best.get("liquidity", {}).get("usd") or 0),
                "volume_24h": float(best.get("volume", {}).get("h24") or 0),
                "price_change_24h": float(best.get("priceChange", {}).get("h24") or 0),
                "dex": best.get("dexId"),
                "pair_address": best.get("pairAddress"),
                "url": best.get("url")
            }

            self.cache[token_address] = result
            self.cache_time[token_address] = now

            return result

        except Exception as e:
            log.warning(f"Dex fetch failed for {token_address}: {e}")
            return {}

    def bulk_fetch(self, token_addresses: List[str]) -> Dict[str, Dict]:
        results = {}
        for t in token_addresses:
            results[t] = self.get_token(t)
        return results
