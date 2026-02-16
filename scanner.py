"""
scanner.py - Solana Memecoin Scanner v2.1

Self-learning memecoin scanner with paper trading engine.
Runs on GitHub Actions every 15 minutes, sends alerts via Telegram,
and persists learning state via playbook.json committed to git.
"""

import os
import re
import json
import time
import logging
import random
import requests
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from typing import Any, Optional, List, Dict, Tuple
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# CONSTANTS
# ============================================================

MAX_SCANS_PER_PICK = 12
MAX_TOKENS_PER_SCAN = 12
MIN_MARKET_CAP = 5000

MAX_OPEN_PAPER_TRADES = 3
MAX_PENDING_TRADES = 5
MIN_CONFIDENCE_FOR_TRADE = 7
PRICE_DUMP_REJECT_PCT = -15
BUY_PRESSURE_CONFIRM_MIN = 0.8
SLIPPAGE_MIN_PCT = 0.5
SLIPPAGE_MAX_PCT = 1.0
DEAD_TOKEN_SCAN_LIMIT = 8

RULE_REGEN_INTERVAL = 5
MIN_TRADES_FOR_RULES = 5
TOKEN_BLACKLIST_HOURS = 24

API_RATE_LIMIT_DELAY = 0.4
API_MAX_RETRIES = 2
API_TIMEOUT = 10
CACHE_TTL = 300  # 5 min

TELEGRAM_MSG_LIMIT = 4000
TELEGRAM_SEND_TIMEOUT = 10

MAX_TOKEN_NAME_LEN = 50

TRIM = {
    "tokens_seen": 200,
    "pick_history": 100,
    "active_picks": 20,
    "win_patterns": 30,
    "lose_patterns": 30,
    "strategy_rules": 20,
    "avoid_conditions": 20,
    "mistake_log": 20,
    "trade_memory": 50,
    "lessons": 50,
    "paper_trade_history": 100,
    "paper_trades": MAX_OPEN_PAPER_TRADES,
    "pending_paper_trades": MAX_PENDING_TRADES,
    "token_blacklist": 200,
}

MC_TIERS = [
    (100_000, "micro (<100k)"),
    (500_000, "small (100k-500k)"),
    (2_000_000, "mid (500k-2M)"),
    (5_000_000, "large (2M-5M)"),
]
MC_TIER_DEFAULT = "mega (5M+)"

VOL_SPIKE_TIERS = [
    (5.0, "extreme_spike"),
    (3.0, "high_spike"),
    (1.5, "moderate"),
]
VOL_SPIKE_DEFAULT = "normal"

PRESSURE_TIERS = [
    (2.0, "heavy_buying"),
    (1.5, "strong_buying"),
    (1.0, "balanced"),
]
PRESSURE_DEFAULT = "selling_pressure"

TRAILING_STOPS = [
    (400, 0.15),
    (200, 0.18),
    (100, 0.20),
    (50, 0.22),
]
TRAILING_STOP_DEFAULT = 0.25

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scanner.log", mode="a", encoding="utf-8"),
    ],
)
log = logging.getLogger("scanner")

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
USER_ID = os.environ.get("TELEGRAM_USER_ID", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
TAPI = f"https://api.telegram.org/bot{BOT_TOKEN}"

_missing_vars = [
    name for name, val in [
        ("TELEGRAM_BOT_TOKEN", BOT_TOKEN),
        ("TELEGRAM_USER_ID", USER_ID),
        ("GROQ_API_KEY", GROQ_KEY),
    ] if not val
]
if _missing_vars:
    raise EnvironmentError(f"Missing required environment variables: {', '.join(_missing_vars)}")

# ============================================================
# UTILITY FUNCTIONS
# ============================================================

_last_api_call_time: float = 0.0

def rate_limited_get(url: str, timeout: int = API_TIMEOUT) -> Optional[requests.Response]:
    global _last_api_call_time

    for attempt in range(API_MAX_RETRIES + 1):
        elapsed = time.time() - _last_api_call_time
        if elapsed < API_RATE_LIMIT_DELAY:
            time.sleep(API_RATE_LIMIT_DELAY - elapsed)

        try:
            _last_api_call_time = time.time()
            r = requests.get(url, timeout=timeout)

            if r.status_code == 429:
                wait = min(2 ** (attempt + 1), 10)
                log.warning(f"Rate limited on {url}, retrying in {wait}s")
                time.sleep(wait)
                continue

            if r.status_code >= 500 and attempt < API_MAX_RETRIES:
                log.warning(f"Server error {r.status_code} on {url}, retrying")
                time.sleep(1)
                continue

            return r

        except requests.exceptions.Timeout:
            log.warning(f"Timeout on {url} (attempt {attempt + 1}/{API_MAX_RETRIES + 1})")
        except requests.exceptions.ConnectionError:
            log.warning(f"Connection error on {url} (attempt {attempt + 1})")
        except requests.exceptions.RequestException as e:
            log.error(f"Request failed on {url}: {e}")
            break

    return None

def sanitize_for_prompt(text: str, max_length: int = MAX_TOKEN_NAME_LEN) -> str:
    if not text:
        return "?"
    cleaned = re.sub(r"(?i)(ignore|forget|disregard|override)\s+(all|previous|above|prior)", "", text)
    cleaned = re.sub(r"(?i)(system|assistant|user)\s*:", "", cleaned)
    cleaned = re.sub(r"```", "", cleaned)
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length] + "..."
    return cleaned or "?"

