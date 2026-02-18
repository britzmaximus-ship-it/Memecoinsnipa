import time
import logging
import requests
from typing import List, Dict, Any

log = logging.getLogger("memecoinsnipa.discovery")

SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"


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

    # 5m pace vs 1h total. If vol_5m is strong relative to vol_1h, accel > ~1
    # multiply vol_5m by 12 to estimate an hourly pace.
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


def discover_solana_candidates(limit: int = 80) -> List[Dict[str, Any]]:
    """
    Auto-discovery from DexScreener.

    We fetch a broad set of Solana pairs and then your scanner will apply stricter
    filters + scoring + paper/live decisions.

    Returns: list of candidate dicts.
    """
    # DexScreener doesn't expose a perfect "new pairs feed", so we do a broad query
    # and then aggressively filter + rank.
    r = requests.get(SEARCH_URL, params={"q": "solana"}, timeout=20)
    r.raise_for_status()
    data = r.json()
    pairs = data.get("pairs") or []

    sol_pairs = [p for p in pairs if (p.get("chainId") or "").lower() == "solana"]

    # Convert + dedupe by mint
    seen = set()
    candidates: List[Dict[str, Any]] = []
    for p in sol_pairs:
        c = _pair_to_candidate(p)
        mint = c.get("mint")
        if not mint or mint in seen:
            continue
        seen.add(mint)
        candidates.append(c)

    # Light ranking here: combine "newness sweet spot" + volume acceleration
    def discovery_rank(c: Dict[str, Any]) -> float:
        age = c["age_min"]
        accel = c["vol_accel"]
        liq = c["liq"]
        chg5 = c["chg_5m"]

        score = 0.0

        # Age sweet spot: prefer 15m to 6h for aggressive runs
        if 15 <= age <= 360:
            score += 2.0
        elif age < 15:
            score += 0.3  # too new = high rug rate
        elif 360 < age <= 720:
            score += 0.8
        else:
            score -= 0.5

        # Volume acceleration: prefer current 5m pace > 1h average
        if accel >= 2.0:
            score += 2.0
        elif accel >= 1.2:
            score += 1.0
        elif accel >= 0.8:
            score += 0.3
        else:
            score -= 0.5

        # Liquidity sanity: higher liq survives swings
        if liq >= 100_000:
            score += 1.2
        elif liq >= 50_000:
            score += 0.8
        elif liq >= 20_000:
            score += 0.3
        else:
            score -= 0.8

        # Avoid already-mega candles in 5m (often late)
        if chg5 >= 60:
            score -= 0.8

        return score

    candidates.sort(key=discovery_rank, reverse=True)
    candidates = candidates[: max(20, min(limit, 200))]

    log.info("Discovery: %d candidates after dedupe+rank", len(candidates))
    return candidates