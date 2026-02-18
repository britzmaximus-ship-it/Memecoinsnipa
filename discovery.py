"""
discovery.py - Automatic token discovery (Solana) for Memecoinsnipa

Uses Birdeye "Trending tokens" endpoint to discover candidates.

Endpoint: https://public-api.birdeye.so/defi/token_trending

Env vars:
- BIRDEYE_API_KEY (required)
- DISCOVERY_LIMIT (default 20, max 20)
- DISCOVERY_SORT_BY (rank|liquidity|volume24hUSD) default volume24hUSD
- DISCOVERY_SORT_TYPE (asc|desc) default desc
- DISCOVERY_MIN_LIQUIDITY_USD (default 50000)
- DISCOVERY_MIN_VOLUME24H_USD (default 200000)
- DISCOVERY_EXCLUDE_SYMBOLS (comma-separated, optional)
"""

from __future__ import annotations

import os
import time
import logging
from typing import Dict, List, Optional

import requests

log = logging.getLogger("discovery")

BIRDEYE_TRENDING_URL = "https://public-api.birdeye.so/defi/token_trending"


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env_str(name, str(default)))
    except Exception:
        return default


def _headers() -> Dict[str, str]:
    key = _env_str("BIRDEYE_API_KEY")
    if not key:
        raise EnvironmentError("BIRDEYE_API_KEY is missing")
    return {"accept": "application/json", "X-API-KEY": key, "x-chain": "solana"}


def _excluded_symbols() -> set:
    raw = _env_str("DISCOVERY_EXCLUDE_SYMBOLS", "")
    if not raw:
        return set()
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


def discover_tokens(limit: Optional[int] = None) -> List[str]:
    """Return a list of token mint addresses (strings)."""
    lim = limit if limit is not None else _env_int("DISCOVERY_LIMIT", 20)
    lim = max(1, min(20, lim))

    sort_by = _env_str("DISCOVERY_SORT_BY", "volume24hUSD") or "volume24hUSD"
    sort_type = _env_str("DISCOVERY_SORT_TYPE", "desc") or "desc"

    min_liq = _env_float("DISCOVERY_MIN_LIQUIDITY_USD", 50_000.0)
    min_vol = _env_float("DISCOVERY_MIN_VOLUME24H_USD", 200_000.0)

    params = {"sort_by": sort_by, "sort_type": sort_type, "offset": 0, "limit": lim}

    t0 = time.time()
    resp = requests.get(BIRDEYE_TRENDING_URL, params=params, headers=_headers(), timeout=20)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success"):
        raise RuntimeError(f"Birdeye trending returned success=false: {str(data)[:300]}")

    tokens = (data.get("data") or {}).get("tokens") or []
    excl = _excluded_symbols()

    mints: List[str] = []
    for t in tokens:
        try:
            mint = t.get("address")
            sym = (t.get("symbol") or "").upper()
            liq = float(t.get("liquidity") or 0.0)
            vol = float(t.get("volume24hUSD") or 0.0)

            if not mint:
                continue
            if sym and sym in excl:
                continue
            if liq < min_liq:
                continue
            if vol < min_vol:
                continue

            mints.append(mint)
        except Exception:
            continue

    log.info(
        "Discovery: fetched %d trending tokens (limit=%d) -> %d passed filters in %.2fs",
        len(tokens),
        lim,
        len(mints),
        time.time() - t0,
    )
    return mints
