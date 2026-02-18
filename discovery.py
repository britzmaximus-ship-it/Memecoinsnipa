import time
import logging
import requests
from typing import List, Dict, Any

log = logging.getLogger("memecoinsnipa.discovery")

SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"

# Multiple queries -> much larger candidate pool
QUERIES = [
    "pump",
    "sol",
    "raydium",
    "bonk",
    "meme",
    "moon",
    "dog",
    "cat",
    "pepe",
]

def _num(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default

def _int(x, default=0) -> int:
    try:
        if x is None:
            return default
        return int(x)
    except Exception:
        return default

def _age_minutes(pair_created_at_ms) -> int:
    if not pair_created_at_ms:
        return 10**9
    try:
        return int(max(0, (time.time() * 1000 - int(pair_created_at_ms)) / 60000))
    except Exception:
        return 10**9

def _pair_to_candidate(p: Dict[str, Any]) -> Dict[str, Any]:
    base = p.get("baseToken") or {}
    mint = base.get("address") or ""
    symbol = base.get("symbol") or base.get("name") or (mint[:6] if mint else "UNK")

    age_min = _age_minutes(p.get("pairCreatedAt"))

    liq = _num((p.get("liquidity") or {}).get("usd"), 0.0)
    mc = _num(p.get("fdv"), 0.0)

    vol = p.get("volume") or {}
    vol_5m = _num(vol.get("m5"), 0.0)
    vol_1h = _num(vol.get("h1"), 0.0)
    vol_24h = _num(vol.get("h24"), 0.0)

    txns = p.get("txns") or {}
    h1 = txns.get("h1") or {}
    buys_1h = _int(h1.get("buys"), 0)
    sells_1h = _int(h1.get("sells"), 0)

    chg = p.get("priceChange") or {}
    chg_5m = _num(chg.get("m5"), 0.0)
    chg_1h = _num(chg.get("h1"), 0.0)

    liq_to_mc = (liq / mc) if mc > 0 else 0.0
    vol_accel = (vol_5m * 12.0) / max(1.0, vol_1h)

    return {
        "mint": mint,
        "symbol": symbol,
        "age_min": age_min,
        "liq": liq,
        "mc": mc,
        "liq_to_mc": liq_to_mc,
        "vol_5m": vol_5m,
        "vol_1h": vol_1h,
        "vol_24h": vol_24h,
        "buys_1h": buys_1h,
        "sells_1h": sells_1h,
        "chg_5m": chg_5m,
        "chg_1h": chg_1h,
        "vol_accel": vol_accel,
        "pair": p.get("pairAddress"),
        "url": p.get("url"),
        "dex": p.get("dexId"),
        "chain": (p.get("chainId") or "").lower(),
    }

def discover_solana_candidates(limit: int = 200) -> List[Dict[str, Any]]:
    """
    Multi-query discovery for Solana pairs to avoid tiny result sets.
    """
    all_pairs: List[Dict[str, Any]] = []

    for q in QUERIES:
        try:
            r = requests.get(SEARCH_URL, params={"q": q}, timeout=20)
            if r.status_code != 200:
                continue
            data = r.json()
            pairs = data.get("pairs") or []
            all_pairs.extend(pairs)
        except Exception:
            continue

    sol_pairs = [p for p in all_pairs if (p.get("chainId") or "").lower() == "solana"]

    seen = set()
    candidates: List[Dict[str, Any]] = []
    for p in sol_pairs:
        c = _pair_to_candidate(p)
        mint = c.get("mint")
        if not mint or mint in seen:
            continue
        seen.add(mint)
        candidates.append(c)

    # Rank for early + moving
    def rank(c: Dict[str, Any]) -> float:
        age = int(c.get("age_min", 10**9))
        accel = _num(c.get("vol_accel"), 0.0)
        liq = _num(c.get("liq"), 0.0)
        chg5 = _num(c.get("chg_5m"), 0.0)

        s = 0.0
        # Age sweet spot
        if 3 <= age <= 360:
            s += 2.0
        elif age < 3:
            s += 0.2
        elif age <= 720:
            s += 0.8
        else:
            s -= 0.4

        # Acceleration
        if accel >= 2.0:
            s += 2.0
        elif accel >= 1.2:
            s += 1.0
        elif accel >= 0.8:
            s += 0.2
        else:
            s -= 0.5

        # Liquidity
        if liq >= 100_000:
            s += 1.2
        elif liq >= 50_000:
            s += 0.8
        elif liq >= 8_000:
            s += 0.3
        else:
            s -= 0.9

        # Avoid ultra-late candles
        if chg5 >= 80:
            s -= 0.7

        return s

    candidates.sort(key=rank, reverse=True)
    candidates = candidates[: max(30, min(int(limit), 500))]

    log.info("Discovery: queries=%d sol_pairs=%d candidates=%d", len(QUERIES), len(sol_pairs), len(candidates))
    return candidates