def extract_json_from_response(response: str) -> Optional[list | dict]:
    if not response:
        return None
    try:
        match = re.search(r"```json\s*(\[.*?\]|\{.*?\})\s*```", response, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        match = re.search(r"(\[.*\]|\{.*\})", response, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        log.warning(f"JSON parse error: {e}")
    return None

def safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default

def safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default

def classify_mc_tier(mc: float) -> str:
    for threshold, label in MC_TIERS:
        if mc < threshold:
            return label
    return MC_TIER_DEFAULT

def classify_descending(value: float, tiers: list[tuple[float, str]], default: str) -> str:
    for threshold, label in tiers:
        if value >= threshold:
            return label
    return default

def parse_price_value(text: str) -> Optional[float]:
    cleaned = text.replace(",", "")
    for pat in [
        r"\$\s*([0-9]*\.?[0-9]+(?:[eE][+-]?\d+)?)",
        r"([0-9]*\.?[0-9]+(?:[eE][+-]?\d+)?)",
    ]:
        m = re.search(pat, cleaned)
        if m:
            try:
                val = float(m.group(1))
                if val > 0:
                    return val
            except ValueError:
                continue
    return None

# ============================================================
# TOKEN BLACKLIST
# ============================================================

def is_blacklisted(pb: dict, contract: str) -> bool:
    blacklist = pb.get("token_blacklist", {})
    entry = blacklist.get(contract)
    if not entry:
        return False
    try:
        if datetime.fromisoformat(entry["expires"]) > datetime.now():
            return True
    except (ValueError, TypeError, KeyError):
        pass
    return False

def blacklist_token(pb: dict, contract: str, name: str, reason: str) -> None:
    blacklist = pb.setdefault("token_blacklist", {})
    if contract in blacklist:
        blacklist[contract]["expires"] = (datetime.fromisoformat(blacklist[contract]["expires"]) + timedelta(hours=TOKEN_BLACKLIST_HOURS)).isoformat()
        blacklist[contract]["reason"] += f"; {reason}"
        log.info(f"Extended blacklist for {name} ({contract[:12]}...): {reason}")
    else:
        expires = (datetime.now() + timedelta(hours=TOKEN_BLACKLIST_HOURS)).isoformat()
        blacklist[contract] = {
            "name": name,
            "reason": reason,
            "blacklisted_at": datetime.now().isoformat()[:16],
            "expires": expires,
        }
        log.info(f"Blacklisted {name} ({contract[:12]}...) for {TOKEN_BLACKLIST_HOURS}h: {reason}")

def clean_expired_blacklist(pb: dict) -> None:
    blacklist = pb.get("token_blacklist", {})
    now = datetime.now()
    expired = [
        addr for addr, entry in blacklist.items()
        if datetime.fromisoformat(entry.get("expires", "2000-01-01")) <= now
    ]
    for addr in expired:
        del blacklist[addr]
    if expired:
        log.info(f"Cleaned {len(expired)} expired blacklist entries")

# ============================================================
# SHARED DEX PAIR PARSER
# ============================================================

def _fetch_dex_pairs(contract: str) -> Optional[list[dict]]:
    r = rate_limited_get(f"https://api.dexscreener.com/tokens/v1/solana/{contract}")
    if r is None or r.status_code != 200:
        return None
    try:
        data = r.json()
        pairs = data if isinstance(data, list) else data.get("pairs", [])
        if not pairs:
            return None
        return sorted(
            [p for p in pairs if p],
            key=lambda x: safe_float(x.get("liquidity", {}).get("usd")),
            reverse=True,
        )
    except (json.JSONDecodeError, AttributeError) as e:
        log.warning(f"Failed to parse DexScreener response for {contract[:12]}...: {e}")
        return None

def parse_pair_data(pair: dict) -> dict:
    price = safe_float(pair.get("priceUsd"))
    mc = safe_float(pair.get("marketCap"))
    lq = safe_float(pair.get("liquidity", {}).get("usd"))

    vol = pair.get("volume", {})
    vol_1h = safe_float(vol.get("h1"))
    vol_24h = safe_float(vol.get("h24"))

    pc = pair.get("priceChange", {})

    tx_h1 = pair.get("txns", {}).get("h1", {})
    buys_1h = safe_int(tx_h1.get("buys"))
    sells_1h = safe_int(tx_h1.get("sells"))

    buy_ratio_1h = round(buys_1h / max(sells_1h, 1), 2)

    avg_hourly = vol_24h / 24 if vol_24h > 0 else 0
    vol_spike = round(vol_1h / max(avg_hourly, 1), 2) if avg_hourly > 0 else 0

    mc_tier = classify_mc_tier(mc)
    vol_tier = classify_descending(vol_spike, VOL_SPIKE_TIERS, VOL_SPIKE_DEFAULT)
    pressure_tier = classify_descending(buy_ratio_1h, PRESSURE_TIERS, PRESSURE_DEFAULT)

    dex_id = pair.get("dexId", "unknown")
    base_token = pair.get("baseToken", {})
    token_name = base_token.get("name", "?")
    token_symbol = base_token.get("symbol", "?")
    url = pair.get("url", "?")

    pair_created = pair.get("pairCreatedAt")
    hours_old = None
    is_new_tag = ""
    if pair_created:
        try:
            created_ts = int(pair_created) / 1000
            hours_old = round((datetime.now().timestamp() - created_ts) / 3600, 1)
            if hours_old < 1:
                is_new_tag = f" | NEW ({int(hours_old * 60)}min old)"
            elif hours_old < 24:
                is_new_tag = f" | NEW ({int(hours_old)}h old)"
        except (ValueError, TypeError):
            pass

    whale_signals = []
    if buy_ratio_1h >= 2.0:
        whale_signals.append("HEAVY buy pressure")
    elif buy_ratio_1h >= 1.5:
        whale_signals.append("Strong buy pressure")
    if vol_spike >= 3.0:
        whale_signals.append(f"Vol spike {vol_spike}x")

    return {
        "price": price,
        "market_cap": mc,
        "mc_tier": mc_tier,
        "liquidity": lq,
        "liq_to_mc_ratio": round(lq / max(mc, 1), 4),
        "volume_1h": vol_1h,
        "volume_24h": vol_24h,
        "vol_spike": vol_spike,
        "vol_tier": vol_tier,
        "buy_ratio_1h": buy_ratio_1h,
        "pressure_tier": pressure_tier,
        "price_change_1h": safe_float(pc.get("h1")),
        "price_change_24h": safe_float(pc.get("h24")),
        "dex": dex_id,
        "hours_old": hours_old,
        "token_name": token_name,
        "token_symbol": token_symbol,
        "url": url,
        "whale_signals": whale_signals,
        "is_new_tag": is_new_tag,
    }

def get_token_price(contract: str) -> Optional[float]:
    pairs = _fetch_dex_pairs(contract)
    if not pairs:
        return None
    return safe_float(pairs[0].get("priceUsd")) or None

def get_token_market_data(contract: str) -> Optional[dict]:
    pairs = _fetch_dex_pairs(contract)
    if not pairs:
        return None
    return parse_pair_data(pairs[0])

def format_token_for_prompt(parsed: dict, contract: str, source: str) -> str:
    name = sanitize_for_prompt(parsed["token_name"])
    symbol = sanitize_for_prompt(parsed["token_symbol"], max_length=20)
    whale_tag = " | WHALE: " + ", ".join(parsed["whale_signals"]) if parsed["whale_signals"] else ""

    return (
        f"Token: {name} ({symbol})\n"
        f"Contract: {contract}\n"
        f"DEX: {parsed['dex']}{parsed['is_new_tag']} | Found via: {source}\n"
        f"Price: ${parsed['price']}\n"
        f"Price Change 1h: {parsed['price_change_1h']}% | 24h: {parsed['price_change_24h']}%\n"
        f"Volume 1h: ${parsed['volume_1h']} | 24h: ${parsed['volume_24h']}\n"
        f"Volume Spike: {parsed['vol_spike']}x\n"
        f"Buy/Sell Ratio 1h: {parsed['buy_ratio_1h']}\n"
        f"Liquidity: ${parsed['liquidity']:,.0f} | Market Cap: ${parsed['market_cap']:,.0f}\n"
        f"URL: {parsed['url']}"
        f"{whale_tag}"
    )

# ============================================================
# TELEGRAM
# ============================================================

def send_msg(text: str, parse_mode: str = "MarkdownV2") -> None:
    for i in range(0, len(text), TELEGRAM_MSG_LIMIT):
        chunk = text[i : i + TELEGRAM_MSG_LIMIT]
        try:
            r = requests.post(
                f"{TAPI}/sendMessage",
                json={"chat_id": USER_ID, "text": chunk, "parse_mode": parse_mode},
                timeout=TELEGRAM_SEND_TIMEOUT,
            )
            if r.status_code != 200:
                log.warning(f"Telegram send failed (HTTP {r.status_code}): {r.text[:200]}")
        except requests.exceptions.RequestException as e:
            log.error(f"Telegram send error: {e}")

# ============================================================
# DATA SOURCES
# ============================================================

def fetch_boosted_tokens() -> list[dict]:
    r = rate_limited_get("https://api.dexscreener.com/token-boosts/top/v1")
    if r is None or r.status_code != 200:
        return []
    try:
        tokens = r.json()[:30]
        return [{"addr": t.get("tokenAddress", ""), "source": "boosted"} for t in tokens if t.get("chainId") == "solana"]
    except:
        return []

def fetch_latest_profiles() -> list[dict]:
    r = rate_limited_get("https://api.dexscreener.com/token-profiles/latest/v1")
    if r is None or r.status_code != 200:
        return []
    try:
        tokens = r.json()[:30]
        return [{"addr": t.get("tokenAddress", ""), "source": "profile"} for t in tokens if t.get("chainId") == "solana"]
    except:
        return []

def fetch_new_pairs() -> list[dict]:
    r = rate_limited_get("https://api.dexscreener.com/token-boosts/latest/v1")
    if r is None or r.status_code != 200:
        return []
    try:
        tokens = r.json()[:30]
        return [{"addr": t.get("tokenAddress", ""), "source": "new/pumpfun"} for t in tokens if t.get("chainId") == "solana"]
    except:
        return []

def fetch_scan_data(token_list: list[dict]) -> list[str]:
    results = []
    seen: set[str] = set()

    def fetch_one(item: dict) -> Optional[str]:
        addr = item["addr"]
        source = item["source"]
        if addr in seen or not addr:
            return None
        seen.add(addr)

        pairs = _fetch_dex_pairs(addr)
        if not pairs:
            return None

        parsed = parse_pair_data(pairs[0])
        if parsed["market_cap"] < MIN_MARKET_CAP:
            return None

        return format_token_for_prompt(parsed, addr, source)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_one, item) for item in token_list]
        for future in as_completed(futures):
            res = future.result()
            if res:
                results.append(res)
            if len(results) >= MAX_TOKENS_PER_SCAN:
                break

    return results

def run_full_scan() -> tuple[list[str], dict]:
    boosted = fetch_boosted_tokens()
    profiles = fetch_latest_profiles()
    new_pairs = fetch_new_pairs()

    all_tokens = boosted + profiles + new_pairs
    seen: set[str] = set()
    unique = []
    for item in all_tokens:
        if item["addr"] not in seen and item["addr"]:
            seen.add(item["addr"])
            unique.append(item)

    if not unique:
        return [], {"boosted": 0, "profiles": 0, "new_pairs": 0}

    tokens = fetch_scan_data(unique)
    stats = {
        "boosted": len(boosted),
        "profiles": len(profiles),
        "new_pairs": len(new_pairs),
        "unique": len(seen),
        "with_data": len(tokens),
    }
    return tokens, stats

# ============================================================
# MAIN
# ============================================================

def main() -> None:
    log.info("=" * 50)
    log.info("SCANNER START")
    log.info("=" * 50)

    tokens, stats = run_full_scan()

    if not tokens:
        log.info("No tokens found")
        return

    token_data = "\n\n---\n\n".join(tokens)
    log.info(f"Found {len(tokens)} tokens")

    send_msg(f"Scan complete. Found {len(tokens)} tokens.")

if __name__ == "__main__":
    main()