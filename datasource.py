import time
import requests
import logging
from typing import Dict, Any, Optional

log = logging.getLogger("memecoinsnipa.datasource")

DEX_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/"


class DexScreenerClient:
    """
    Price/liquidity/volume fetch with caching.

    get_token(mint) returns a normalized dict:
      price, market_cap, liquidity, volume_24h, volume_1h, volume_5m,
      price_change_24h, price_change_1h, price_change_5m, url, dex, pair_address
    """

    def __init__(self, cache_seconds: int = 60):
        self.cache_seconds = int(cache_seconds)
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.cache_time: Dict[str, float] = {}

    def _fresh(self, key: str) -> bool:
        ts = self.cache_time.get(key)
        if not ts:
            return False
        return (time.time() - ts) < self.cache_seconds

    def _num(self, x, default=0.0) -> float:
        try:
            if x is None:
                return default
            return float(x)
        except Exception:
            return default

    def get_token(self, token_address: str) -> Dict[str, Any]:
        if not token_address:
            return {}

        # cache hit
        if token_address in self.cache and self._fresh(token_address):
            return self.cache[token_address]

        try:
            r = requests.get(f"{DEX_TOKEN_URL}{token_address}", timeout=15)
            if r.status_code != 200:
                log.warning(f"Dex token fetch HTTP {r.status_code} for {token_address}")
                return {}

            data = r.json()
            pairs = data.get("pairs") or []
            if not pairs:
                return {}

            # choose best liquidity pair
            best = max(pairs, key=lambda x: self._num((x.get("liquidity") or {}).get("usd"), 0.0))

            vol = best.get("volume") or {}
            chg = best.get("priceChange") or {}

            out = {
                "price": self._num(best.get("priceUsd"), 0.0),
                "market_cap": self._num(best.get("fdv"), 0.0),
                "liquidity": self._num((best.get("liquidity") or {}).get("usd"), 0.0),

                "volume_24h": self._num(vol.get("h24"), 0.0),
                "volume_1h": self._num(vol.get("h1"), 0.0),
                "volume_5m": self._num(vol.get("m5"), 0.0),

                "price_change_24h": self._num(chg.get("h24"), 0.0),
                "price_change_1h": self._num(chg.get("h1"), 0.0),
                "price_change_5m": self._num(chg.get("m5"), 0.0),

                "dex": best.get("dexId"),
                "pair_address": best.get("pairAddress"),
                "url": best.get("url"),
            }

            self.cache[token_address] = out
            self.cache_time[token_address] = time.time()
            return out

        except Exception as e:
            log.warning(f"Dex token fetch failed for {token_address}: {e}")
            return {}

    def get_best_pair_raw(self, token_address: str) -> Optional[Dict[str, Any]]:
        """
        Returns the raw best pair object (DexScreener response format) for advanced logic.
        """
        try:
            r = requests.get(f"{DEX_TOKEN_URL}{token_address}", timeout=15)
            if r.status_code != 200:
                return None
            data = r.json()
            pairs = data.get("pairs") or []
            if not pairs:
                return None
            return max(pairs, key=lambda x: self._num((x.get("liquidity") or {}).get("usd"), 0.0))
        except Exception:
            return None