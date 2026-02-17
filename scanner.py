"""
scanner.py - Solana Memecoin Scanner v2.2

Self-learning memecoin scanner with paper trading engine.
Runs on GitHub Actions every 15 minutes, sends alerts via Telegram,
and persists learning state via playbook.json committed to git.

Changes from v2.1:
- Explicit Telegram alerts for every live buy/sell attempt with error details
- Fixed Stage 2 timeout (increased to 120s, prompt trimming)
- Better error visibility for debugging live trades
- Trimmed research prompt to prevent Groq context overflow
"""

import os
import re
import json
import time
import logging
import random
import requests
from datetime import datetime, timedelta
from collections import Counter
from typing import Any, Optional

try:
    from trader import buy_token, sell_token, get_wallet_summary, is_live_trading_enabled
    TRADER_AVAILABLE = True
except Exception as _trader_import_err:
    TRADER_AVAILABLE = False
    _TRADER_IMPORT_ERROR = str(_trader_import_err)


# ============================================================
# CONSTANTS
# ============================================================

# -- Scan lifecycle --
MAX_SCANS_PER_PICK = 12                # Close picks after N scans (~3h at 15min intervals)
MAX_TOKENS_PER_SCAN = 12               # Max tokens to fetch detailed data for
MIN_MARKET_CAP = 5000                  # Skip tokens below this MC ($)

# -- Paper trading --
MAX_OPEN_PAPER_TRADES = 3              # Maximum simultaneous paper trades
MAX_PENDING_TRADES = 5                 # Maximum pending confirmations
MIN_CONFIDENCE_FOR_TRADE = 7           # Minimum AI confidence to queue a paper trade
PRICE_DUMP_REJECT_PCT = -15            # Reject pending trade if price dumps more than this %
BUY_PRESSURE_CONFIRM_MIN = 0.8        # Min buy ratio to confirm a pending trade
SLIPPAGE_MIN_PCT = 0.5                # Simulated slippage range (min %)
SLIPPAGE_MAX_PCT = 1.0                # Simulated slippage range (max %)
DEAD_TOKEN_SCAN_LIMIT = 8             # Close paper trade as dead after N unreachable scans

# -- Strategy learning --
RULE_REGEN_INTERVAL = 5               # Re-evaluate rules every N scans
MIN_TRADES_FOR_RULES = 5              # Need this many trades before generating rules
TOKEN_BLACKLIST_HOURS = 24            # How long to blacklist a failed token

# -- API --
API_RATE_LIMIT_DELAY = 0.4            # Seconds between DexScreener API calls
API_MAX_RETRIES = 2                   # Max retries for failed API calls
API_TIMEOUT = 10                      # Default API timeout in seconds

# -- Groq --
GROQ_TIMEOUT_STAGE1 = 60              # Timeout for Stage 1 (short prompt)
GROQ_TIMEOUT_STAGE2 = 120             # Timeout for Stage 2 (long prompt)
GROQ_TIMEOUT_RULES = 90               # Timeout for rule evolution
GROQ_MAX_TOKENS_STAGE1 = 4096         # Max output tokens for Stage 1 (needs room for 12 token scores)
GROQ_MAX_TOKENS_STAGE2 = 4096         # Max output tokens for Stage 2

# -- Telegram --
TELEGRAM_MSG_LIMIT = 4000             # Max chars per Telegram message
TELEGRAM_SEND_TIMEOUT = 10            # Timeout for sending Telegram messages

# -- Prompt safety --
MAX_TOKEN_NAME_LEN = 50               # Truncate token names for prompt injection safety

# -- Research prompt limits --
MAX_RESEARCH_PROMPT_CHARS = 12000     # Trim research prompt if longer than this

# -- Playbook trim limits --
TRIM = {
    "tokens_seen": 200,
    "pick_history": 100,
    "active_picks": 20,
    "win_patterns": 30,
    "lose_patterns": 30,
    "strategy_rules": 20,
    "avoid_conditions": 20,
    "mistake_log": 20,
    "trade_memory": 100,
    "lessons": 50,
    "paper_trade_history": 100,
    "paper_trades": MAX_OPEN_PAPER_TRADES,
    "pending_paper_trades": MAX_PENDING_TRADES,
    "token_blacklist": 200,
}

# -- Market cap tier thresholds (upper bound, label) --
MC_TIERS = [
    (100_000, "micro (<100k)"),
    (500_000, "small (100k-500k)"),
    (2_000_000, "mid (500k-2M)"),
    (5_000_000, "large (2M-5M)"),
]
MC_TIER_DEFAULT = "mega (5M+)"

# -- Volume spike tiers (lower bound, label) --
VOL_SPIKE_TIERS = [
    (5.0, "extreme_spike"),
    (3.0, "high_spike"),
    (1.5, "moderate"),
]
VOL_SPIKE_DEFAULT = "normal"

# -- Buy pressure tiers (lower bound, label) --
PRESSURE_TIERS = [
    (2.0, "heavy_buying"),
    (1.5, "strong_buying"),
    (1.0, "balanced"),
]
PRESSURE_DEFAULT = "selling_pressure"

# -- Trailing stop distances (profit_pct_threshold, trail_pct) --
TRAILING_STOPS = [
    (400, 0.15),   # 5x+: trail 15% below peak
    (200, 0.18),   # 3x+: trail 18%
    (100, 0.20),   # 2x+: trail 20%
    (50, 0.22),    # 1.5x+: trail 22%
]
TRAILING_STOP_DEFAULT = 0.25  # Default: trail 25% below peak


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

if not TRADER_AVAILABLE:
    log.warning(f"trader.py import failed - live trading unavailable: {_TRADER_IMPORT_ERROR}")


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
    """HTTP GET with rate limiting and retry logic for DexScreener API."""
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
    """Sanitize text for safe inclusion in AI prompts."""
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
    """Extract and parse JSON from an AI response."""
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
    """Safely convert a value to float."""
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def safe_int(val: Any, default: int = 0) -> int:
    """Safely convert a value to int."""
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def classify_mc_tier(mc: float) -> str:
    """Classify market cap into a tier."""
    for threshold, label in MC_TIERS:
        if mc < threshold:
            return label
    return MC_TIER_DEFAULT


def classify_descending(value: float, tiers: list[tuple[float, str]], default: str) -> str:
    """Classify a value into a tier using >= thresholds (checked high to low)."""
    for threshold, label in tiers:
        if value >= threshold:
            return label
    return default


def parse_price_value(text: str) -> Optional[float]:
    """Extract a numeric price value from text."""
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
    """Check if a token contract is blacklisted."""
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
    """Add a token to the blacklist."""
    blacklist = pb.setdefault("token_blacklist", {})
    expires = (datetime.now() + timedelta(hours=TOKEN_BLACKLIST_HOURS)).isoformat()
    blacklist[contract] = {
        "name": name,
        "reason": reason,
        "blacklisted_at": datetime.now().isoformat()[:16],
        "expires": expires,
    }
    log.info(f"Blacklisted {name} ({contract[:12]}...) for {TOKEN_BLACKLIST_HOURS}h: {reason}")


def clean_expired_blacklist(pb: dict) -> None:
    """Remove expired blacklist entries."""
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
    """Fetch raw pair data from DexScreener for a Solana token."""
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
    """Parse a single DEX pair into a standardized market data dictionary."""
    price = safe_float(pair.get("priceUsd"))
    mc = safe_float(pair.get("marketCap"))
    lq = safe_float(pair.get("liquidity", {}).get("usd"))

    vol = pair.get("volume", {})
    vol_1h = safe_float(vol.get("h1"))
    vol_6h = safe_float(vol.get("h6"))
    vol_24h = safe_float(vol.get("h24"))

    pc = pair.get("priceChange", {})

    tx_h1 = pair.get("txns", {}).get("h1", {})
    tx_h6 = pair.get("txns", {}).get("h6", {})
    tx_h24 = pair.get("txns", {}).get("h24", {})

    buys_1h = safe_int(tx_h1.get("buys"))
    sells_1h = safe_int(tx_h1.get("sells"))
    buys_6h = safe_int(tx_h6.get("buys"))
    sells_6h = safe_int(tx_h6.get("sells"))
    buys_24h = safe_int(tx_h24.get("buys"))
    sells_24h = safe_int(tx_h24.get("sells"))

    buy_ratio_1h = round(buys_1h / max(sells_1h, 1), 2)
    buy_ratio_24h = round(buys_24h / max(sells_24h, 1), 2)

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
            elif hours_old < 72:
                is_new_tag = f" | RECENT ({int(hours_old / 24)}d old)"
        except (ValueError, TypeError):
            pass

    whale_signals = []
    if buy_ratio_1h >= 2.0:
        whale_signals.append("HEAVY buy pressure")
    elif buy_ratio_1h >= 1.5:
        whale_signals.append("Strong buy pressure")
    if vol_spike >= 3.0:
        whale_signals.append(f"Vol spike {vol_spike}x")
    if mc > 100_000 and buy_ratio_1h > 1.3:
        whale_signals.append("Whale accumulation")

    is_pumpfun_grad = dex_id in ("raydium", "orca") and bool(is_new_tag)

    return {
        "price": price,
        "market_cap": mc,
        "mc_tier": mc_tier,
        "liquidity": lq,
        "liq_to_mc_ratio": round(lq / max(mc, 1), 4),
        "volume_1h": vol_1h,
        "volume_6h": vol_6h,
        "volume_24h": vol_24h,
        "vol_spike": vol_spike,
        "vol_tier": vol_tier,
        "buys_1h": buys_1h,
        "sells_1h": sells_1h,
        "buys_6h": buys_6h,
        "sells_6h": sells_6h,
        "buys_24h": buys_24h,
        "sells_24h": sells_24h,
        "buy_ratio_1h": buy_ratio_1h,
        "buy_ratio_24h": buy_ratio_24h,
        "pressure_tier": pressure_tier,
        "price_change_5m": safe_float(pc.get("m5")),
        "price_change_1h": safe_float(pc.get("h1")),
        "price_change_6h": safe_float(pc.get("h6")),
        "price_change_24h": safe_float(pc.get("h24")),
        "dex": dex_id,
        "hours_old": hours_old,
        "token_name": token_name,
        "token_symbol": token_symbol,
        "url": url,
        "whale_signals": whale_signals,
        "is_new_tag": is_new_tag,
        "is_pumpfun_graduate": is_pumpfun_grad,
    }


def get_token_price(contract: str) -> Optional[float]:
    """Get current price for a single token."""
    pairs = _fetch_dex_pairs(contract)
    if not pairs:
        return None
    return safe_float(pairs[0].get("priceUsd")) or None


def get_token_market_data(contract: str) -> Optional[dict]:
    """Get full parsed market data for a token."""
    pairs = _fetch_dex_pairs(contract)
    if not pairs:
        return None
    return parse_pair_data(pairs[0])


def format_token_for_prompt(parsed: dict, contract: str, source: str) -> str:
    """Format parsed pair data into a text block for AI prompts."""
    name = sanitize_for_prompt(parsed["token_name"])
    symbol = sanitize_for_prompt(parsed["token_symbol"], max_length=20)
    whale_tag = " | WHALE: " + ", ".join(parsed["whale_signals"]) if parsed["whale_signals"] else ""
    pumpfun_tag = " | LIKELY PUMP.FUN GRADUATE" if parsed["is_pumpfun_graduate"] else ""

    return (
        f"Token: {name} ({symbol})\n"
        f"Contract: {contract}\n"
        f"DEX: {parsed['dex']}{parsed['is_new_tag']}{pumpfun_tag} | Found via: {source}\n"
        f"Price: ${parsed['price']}\n"
        f"Price Change >> 5m: {parsed['price_change_5m']}% | 1h: {parsed['price_change_1h']}% | "
        f"6h: {parsed['price_change_6h']}% | 24h: {parsed['price_change_24h']}%\n"
        f"Volume >> 1h: ${parsed['volume_1h']} | 6h: ${parsed['volume_6h']} | "
        f"24h: ${parsed['volume_24h']}\n"
        f"Volume Spike: {parsed['vol_spike']}x vs 24h avg\n"
        f"Txns 1h >> Buys: {parsed['buys_1h']} | Sells: {parsed['sells_1h']} | Ratio: {parsed['buy_ratio_1h']}\n"
        f"Txns 6h >> Buys: {parsed['buys_6h']} | Sells: {parsed['sells_6h']}\n"
        f"Txns 24h >> Buys: {parsed['buys_24h']} | Sells: {parsed['sells_24h']} | Ratio: {parsed['buy_ratio_24h']}\n"
        f"Liquidity: ${parsed['liquidity']:,.0f} | Market Cap: ${parsed['market_cap']:,.0f}\n"
        f"URL: {parsed['url']}"
        f"{whale_tag}"
    )


# ============================================================
# TELEGRAM
# ============================================================

def send_msg(text: str) -> None:
    """Send a message to the configured Telegram user, chunked if needed."""
    for i in range(0, len(text), TELEGRAM_MSG_LIMIT):
        chunk = text[i : i + TELEGRAM_MSG_LIMIT]
        try:
            r = requests.post(
                f"{TAPI}/sendMessage",
                json={"chat_id": USER_ID, "text": chunk},
                timeout=TELEGRAM_SEND_TIMEOUT,
            )
            if r.status_code != 200:
                log.warning(f"Telegram send failed (HTTP {r.status_code}): {r.text[:200]}")
        except requests.exceptions.RequestException as e:
            log.error(f"Telegram send error: {e}")


# ============================================================
# DATA SOURCES
# ============================================================

def _fetch_token_list(url: str, source_name: str) -> list[dict]:
    """Fetch a list of Solana token addresses from a DexScreener endpoint."""
    r = rate_limited_get(url)
    if r is None or r.status_code != 200:
        log.warning(f"Failed to fetch {source_name}")
        return []
    try:
        tokens = r.json()[:30]
        return [
            {"addr": t.get("tokenAddress", ""), "source": source_name}
            for t in tokens
            if t.get("chainId") == "solana" and t.get("tokenAddress")
        ]
    except (json.JSONDecodeError, AttributeError) as e:
        log.error(f"Failed to parse {source_name} response: {e}")
        return []


def fetch_boosted_tokens() -> list[dict]:
    return _fetch_token_list("https://api.dexscreener.com/token-boosts/top/v1", "boosted")


def fetch_latest_profiles() -> list[dict]:
    return _fetch_token_list("https://api.dexscreener.com/token-profiles/latest/v1", "profile")


def fetch_new_pairs() -> list[dict]:
    return _fetch_token_list("https://api.dexscreener.com/token-boosts/latest/v1", "new/pumpfun")


def fetch_scan_data(token_list: list[dict]) -> list[str]:
    """Fetch detailed data for tokens and return formatted text blocks."""
    results = []
    seen: set[str] = set()

    for item in token_list:
        addr = item["addr"]
        source = item["source"]
        if addr in seen or not addr:
            continue
        seen.add(addr)
        if len(results) >= MAX_TOKENS_PER_SCAN:
            break

        pairs = _fetch_dex_pairs(addr)
        if not pairs:
            continue

        parsed = parse_pair_data(pairs[0])
        if parsed["market_cap"] < MIN_MARKET_CAP:
            continue

        results.append(format_token_for_prompt(parsed, addr, source))

    return results


def run_full_scan() -> tuple[list[str], dict]:
    """Gather tokens from all sources and fetch detailed data."""
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
# TOKEN SAFETY CHECK (RugCheck API)
# ============================================================

def check_token_safety(contract: str) -> tuple[bool, int, list[str], str]:
    """Check token safety via RugCheck API."""
    try:
        r = rate_limited_get(
            f"https://api.rugcheck.xyz/v1/tokens/{contract}/report/summary"
        )
        if r is None or r.status_code != 200:
            log.warning(f"RugCheck API unavailable for {contract[:12]}...")
            return True, 0, [], "API unavailable"

        data = r.json()
        score = data.get("score", 0)
        risks = data.get("risks", [])
        rugged = data.get("rugged", False)

        risk_names = []
        danger_count = 0
        critical_flags: list[str] = []

        for risk in risks:
            level = risk.get("level", "")
            name = risk.get("name", "unknown risk")
            risk_names.append(f"{name} ({level})")

            if level in ("danger", "error"):
                danger_count += 1
                name_lower = name.lower()
                if "mint" in name_lower and ("not" in name_lower or "enabled" in name_lower):
                    critical_flags.append("MINT_AUTHORITY_ACTIVE")
                if "freeze" in name_lower:
                    critical_flags.append("FREEZE_AUTHORITY_ACTIVE")
                if "rug" in name_lower or "scam" in name_lower:
                    critical_flags.append("FLAGGED_SCAM")
                if "copycat" in name_lower:
                    critical_flags.append("COPYCAT_TOKEN")
                if "top" in name_lower and ("holder" in name_lower or "10" in name_lower):
                    critical_flags.append("CONCENTRATED_HOLDERS")

        if rugged:
            return False, score, risk_names, "RUGGED"
        if "FLAGGED_SCAM" in critical_flags:
            return False, score, risk_names, "FLAGGED_SCAM"
        if "MINT_AUTHORITY_ACTIVE" in critical_flags:
            return False, score, risk_names, "MINT_NOT_REVOKED"
        if danger_count >= 3:
            return False, score, risk_names, f"{danger_count}_DANGER_FLAGS"
        if "FREEZE_AUTHORITY_ACTIVE" in critical_flags and danger_count >= 2:
            return False, score, risk_names, "FREEZE+DANGER"

        verdict = "SAFE" if danger_count == 0 else f"CAUTION ({danger_count} warnings)"
        return True, score, risk_names, verdict

    except Exception as e:
        log.error(f"RugCheck error for {contract[:12]}...: {e}")
        return True, 0, [], "API error"


# ============================================================
# PLAYBOOK MANAGEMENT
# ============================================================

_PLAYBOOK_DEFAULTS: dict[str, Any] = {
    "lessons": [],
    "scans": 0,
    "tokens_seen": [],
    "last_scan": None,
    "active_picks": [],
    "pick_history": [],
    "performance": {
        "total_picks": 0, "wins": 0, "losses": 0, "neutral": 0,
        "avg_return_pct": 0, "best_pick": {}, "worst_pick": {},
    },
    "win_patterns": [],
    "lose_patterns": [],
    "strategy_rules": [],
    "avoid_conditions": [],
    "mistake_log": [],
    "roi_tiers": {},
    "trade_memory": [],
    "pattern_stats": {
        "by_mc_tier": {}, "by_vol_tier": {},
        "by_pressure_tier": {}, "by_age_group": {}, "by_source": {},
    },
    "paper_trades": [],
    "paper_trade_history": [],
    "paper_trade_stats": {
        "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
        "avg_return_pct": 0, "avg_return_x": 0,
        "best_trade": {}, "worst_trade": {},
        "current_streak": 0, "strategy_accuracy_pct": 0,
    },
    "paper_trade_id_counter": 0,
    "pending_paper_trades": [],
    "last_update_id": 0,
    "token_blacklist": {},
}


def load_playbook() -> dict:
    """Load playbook from disk, ensuring all required fields exist."""
    try:
        with open("playbook.json") as f:
            pb = json.load(f)
        for key, default in _PLAYBOOK_DEFAULTS.items():
            pb.setdefault(key, default)
        log.info(f"Playbook loaded: {pb.get('scans', 0)} scans, {len(pb.get('trade_memory', []))} trades in memory")
        return pb
    except FileNotFoundError:
        log.info("No playbook found, starting fresh")
        return {k: (v.copy() if isinstance(v, (dict, list)) else v) for k, v in _PLAYBOOK_DEFAULTS.items()}
    except json.JSONDecodeError as e:
        log.error(f"Playbook corrupted, attempting backup restore: {e}")
        try:
            with open("playbook.backup.json") as f:
                pb = json.load(f)
            for key, default in _PLAYBOOK_DEFAULTS.items():
                pb.setdefault(key, default)
            log.info("Restored playbook from backup")
            return pb
        except Exception:
            log.error("Backup restore failed, starting fresh")
            return {k: (v.copy() if isinstance(v, (dict, list)) else v) for k, v in _PLAYBOOK_DEFAULTS.items()}


def save_playbook(pb: dict) -> None:
    """Save playbook to disk with backup and trimming."""
    if os.path.exists("playbook.json"):
        try:
            with open("playbook.json", "r") as src:
                backup_data = src.read()
            with open("playbook.backup.json", "w") as dst:
                dst.write(backup_data)
        except Exception as e:
            log.warning(f"Failed to backup playbook: {e}")

    pb["last_scan"] = datetime.now().isoformat()[:16]
    pb["scans"] = pb.get("scans", 0) + 1

    for key, limit in TRIM.items():
        if key in pb:
            val = pb[key]
            if isinstance(val, list):
                pb[key] = val[-limit:]
            elif isinstance(val, dict) and key == "token_blacklist":
                if len(val) > limit:
                    sorted_keys = sorted(
                        val.keys(),
                        key=lambda k: val[k].get("blacklisted_at", ""),
                        reverse=True,
                    )
                    pb[key] = {k: val[k] for k in sorted_keys[:limit]}

    try:
        with open("playbook.json", "w") as f:
            json.dump(pb, f, indent=2)
        log.info(f"Playbook saved (scan #{pb['scans']})")
    except Exception as e:
        log.error(f"Failed to save playbook: {e}")


def track_tokens(pb: dict, tokens_text: str) -> None:
    """Record which tokens were seen in this scan."""
    seen_list = pb.get("tokens_seen", [])
    now = datetime.now().isoformat()[:16]
    for line in tokens_text.split("\n"):
        if line.startswith("Token: "):
            token_name = line.replace("Token: ", "").strip()
            seen_list.append({"name": token_name, "date": now})
    pb["tokens_seen"] = seen_list[-TRIM["tokens_seen"]:]


# ============================================================
# AI PROMPT BUILDING
# ============================================================

def build_scan_prompt(pb: dict) -> str:
    """Build Stage 1 prompt that uses learned strategy rules for filtering."""
    base = """You are a sharp Solana memecoin scanner. Analyze the token data and quickly identify
the TOP 3 tokens with the best 2x-10x potential.

For each pick, respond in this EXACT JSON format and nothing else:
```json
[
  {"rank": 1, "name": "TOKEN_NAME", "symbol": "SYMBOL", "contract": "ADDRESS", "entry_price": "current_price_number", "confidence": 7, "reason": "Brief reason"},
  {"rank": 2, "name": "TOKEN_NAME", "symbol": "SYMBOL", "contract": "ADDRESS", "entry_price": "current_price_number", "confidence": 6, "reason": "Brief reason"},
  {"rank": 3, "name": "TOKEN_NAME", "symbol": "SYMBOL", "contract": "ADDRESS", "entry_price": "current_price_number", "confidence": 5, "reason": "Brief reason"}
]
```

Use the ACTUAL current price from the data as entry_price (just the number, no $ sign).
confidence = 1-10 based on how closely the token matches your winning criteria.
"""

    rules = pb.get("strategy_rules", [])
    if rules:
        base += "\n--- YOUR LEARNED STRATEGY RULES (follow these, they come from your real results) ---\n"
        for rule in rules[-15:]:
            base += f"- {rule}\n"
        base += "\nPrioritize tokens that match WINNING rules. Avoid tokens that match LOSING rules.\n"
    else:
        base += """
Default criteria (replaced by learned rules once you have data):
- Buy/sell ratio > 1.3 in last hour (whale accumulation)
- Volume spike above average (momentum building)
- Market cap under $5M (room to run 2-10x)
- Strong recent price action but NOT already pumped 500%+ in 24h
- Good liquidity relative to market cap
- NEW PAIRS and PUMP.FUN graduates get bonus points
"""

    avoid_patterns = pb.get("avoid_conditions", [])
    if avoid_patterns:
        base += "\n--- RED FLAGS (these conditions caused losses - AVOID) ---\n"
        for ap in avoid_patterns[-10:]:
            base += f"- {ap}\n"

    blacklist = pb.get("token_blacklist", {})
    active_blacklist = [
        entry["name"] for addr, entry in blacklist.items()
        if is_blacklisted(pb, addr)
    ]
    if active_blacklist:
        base += "\n--- BLACKLISTED TOKENS (DO NOT PICK THESE) ---\n"
        for name in active_blacklist[:20]:
            base += f"- {name}\n"

    base += "\nSkip: already-pumped tokens, dead volume, MC > $10M\nOnly output the JSON, nothing else."
    return base


def build_research_prompt(pb: dict) -> str:
    """Build Stage 2 deep research prompt with full performance context.

    v2.2: Trimmed to prevent Groq context overflow after many scans.
    """
    prompt = """You are a sharp Solana memecoin trading AI that LEARNS FROM REAL RESULTS.
Talk like a real trading partner - casual, direct, decisive.

IMPORTANT: Only pick coins that realistically have 2x-10x potential.
"""

    stats = pb.get("performance", {})
    total_picks = stats.get("total_picks", 0)
    if total_picks > 0:
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        avg_return = stats.get("avg_return_pct", 0)
        best = stats.get("best_pick", {})
        worst = stats.get("worst_pick", {})
        win_rate = round((wins / max(total_picks, 1)) * 100, 1)

        prompt += f"""
YOUR TRACK RECORD: {total_picks} picks | {wins}W-{losses}L | {win_rate}% WR | Avg: {avg_return:+.1f}%
"""
        if best.get("name"):
            prompt += f"Best: {best['name']} ({best.get('return_pct', 0):+.1f}%) | "
        if worst.get("name"):
            prompt += f"Worst: {worst['name']} ({worst.get('return_pct', 0):+.1f}%)\n"

    roi_tiers = pb.get("roi_tiers", {})
    if roi_tiers:
        prompt += "\nBEST SETUPS BY ROI:\n"
        sorted_tiers = sorted(roi_tiers.items(), key=lambda x: x[1]["avg_roi"], reverse=True)
        for tier_name, tier_data in sorted_tiers[:5]:
            prompt += f"- {tier_name}: avg {tier_data.get('avg_roi', 0):+.1f}% ({tier_data.get('count', 0)} trades)\n"

    for label, key, limit in [
        ("WIN PATTERNS", "win_patterns", 5),
        ("LOSS PATTERNS", "lose_patterns", 5),
    ]:
        patterns = pb.get(key, [])
        if patterns:
            prompt += f"\n{label}:\n"
            for p in patterns[-limit:]:
                prompt += f"- {p[:200]}\n"

    rules = pb.get("strategy_rules", [])
    if rules:
        prompt += "\nSTRATEGY RULES:\n"
        for r in rules[-10:]:
            prompt += f"- {r[:200]}\n"

    avoid_conditions = pb.get("avoid_conditions", [])
    if avoid_conditions:
        prompt += "\nAVOID:\n"
        for ac in avoid_conditions[-5:]:
            prompt += f"- {ac[:200]}\n"

    mistakes = pb.get("mistake_log", [])
    if mistakes:
        prompt += "\nRECENT MISTAKES:\n"
        for m in mistakes[-3:]:
            prompt += f"- {m.get('token', '')}: {m.get('lesson', '')[:150]}\n"

    pt_stats = pb.get("paper_trade_stats", {})
    if pt_stats.get("total_trades", 0) > 0:
        prompt += (
            f"\nPAPER TRADES: {pt_stats['total_trades']} done | "
            f"{pt_stats.get('win_rate', 0)}% WR | Avg: {pt_stats.get('avg_return_pct', 0):+.1f}%\n"
        )

    prompt += f"""
Scan #{pb.get('scans', 0) + 1}.

For EACH pick provide:
PICK #[n]: [NAME] ([SYMBOL])
Contract: [address]
RESEARCH: What data says, volume/buy analysis, MC trajectory, pattern match
TRADE SETUP: Entry, Stop Loss (20-30% below), TP1 (~2x), TP2 (~3-5x), TP3 (~5-10x)
Strategy / Risk Level / Confidence (1-10)

After all picks: WHALE WATCH, AVOID LIST, MARKET VIBE

End with ```json summary:
```json
{{"lessons": ["lesson1"], "rule_updates": {{"add": ["rule"], "remove_keywords": ["keyword"]}}, "self_reflection": "brief"}}
```
Not financial advice."""

    # Trim if too long
    if len(prompt) > MAX_RESEARCH_PROMPT_CHARS:
        prompt = prompt[:MAX_RESEARCH_PROMPT_CHARS] + "\n...(trimmed for length)"
        log.warning(f"Research prompt trimmed to {MAX_RESEARCH_PROMPT_CHARS} chars")

    return prompt


# ============================================================
# AI CALLS (Groq)
# ============================================================

_last_groq_error = ""  # Stores last Groq error for Telegram reporting

# Model fallback chain - if one hits rate limit, try the next
# Each model has its own separate daily token limit on Groq free tier
GROQ_MODEL_CHAIN = [
    "llama-3.3-70b-versatile",    # Best quality (primary)
    "llama-3.1-70b-versatile",    # Similar quality (fallback 1)
    "llama3-70b-8192",            # Older but solid (fallback 2)
    "mixtral-8x7b-32768",         # Different architecture (fallback 3)
    "llama-3.1-8b-instant",       # Smaller but fast (last resort)
]

def call_groq(system: str, prompt: str, temperature: float = 0.8,
              timeout: int = GROQ_TIMEOUT_STAGE1, max_tokens: int = GROQ_MAX_TOKENS_STAGE2) -> Optional[str]:
    """Call Groq API with model fallback chain. If a model hits rate limit (429),
    automatically tries the next model in the chain."""
    global _last_groq_error
    prompt_len = len(system) + len(prompt)
    log.info(f"Groq call: prompt_len={prompt_len}, max_tokens={max_tokens}, timeout={timeout}s")

    errors = []
    for model in GROQ_MODEL_CHAIN:
        try:
            log.info(f"Trying model: {model}")
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                timeout=timeout,
            )

            # Rate limited - try next model
            if resp.status_code == 429:
                err = f"{model}: rate limited"
                log.warning(f"Groq {err}, trying next model...")
                errors.append(err)
                continue

            # Other HTTP error - don't retry, it's likely a real problem
            if resp.status_code != 200:
                _last_groq_error = f"{model} HTTP {resp.status_code}: {resp.text[:200]}"
                log.error(f"Groq API error: {_last_groq_error}")
                return None

            result = resp.json()
            content = result["choices"][0]["message"]["content"]
            if model != GROQ_MODEL_CHAIN[0]:
                log.info(f"Fallback model {model} succeeded")
            return content

        except requests.exceptions.Timeout:
            _last_groq_error = f"{model}: Timeout after {timeout}s (prompt_len={prompt_len})"
            log.error(f"Groq API: {_last_groq_error}")
            return None
        except requests.exceptions.RequestException as e:
            _last_groq_error = f"{model}: Request failed: {str(e)[:200]}"
            log.error(f"Groq API: {_last_groq_error}")
            return None
        except (KeyError, IndexError) as e:
            _last_groq_error = f"{model}: Bad response format: {str(e)[:200]}"
            log.error(f"Groq API: {_last_groq_error}")
            return None

    # All models rate limited
    _last_groq_error = f"All models rate limited: {', '.join(errors)}"
    log.error(f"Groq API: {_last_groq_error}")
    return None


# ============================================================
# PICK TRACKING & PERFORMANCE
# ============================================================

def save_new_picks(pb: dict, stage1_result: str) -> None:
    """Parse AI picks, enrich with market snapshots, and save as active picks."""
    picks = extract_json_from_response(stage1_result)
    if not picks or not isinstance(picks, list):
        return

    active = pb.get("active_picks", [])

    for pick in picks:
        try:
            entry_str = str(pick.get("entry_price", "0")).replace("$", "").replace(",", "").strip()
            entry_float = float(entry_str)
            if entry_float <= 0:
                continue

            contract = pick.get("contract", "")
            if not contract:
                continue

            if is_blacklisted(pb, contract):
                log.info(f"Skipping blacklisted token: {pick.get('name', '?')}")
                continue

            market_data = get_token_market_data(contract) or {}

            active.append({
                "name": pick.get("name", "Unknown"),
                "symbol": pick.get("symbol", "?"),
                "contract": contract,
                "entry_price": entry_float,
                "confidence": pick.get("confidence", 5),
                "reason": pick.get("reason", ""),
                "picked_at": datetime.now().isoformat()[:16],
                "scans_tracked": 0,
                "peak_price": entry_float,
                "lowest_price": entry_float,
                "entry_snapshot": {
                    "market_cap": market_data.get("market_cap", 0),
                    "mc_tier": market_data.get("mc_tier", "unknown"),
                    "liquidity": market_data.get("liquidity", 0),
                    "liq_to_mc_ratio": market_data.get("liq_to_mc_ratio", 0),
                    "volume_1h": market_data.get("volume_1h", 0),
                    "volume_24h": market_data.get("volume_24h", 0),
                    "vol_spike": market_data.get("vol_spike", 0),
                    "vol_tier": market_data.get("vol_tier", "unknown"),
                    "buy_ratio_1h": market_data.get("buy_ratio_1h", 0),
                    "pressure_tier": market_data.get("pressure_tier", "unknown"),
                    "price_change_1h": market_data.get("price_change_1h", 0),
                    "price_change_6h": market_data.get("price_change_6h", 0),
                    "price_change_24h": market_data.get("price_change_24h", 0),
                    "dex": market_data.get("dex", "unknown"),
                    "hours_old": market_data.get("hours_old"),
                    "source": pick.get("source", "scan"),
                },
            })
        except (ValueError, TypeError) as e:
            log.warning(f"Failed to save pick {pick.get('name', '?')}: {e}")

    pb["active_picks"] = active[-TRIM["active_picks"]:]


def update_pattern_stats(pb: dict, pick: dict, return_pct: float) -> None:
    """Update statistical pattern tracking based on trade outcomes."""
    stats = pb.setdefault("pattern_stats", {
        "by_mc_tier": {}, "by_vol_tier": {},
        "by_pressure_tier": {}, "by_age_group": {}, "by_source": {},
    })
    snapshot = pick.get("entry_snapshot", {})

    def update_cat(category_key: str, tier_value: Optional[str]) -> None:
        if not tier_value or tier_value == "unknown":
            return
        cat = stats.setdefault(category_key, {})
        tier = cat.setdefault(tier_value, {"total": 0, "wins": 0, "losses": 0, "returns": []})
        tier["total"] += 1
        if return_pct >= 20:
            tier["wins"] += 1
        elif return_pct < -10:
            tier["losses"] += 1
        tier["returns"].append(round(return_pct, 1))
        tier["returns"] = tier["returns"][-50:]

    update_cat("by_mc_tier", snapshot.get("mc_tier"))
    update_cat("by_vol_tier", snapshot.get("vol_tier"))
    update_cat("by_pressure_tier", snapshot.get("pressure_tier"))

    hours_old = snapshot.get("hours_old")
    if hours_old is not None:
        if hours_old < 1:
            age_group = "<1h"
        elif hours_old < 6:
            age_group = "1-6h"
        elif hours_old < 24:
            age_group = "6-24h"
        elif hours_old < 72:
            age_group = "1-3d"
        else:
            age_group = "3d+"
        update_cat("by_age_group", age_group)

    update_cat("by_source", snapshot.get("source", "scan"))


def save_trade_memory(pb: dict, pick: dict, current_price: float, return_pct: float, result_tag: str) -> None:
    """Save detailed trade record for pattern analysis."""
    snapshot = pick.get("entry_snapshot", {})
    entry_price = pick.get("entry_price", 1)
    peak_price = pick.get("peak_price", 0)
    peak_return = round(((peak_price - entry_price) / max(entry_price, 1e-15)) * 100, 1)

    record = {
        "name": pick.get("name", "?"),
        "symbol": pick.get("symbol", "?"),
        "contract": pick.get("contract", ""),
        "entry_price": entry_price,
        "exit_price": current_price,
        "return_pct": round(return_pct, 1),
        "peak_return_pct": peak_return,
        "result": result_tag,
        "confidence_at_pick": pick.get("confidence", 5),
        "reason": pick.get("reason", ""),
        "picked_at": pick.get("picked_at", ""),
        "closed_at": datetime.now().isoformat()[:16],
        "scans_held": pick.get("scans_tracked", 0),
        "entry_market_cap": snapshot.get("market_cap", 0),
        "entry_mc_tier": snapshot.get("mc_tier", "unknown"),
        "entry_liquidity": snapshot.get("liquidity", 0),
        "entry_liq_ratio": snapshot.get("liq_to_mc_ratio", 0),
        "entry_vol_spike": snapshot.get("vol_spike", 0),
        "entry_vol_tier": snapshot.get("vol_tier", "unknown"),
        "entry_buy_ratio": snapshot.get("buy_ratio_1h", 0),
        "entry_pressure": snapshot.get("pressure_tier", "unknown"),
        "entry_price_change_1h": snapshot.get("price_change_1h", 0),
        "entry_age_hours": snapshot.get("hours_old"),
        "entry_dex": snapshot.get("dex", "unknown"),
    }
    pb.setdefault("trade_memory", []).append(record)
    pb["trade_memory"] = pb["trade_memory"][-TRIM["trade_memory"]:]


def detect_mistakes(pb: dict, pick: dict, return_pct: float) -> None:
    """After a significant loss, analyze what went wrong."""
    if return_pct >= -10:
        return

    snapshot = pick.get("entry_snapshot", {})
    name = pick.get("name", "?")
    contract = pick.get("contract", "")

    conditions: list[str] = []
    if snapshot.get("vol_spike", 0) < 1.5:
        conditions.append("low volume spike at entry")
    if snapshot.get("buy_ratio_1h", 0) < 1.0:
        conditions.append("sellers outnumbered buyers at entry")
    if snapshot.get("price_change_1h", 0) > 100:
        conditions.append("token had already pumped 100%+ in 1h before entry")
    if snapshot.get("price_change_24h", 0) > 500:
        conditions.append("token had already pumped 500%+ in 24h")
    if snapshot.get("liq_to_mc_ratio", 0) < 0.05:
        conditions.append("very low liquidity-to-MC ratio (thin exit)")
    if snapshot.get("market_cap", 0) > 5_000_000:
        conditions.append("MC was over $5M (limited upside)")
    hours_old = snapshot.get("hours_old")
    if hours_old and hours_old > 72:
        conditions.append("token was already 3+ days old (not fresh)")

    lesson = f"Lost {return_pct:.1f}% on {name}"
    if conditions:
        lesson += f". Warning signs: {', '.join(conditions)}"
    else:
        lesson += ". No obvious warning signs at entry - may be random market conditions"

    pb.setdefault("mistake_log", []).append({
        "date": datetime.now().isoformat()[:10],
        "token": name,
        "return_pct": round(return_pct, 1),
        "confidence_was": pick.get("confidence", "?"),
        "lesson": lesson,
        "conditions": conditions,
    })

    avoid = pb.setdefault("avoid_conditions", [])
    new_avoids = []
    if snapshot.get("price_change_1h", 0) > 100:
        new_avoids.append(f"AVOID tokens already pumped >100% in 1h (lost {return_pct:.1f}% on {name})")
    if snapshot.get("buy_ratio_1h", 0) < 1.0:
        new_avoids.append(f"AVOID tokens with sell pressure (ratio <1.0) (lost {return_pct:.1f}% on {name})")
    if snapshot.get("liq_to_mc_ratio", 0) < 0.05:
        new_avoids.append(f"AVOID tokens with liq/MC ratio <5% (thin liquidity trap, lost on {name})")

    for rule in new_avoids:
        if rule not in avoid:
            avoid.append(rule)
    pb["avoid_conditions"] = avoid[-TRIM["avoid_conditions"]:]

    if contract and return_pct < -20:
        blacklist_token(pb, contract, name, f"Lost {return_pct:.1f}%")


def update_roi_tiers(pb: dict) -> None:
    """Analyze trade memory to find which setup types produce the best ROI."""
    memory = pb.get("trade_memory", [])
    if len(memory) < 3:
        return

    tiers: dict[str, dict] = {}

    def analyze_group(prefix: str, key: str, extractor) -> None:
        groups: dict[str, list[float]] = {}
        for trade in memory:
            val = extractor(trade)
            if val and val != "unknown":
                groups.setdefault(val, []).append(trade["return_pct"])
        for group_name, returns in groups.items():
            if len(returns) >= 2:
                tiers[f"{prefix}_{group_name}"] = {
                    "avg_roi": round(sum(returns) / len(returns), 1),
                    "count": len(returns),
                    "win_rate": round(len([r for r in returns if r >= 20]) / len(returns) * 100, 1),
                }

    analyze_group("MC", "mc_tier", lambda t: t.get("entry_mc_tier"))
    analyze_group("Vol", "vol_tier", lambda t: t.get("entry_vol_tier"))
    analyze_group("Pressure", "pressure", lambda t: t.get("entry_pressure"))

    def age_group(t: dict) -> Optional[str]:
        h = t.get("entry_age_hours")
        if h is None:
            return None
        if h < 1:
            return "<1h"
        if h < 6:
            return "1-6h"
        if h < 24:
            return "6-24h"
        return "24h+"

    analyze_group("Age", "age", age_group)

    pb["roi_tiers"] = tiers


def evolve_strategy_rules(pb: dict) -> None:
    """Evolve strategy rules incrementally using AI analysis."""
    memory = pb.get("trade_memory", [])
    if len(memory) < MIN_TRADES_FOR_RULES:
        return

    existing_rules = pb.get("strategy_rules", [])
    roi_tiers = pb.get("roi_tiers", {})

    wins = [t for t in memory if t["return_pct"] >= 20]
    losses = [t for t in memory if t["return_pct"] < -10]

    summary = f"TRADE HISTORY: {len(memory)} trades total\n"

    if wins:
        summary += f"\nWINNING TRADES ({len(wins)}):\n"
        for w in wins[-8:]:
            summary += (
                f"- {w['name']}: +{w['return_pct']}% | MC: ${w.get('entry_market_cap', 0):,.0f} "
                f"({w.get('entry_mc_tier', '?')}) | Vol spike: {w.get('entry_vol_spike', 0)}x | "
                f"Buy ratio: {w.get('entry_buy_ratio', 0)} | Age: {w.get('entry_age_hours', '?')}h\n"
            )

    if losses:
        summary += f"\nLOSING TRADES ({len(losses)}):\n"
        for loss in losses[-8:]:
            summary += (
                f"- {loss['name']}: {loss['return_pct']}% | MC: ${loss.get('entry_market_cap', 0):,.0f} "
                f"({loss.get('entry_mc_tier', '?')}) | Vol spike: {loss.get('entry_vol_spike', 0)}x | "
                f"Buy ratio: {loss.get('entry_buy_ratio', 0)} | Age: {loss.get('entry_age_hours', '?')}h\n"
            )

    if roi_tiers:
        summary += "\nROI BY CATEGORY:\n"
        for name, data in sorted(roi_tiers.items(), key=lambda x: x[1]["avg_roi"], reverse=True)[:8]:
            summary += f"- {name}: avg ROI {data['avg_roi']:+.1f}%, win rate {data['win_rate']}% ({data['count']} trades)\n"

    existing_text = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(existing_rules)) if existing_rules else "(none yet)"

    system = """You are a quantitative trading analyst. Review the trade data and existing strategy rules.

Output your analysis in ```json format:
```json
{
  "keep_indices": [1, 3],
  "modify": [{"index": 2, "new_rule": "Updated rule text backed by data"}],
  "remove_indices": [4],
  "add": ["Brand new rule backed by specific data"]
}
```

- keep_indices: 1-indexed list of existing rules that data supports
- modify: rules to update with new evidence (include index and new text)
- remove_indices: rules contradicted by data
- add: new rules discovered from the data (must cite specific numbers)

Total rules should be 5-10. Each must be specific, actionable, and data-backed."""

    user_prompt = f"EXISTING RULES:\n{existing_text}\n\nTRADE DATA:\n{summary}"

    result = call_groq(system, user_prompt, temperature=0.3, timeout=GROQ_TIMEOUT_RULES)
    if not result:
        return

    parsed = extract_json_from_response(result)
    if not parsed or not isinstance(parsed, dict):
        log.warning("Failed to parse strategy rule evolution response")
        return

    try:
        keep_indices = set(parsed.get("keep_indices", []))
        remove_indices = set(parsed.get("remove_indices", []))
        modify_map = {
            m["index"]: m["new_rule"]
            for m in parsed.get("modify", [])
            if isinstance(m, dict) and "index" in m and "new_rule" in m
        }

        new_rules: list[str] = []
        for i, rule in enumerate(existing_rules):
            idx = i + 1
            if idx in remove_indices:
                continue
            if idx in modify_map:
                new_rules.append(modify_map[idx])
            else:
                new_rules.append(rule)

        for new_rule in parsed.get("add", []):
            if isinstance(new_rule, str) and new_rule.strip():
                new_rules.append(new_rule.strip())

        pb["strategy_rules"] = new_rules[:TRIM["strategy_rules"]]
        log.info(f"Strategy rules evolved: {len(existing_rules)} -> {len(new_rules)}")

    except (KeyError, TypeError) as e:
        log.warning(f"Error applying rule evolution: {e}")


def check_past_picks(pb: dict) -> Optional[str]:
    """Check current prices of active picks and update performance."""
    active = pb.get("active_picks", [])
    if not active:
        return None

    still_active: list[dict] = []
    report_lines: list[str] = []
    performance = pb.get("performance", {
        "total_picks": 0, "wins": 0, "losses": 0, "neutral": 0,
        "avg_return_pct": 0, "best_pick": {}, "worst_pick": {},
    })
    history = pb.get("pick_history", [])

    for pick in active:
        contract = pick.get("contract", "")
        if not contract:
            continue

        current_price = get_token_price(contract)

        if current_price is None:
            pick["scans_tracked"] = pick.get("scans_tracked", 0) + 1
            if pick["scans_tracked"] >= MAX_SCANS_PER_PICK:
                return_pct = -100.0
                result_tag = "DEAD/RUGGED"
                save_trade_memory(pb, pick, 0, return_pct, result_tag)
                update_pattern_stats(pb, pick, return_pct)
                detect_mistakes(pb, pick, return_pct)
                history.append({
                    **pick,
                    "final_price": 0, "return_pct": return_pct,
                    "result": result_tag, "closed_at": datetime.now().isoformat()[:16],
                })
                performance["total_picks"] += 1
                performance["losses"] += 1
                pb.setdefault("lose_patterns", []).append(
                    f"{pick['name']}: Token went dead/unreachable after {pick['scans_tracked']} scans. "
                    f"Reason picked: {pick.get('reason', '?')}"
                )
                report_lines.append(f"\U0001f480 {pick['name']} - DEAD/RUGGED (-100%)")
            else:
                still_active.append(pick)
            continue

        entry_price = pick.get("entry_price", 0)
        if entry_price <= 0:
            still_active.append(pick)
            continue

        return_pct = round(((current_price - entry_price) / entry_price) * 100, 1)
        pick["scans_tracked"] = pick.get("scans_tracked", 0) + 1

        if current_price > pick.get("peak_price", 0):
            pick["peak_price"] = current_price
        if current_price < pick.get("lowest_price", float("inf")):
            pick["lowest_price"] = current_price

        peak_return = round(((pick["peak_price"] - entry_price) / entry_price) * 100, 1)

        if pick["scans_tracked"] >= MAX_SCANS_PER_PICK:
            if return_pct >= 100:
                result_tag = "BIG WIN (2x+)"
                performance["wins"] += 1
                pb.setdefault("win_patterns", []).append(
                    f"{pick['name']}: +{return_pct}% in {pick['scans_tracked']} scans. "
                    f"Peak was +{peak_return}%. Reason: {pick.get('reason', '?')}"
                )
            elif return_pct >= 20:
                result_tag = "SMALL WIN"
                performance["wins"] += 1
                pb.setdefault("win_patterns", []).append(
                    f"{pick['name']}: +{return_pct}% (small win). Reason: {pick.get('reason', '?')}"
                )
            elif return_pct >= -10:
                result_tag = "NEUTRAL"
                performance["neutral"] += 1
            else:
                result_tag = "LOSS"
                performance["losses"] += 1
                pb.setdefault("lose_patterns", []).append(
                    f"{pick['name']}: {return_pct}% loss. Peak was +{peak_return}%. "
                    f"Reason picked: {pick.get('reason', '?')}"
                )

            performance["total_picks"] += 1
            save_trade_memory(pb, pick, current_price, return_pct, result_tag)
            update_pattern_stats(pb, pick, return_pct)

            if return_pct < -10:
                detect_mistakes(pb, pick, return_pct)

            best = performance.get("best_pick", {})
            if not best or return_pct > best.get("return_pct", -999):
                performance["best_pick"] = {"name": pick["name"], "return_pct": return_pct}
            worst = performance.get("worst_pick", {})
            if not worst or return_pct < worst.get("return_pct", 999):
                performance["worst_pick"] = {"name": pick["name"], "return_pct": return_pct}

            total = performance["total_picks"]
            old_avg = performance.get("avg_return_pct", 0)
            performance["avg_return_pct"] = round(((old_avg * (total - 1)) + return_pct) / total, 1)

            history.append({
                **pick,
                "final_price": current_price, "return_pct": return_pct,
                "peak_return_pct": peak_return, "result": result_tag,
                "closed_at": datetime.now().isoformat()[:16],
            })

            emoji = "\U0001f7e2" if return_pct > 20 else "\U0001f534" if return_pct < -10 else "\u26aa"
            report_lines.append(
                f"{emoji} {pick['name']} ({pick['symbol']}): {return_pct:+.1f}% "
                f"(peak: +{peak_return}%) | Conf was: {pick.get('confidence', '?')}/10 - {result_tag}"
            )
        else:
            emoji = "\U0001f4c8" if return_pct > 0 else "\U0001f4c9"
            report_lines.append(
                f"{emoji} {pick['name']} ({pick['symbol']}): {return_pct:+.1f}% "
                f"(peak: +{peak_return}%) - tracking ({pick['scans_tracked']}/{MAX_SCANS_PER_PICK})"
            )
            still_active.append(pick)

    pb["active_picks"] = still_active
    pb["pick_history"] = history[-TRIM["pick_history"]:]
    pb["performance"] = performance

    if report_lines:
        total = performance.get("total_picks", 0)
        wins = performance.get("wins", 0)
        win_rate = round((wins / max(total, 1)) * 100, 1) if total > 0 else 0
        avg_ret = performance.get("avg_return_pct", 0)

        roi_summary = ""
        roi_tiers = pb.get("roi_tiers", {})
        if roi_tiers:
            best_tier = max(roi_tiers.items(), key=lambda x: x[1]["avg_roi"])
            worst_tier = min(roi_tiers.items(), key=lambda x: x[1]["avg_roi"])
            roi_summary = (
                f"\nBest setup: {best_tier[0]} (avg {best_tier[1]['avg_roi']:+.1f}%)"
                f"\nWorst setup: {worst_tier[0]} (avg {worst_tier[1]['avg_roi']:+.1f}%)"
            )

        header = (
            f"\U0001f4cb PICK TRACKER UPDATE\n"
            f"{'=' * 35}\n"
            f"Active: {len(still_active)} | Completed: {total}\n"
            f"Win rate: {win_rate}% | Avg return: {avg_ret:+.1f}%\n"
            f"Rules: {len(pb.get('strategy_rules', []))} | "
            f"Mistakes: {len(pb.get('mistake_log', []))}"
            f"{roi_summary}\n"
            f"{'=' * 35}\n\n"
        )
        return header + "\n".join(report_lines)
    return None


# ============================================================
# PAPER TRADE ENGINE
# ============================================================

def parse_trade_setups(research_text: str) -> dict[str, dict]:
    """Parse trade setups from Stage 2 research."""
    setups: dict[str, dict] = {}
    current_symbol: Optional[str] = None

    for line in research_text.split("\n"):
        stripped = line.strip()

        pick_match = re.search(r"PICK\s*#?\s*\d+\s*[:\-]\s*(.+?)\s*\((\w+)\)", stripped, re.IGNORECASE)
        if pick_match:
            current_symbol = pick_match.group(2).upper()
            setups.setdefault(current_symbol, {})
            continue

        if not current_symbol:
            continue

        lower = stripped.lower()
        price = parse_price_value(stripped)

        if price and price > 0:
            if any(kw in lower for kw in ("stop loss", "stop-loss", "sl:")):
                setups[current_symbol]["stop_loss"] = price
            elif "tp3" in lower or "moon" in lower:
                setups[current_symbol]["tp3"] = price
            elif "tp2" in lower or "mid" in lower:
                setups[current_symbol]["tp2"] = price
            elif "tp1" in lower or "safe" in lower:
                setups[current_symbol]["tp1"] = price

    return setups


def create_paper_trade(pb: dict, pick: dict, trade_setup: dict, scan_num: int) -> Optional[dict]:
    """Create a paper trade entry with simulated slippage and market snapshot."""
    entry_price = pick.get("entry_price", 0)
    if entry_price <= 0:
        return None

    slippage_pct = round(random.uniform(SLIPPAGE_MIN_PCT, SLIPPAGE_MAX_PCT), 2)
    entry_with_slippage = entry_price * (1 + slippage_pct / 100)

    stop_loss = trade_setup.get("stop_loss", entry_with_slippage * 0.75)
    tp1 = trade_setup.get("tp1", entry_with_slippage * 2.0)
    tp2 = trade_setup.get("tp2", entry_with_slippage * 3.5)
    tp3 = trade_setup.get("tp3", entry_with_slippage * 7.0)

    pb["paper_trade_id_counter"] = pb.get("paper_trade_id_counter", 0) + 1
    trade_id = f"PT-{scan_num}-{pb['paper_trade_id_counter']}"

    contract = pick.get("contract", "")
    market_data = get_token_market_data(contract) if contract else {}
    if not market_data:
        market_data = {}

    now = datetime.now().isoformat()[:16]

    return {
        "trade_id": trade_id,
        "token_name": pick.get("name", "Unknown"),
        "symbol": pick.get("symbol", "?"),
        "contract": contract,
        "entry_price": round(entry_with_slippage, 10),
        "original_rec_price": entry_price,
        "slippage_pct": slippage_pct,
        "stop_loss": stop_loss,
        "original_stop_loss": stop_loss,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "tp1_hit": False, "tp2_hit": False,
        "status": "OPEN",
        "confidence": pick.get("confidence", 5),
        "reason": pick.get("reason", ""),
        "opened_at": now,
        "last_update": now,
        "peak_price": entry_with_slippage,
        "lowest_price": entry_with_slippage,
        "scans_monitored": 0,
        "updates": [],
        "entry_snapshot": {
            "market_cap": market_data.get("market_cap", 0),
            "mc_tier": market_data.get("mc_tier", "unknown"),
            "liquidity": market_data.get("liquidity", 0),
            "liq_to_mc_ratio": market_data.get("liq_to_mc_ratio", 0),
            "volume_1h": market_data.get("volume_1h", 0),
            "volume_24h": market_data.get("volume_24h", 0),
            "vol_spike": market_data.get("vol_spike", 0),
            "vol_tier": market_data.get("vol_tier", "unknown"),
            "buy_ratio_1h": market_data.get("buy_ratio_1h", 0),
            "pressure_tier": market_data.get("pressure_tier", "unknown"),
            "price_change_1h": market_data.get("price_change_1h", 0),
            "price_change_6h": market_data.get("price_change_6h", 0),
            "price_change_24h": market_data.get("price_change_24h", 0),
            "dex": market_data.get("dex", "unknown"),
            "hours_old": market_data.get("hours_old"),
        },
    }


def apply_trailing_stop(trade: dict, current_price: float) -> tuple[bool, float]:
    """Apply adaptive trailing stop loss."""
    entry = trade["entry_price"]
    peak = trade.get("peak_price", entry)
    current_sl = trade["stop_loss"]

    if peak <= entry:
        return False, current_sl

    peak_return_pct = ((peak - entry) / entry) * 100

    trail_pct = TRAILING_STOP_DEFAULT
    for threshold, pct in TRAILING_STOPS:
        if peak_return_pct >= threshold:
            trail_pct = pct
            break

    trailing_sl = peak * (1 - trail_pct)

    if trailing_sl > current_sl:
        trade["stop_loss"] = trailing_sl
        return True, trailing_sl

    return False, current_sl


def evaluate_trade_action(trade: dict, current_price: float, current_data: Optional[dict]) -> tuple[str, str]:
    """Evaluate what action to take on an open paper trade."""
    entry = trade["entry_price"]
    sl = trade["stop_loss"]
    tp1 = trade["tp1"]
    tp2 = trade["tp2"]
    tp3 = trade["tp3"]
    return_pct = ((current_price - entry) / entry) * 100

    if current_price <= sl:
        return "EXIT", f"Stop loss hit at ${current_price:.10g} (SL: ${sl:.10g})"

    if current_price >= tp3:
        return "EXIT_TP3", f"TP3 (Moon) reached! ${current_price:.10g} (+{return_pct:.1f}%)"

    if current_price >= tp2 and not trade.get("tp2_hit"):
        return "PARTIAL_TP2", f"TP2 (Mid) reached! ${current_price:.10g} (+{return_pct:.1f}%)"

    if current_price >= tp1 and not trade.get("tp1_hit"):
        return "PARTIAL_TP1", f"TP1 (Safe) reached! ${current_price:.10g} (+{return_pct:.1f}%)"

    if current_data:
        buy_ratio = current_data.get("buy_ratio_1h", 1.0)
        vol_spike = current_data.get("vol_spike", 0)
        price_change_1h = current_data.get("price_change_1h", 0)
        liquidity = current_data.get("liquidity", 0)
        entry_liq = trade.get("entry_snapshot", {}).get("liquidity", 0)

        if buy_ratio < 0.5 and price_change_1h < -30:
            return "EXIT", f"Momentum collapse: buy ratio {buy_ratio}, 1h change {price_change_1h}%"

        if buy_ratio < 0.6 and vol_spike > 3.0 and price_change_1h < -20:
            return "EXIT", f"Whale exit signal: ratio {buy_ratio}, vol spike {vol_spike}x, 1h {price_change_1h}%"

        if entry_liq > 0 and liquidity < entry_liq * 0.3:
            return "EXIT", f"Liquidity drain: ${liquidity:,.0f} (was ${entry_liq:,.0f} at entry)"

        if buy_ratio >= 2.5 and vol_spike >= 2.0 and return_pct > 0:
            return "ADD", f"Strong momentum: buy ratio {buy_ratio}, vol spike {vol_spike}x"

    return "HOLD", f"Monitoring ({return_pct:+.1f}%)"


def close_paper_trade(pb: dict, trade: dict, exit_price: float, reason: str) -> dict:
    """Close a paper trade and record results."""
    entry = trade["entry_price"]
    return_pct = round(((exit_price - entry) / entry) * 100, 1)
    return_x = round(exit_price / entry, 2)
    peak_return = round(((trade.get("peak_price", entry) - entry) / entry) * 100, 1)

    result_tag = "WIN" if return_pct > 0 else "LOSS"
    now = datetime.now().isoformat()[:16]
    opened_at = trade.get("opened_at", now)

    try:
        open_dt = datetime.fromisoformat(opened_at)
        duration_mins = int((datetime.now() - open_dt).total_seconds() / 60)
        duration_str = f"{duration_mins // 60}h {duration_mins % 60}m" if duration_mins >= 60 else f"{duration_mins}m"
    except (ValueError, TypeError):
        duration_str = "unknown"

    snapshot = trade.get("entry_snapshot", {})

    closed_record = {
        "trade_id": trade["trade_id"],
        "token_name": trade["token_name"],
        "symbol": trade["symbol"],
        "contract": trade["contract"],
        "entry_price": entry,
        "exit_price": exit_price,
        "return_pct": return_pct,
        "return_x": return_x,
        "peak_return_pct": peak_return,
        "result": result_tag,
        "reason_closed": reason,
        "confidence_at_entry": trade.get("confidence", 0),
        "reason_entered": trade.get("reason", ""),
        "opened_at": opened_at,
        "closed_at": now,
        "duration": duration_str,
        "scans_monitored": trade.get("scans_monitored", 0),
        "tp1_hit": trade.get("tp1_hit", False),
        "tp2_hit": trade.get("tp2_hit", False),
        "slippage_pct": trade.get("slippage_pct", 0),
        "entry_vol_spike": snapshot.get("vol_spike", 0),
        "entry_buy_pressure": snapshot.get("buy_ratio_1h", 0),
        "entry_pressure_tier": snapshot.get("pressure_tier", "unknown"),
        "entry_whale_activity": snapshot.get("pressure_tier", "unknown"),
        "entry_market_cap": snapshot.get("market_cap", 0),
        "entry_mc_tier": snapshot.get("mc_tier", "unknown"),
        "entry_liquidity": snapshot.get("liquidity", 0),
        "entry_age_hours": snapshot.get("hours_old"),
        "failure_reason": reason if not return_pct > 0 else "",
    }

    # Blacklist losing tokens
    if return_pct < -20 and trade.get("contract"):
        blacklist_token(pb, trade["contract"], trade["token_name"], f"Paper trade lost {return_pct:.1f}%")

    # ---- LIVE TRADING: Execute real sell ----
    if TRADER_AVAILABLE and is_live_trading_enabled() and trade.get("live_trade", {}).get("success"):
        send_msg(
            f"\U0001f4b1 LIVE SELL ATTEMPT: {trade['token_name']} ({trade['symbol']})\n"
            f"Reason: {reason}"
        )
        try:
            sell_result = sell_token(trade["contract"])
            closed_record["live_sell"] = sell_result
            if sell_result["success"]:
                send_msg(
                    f"\u2705 LIVE SELL SUCCESS: {trade['token_name']}\n"
                    f"tx: {sell_result['signature']}"
                )
                log.info(f"LIVE SELL: {trade['token_name']} | tx: {sell_result['signature']}")
            else:
                send_msg(
                    f"\u274c LIVE SELL FAILED: {trade['token_name']}\n"
                    f"Error: {sell_result['error']}"
                )
                log.warning(f"LIVE SELL FAILED: {trade['token_name']} | {sell_result['error']}")
        except Exception as e:
            send_msg(f"\u274c LIVE SELL CRASH: {trade['token_name']}\nException: {str(e)[:300]}")
            log.error(f"LIVE SELL EXCEPTION: {trade['token_name']} | {e}")

    pb.setdefault("paper_trade_history", []).append(closed_record)
    pb["paper_trade_history"] = pb["paper_trade_history"][-TRIM["paper_trade_history"]:]
    update_paper_trade_stats(pb)

    return closed_record


def update_paper_trade_stats(pb: dict) -> None:
    """Recalculate paper trade performance statistics."""
    history = pb.get("paper_trade_history", [])
    if not history:
        return

    total = len(history)
    all_returns = [t["return_pct"] for t in history]
    all_x = [t.get("return_x", 1.0) for t in history]

    win_count = len([r for r in all_returns if r > 0])
    loss_count = total - win_count
    best = max(history, key=lambda t: t["return_pct"])
    worst = min(history, key=lambda t: t["return_pct"])

    streak = 0
    if history:
        last_result = history[-1]["return_pct"] > 0
        for t in reversed(history):
            if (t["return_pct"] > 0) == last_result:
                streak += 1 if last_result else -1
            else:
                break

    high_conf = [t for t in history if t.get("confidence_at_entry", 0) >= 8]
    strategy_accuracy = (
        round(len([t for t in high_conf if t["return_pct"] > 0]) / len(high_conf) * 100, 1)
        if high_conf else 0
    )

    pb["paper_trade_stats"] = {
        "total_trades": total,
        "wins": win_count,
        "losses": loss_count,
        "win_rate": round(win_count / max(total, 1) * 100, 1),
        "avg_return_pct": round(sum(all_returns) / max(total, 1), 1),
        "avg_return_x": round(sum(all_x) / max(total, 1), 2),
        "best_trade": {"token": best["token_name"], "return_pct": best["return_pct"], "return_x": best.get("return_x", 0)},
        "worst_trade": {"token": worst["token_name"], "return_pct": worst["return_pct"], "return_x": worst.get("return_x", 0)},
        "current_streak": streak,
        "strategy_accuracy_pct": strategy_accuracy,
    }


def monitor_paper_trades(pb: dict) -> Optional[str]:
    """Monitor all open paper trades."""
    trades = pb.get("paper_trades", [])
    if not trades:
        return None

    still_open: list[dict] = []
    report_lines: list[str] = []
    closed_lines: list[str] = []

    for trade in trades:
        contract = trade.get("contract", "")
        if not contract:
            still_open.append(trade)
            continue

        current_price = get_token_price(contract)
        current_data = get_token_market_data(contract)

        trade["scans_monitored"] = trade.get("scans_monitored", 0) + 1
        trade["last_update"] = datetime.now().isoformat()[:16]

        if current_price is None:
            if trade["scans_monitored"] >= DEAD_TOKEN_SCAN_LIMIT:
                closed = close_paper_trade(pb, trade, 0, "Token unreachable/rugged")
                closed_lines.append(
                    f"\U0001f480 {trade['trade_id']} {trade['token_name']} - "
                    f"RUGGED/DEAD (-100%) | Duration: {closed['duration']}"
                )
            else:
                still_open.append(trade)
                report_lines.append(
                    f"\u26a0\ufe0f {trade['trade_id']} {trade['symbol']}: "
                    f"Price unavailable (scan {trade['scans_monitored']}/{DEAD_TOKEN_SCAN_LIMIT})"
                )
            continue

        if current_price > trade.get("peak_price", 0):
            trade["peak_price"] = current_price
        if current_price < trade.get("lowest_price", float("inf")):
            trade["lowest_price"] = current_price

        trail_adjusted, new_sl = apply_trailing_stop(trade, current_price)
        action, reason = evaluate_trade_action(trade, current_price, current_data)
        return_pct = round(((current_price - trade["entry_price"]) / trade["entry_price"]) * 100, 1)

        trade.setdefault("updates", []).append({
            "scan": trade["scans_monitored"],
            "price": current_price,
            "action": action,
            "reason": reason,
            "time": datetime.now().isoformat()[:16],
        })
        trade["updates"] = trade["updates"][-20:]

        if action in ("EXIT", "EXIT_TP3"):
            closed = close_paper_trade(pb, trade, current_price, reason)
            emoji = "\u2705" if closed["return_pct"] > 0 else "\u274c"
            closed_lines.append(
                f"{emoji} {trade['trade_id']} {trade['token_name']} ({trade['symbol']})\n"
                f"   Entry: ${trade['entry_price']:.10g} \u2192 Exit: ${current_price:.10g}\n"
                f"   Return: {closed['return_pct']:+.1f}% ({closed['return_x']}x) | "
                f"Duration: {closed['duration']}\n"
                f"   Reason: {reason}"
            )
        elif action == "PARTIAL_TP1":
            trade["tp1_hit"] = True
            trade["stop_loss"] = trade["entry_price"]
            still_open.append(trade)
            report_lines.append(
                f"\U0001f4b0 {trade['trade_id']} {trade['symbol']}: TP1 HIT! "
                f"{return_pct:+.1f}% | SL moved to breakeven\n   {reason}"
            )
        elif action == "PARTIAL_TP2":
            trade["tp2_hit"] = True
            trade["stop_loss"] = trade["tp1"]
            still_open.append(trade)
            report_lines.append(
                f"\U0001f4b0\U0001f4b0 {trade['trade_id']} {trade['symbol']}: TP2 HIT! "
                f"{return_pct:+.1f}% | SL moved to TP1\n   {reason}"
            )
        elif action == "ADD":
            still_open.append(trade)
            report_lines.append(
                f"\U0001f7e2 {trade['trade_id']} {trade['symbol']}: "
                f"{return_pct:+.1f}% | STRONG SIGNAL\n   {reason}"
            )
        else:
            still_open.append(trade)
            emoji = "\U0001f4c8" if return_pct > 0 else "\U0001f4c9" if return_pct < 0 else "\u2796"
            peak_ret = round(((trade["peak_price"] - trade["entry_price"]) / trade["entry_price"]) * 100, 1)

            status_parts = []
            if trade.get("tp1_hit"):
                status_parts.append("TP1\u2705")
            if trade.get("tp2_hit"):
                status_parts.append("TP2\u2705")
            status_tag = " | ".join(status_parts)

            market_info = ""
            if current_data:
                market_info = f" | Buy: {current_data.get('buy_ratio_1h', 0)}x | Vol: {current_data.get('vol_spike', 0)}x"

            trail_info = f" | SL\u2191${new_sl:.10g}" if trail_adjusted else ""

            report_lines.append(
                f"{emoji} {trade['trade_id']} {trade['symbol']}: "
                f"{return_pct:+.1f}% (peak: +{peak_ret}%) | "
                f"HOLD{' | ' + status_tag if status_tag else ''}"
                f"{market_info}{trail_info}"
            )

    pb["paper_trades"] = still_open

    if not report_lines and not closed_lines:
        return None

    pt_stats = pb.get("paper_trade_stats", {})
    streak = pt_stats.get("current_streak", 0)
    streak_str = f"+{streak}W" if streak > 0 else f"{streak}L" if streak < 0 else "0"

    header = (
        f"\U0001f4b5 PAPER TRADE MONITOR\n"
        f"{'=' * 35}\n"
        f"Open: {len(still_open)}/{MAX_OPEN_PAPER_TRADES} | "
        f"Closed: {pt_stats.get('total_trades', 0)} | "
        f"Win rate: {pt_stats.get('win_rate', 0)}%\n"
        f"Avg return: {pt_stats.get('avg_return_pct', 0):+.1f}% | Streak: {streak_str}\n"
        f"{'=' * 35}\n"
    )

    sections = []
    if closed_lines:
        sections.append("\n\U0001f4cb CLOSED TRADES:\n" + "\n".join(closed_lines))
    if report_lines:
        sections.append("\n\U0001f50d OPEN POSITIONS:\n" + "\n".join(report_lines))

    return header + "\n".join(sections)


def queue_pending_paper_trades(pb: dict, stage1_result: str, research_text: str, scan_num: int) -> tuple[list, list]:
    """Queue high-confidence picks as PENDING paper trades with RugCheck safety gate."""
    open_trades = pb.get("paper_trades", [])
    pending = pb.get("pending_paper_trades", [])

    total_slots_used = len(open_trades) + len(pending)
    if total_slots_used >= MAX_OPEN_PAPER_TRADES:
        return [], []

    picks = extract_json_from_response(stage1_result)
    if not picks or not isinstance(picks, list):
        return [], []

    setups = parse_trade_setups(research_text) if research_text else {}

    eligible = sorted(
        [p for p in picks if p.get("confidence", 0) >= MIN_CONFIDENCE_FOR_TRADE],
        key=lambda x: x.get("confidence", 0),
        reverse=True,
    )
    if not eligible:
        return [], []

    used_contracts = {t["contract"] for t in open_trades} | {t["contract"] for t in pending}

    queued: list[dict] = []
    blocked: list[dict] = []
    slots = MAX_OPEN_PAPER_TRADES - total_slots_used

    for pick in eligible:
        if slots <= 0:
            break

        contract = pick.get("contract", "")
        if not contract or contract in used_contracts:
            continue

        if is_blacklisted(pb, contract):
            blocked.append({
                "name": pick.get("name", "?"),
                "symbol": pick.get("symbol", "?"),
                "contract": contract,
                "verdict": "BLACKLISTED",
                "risks": [pb.get("token_blacklist", {}).get(contract, {}).get("reason", "previously failed")],
                "confidence": pick.get("confidence", 0),
            })
            continue

        is_safe, safety_score, risk_names, verdict = check_token_safety(contract)
        if not is_safe:
            blocked.append({
                "name": pick.get("name", "?"),
                "symbol": pick.get("symbol", "?"),
                "contract": contract,
                "verdict": verdict,
                "risks": risk_names[:5],
                "confidence": pick.get("confidence", 0),
            })
            continue

        symbol = pick.get("symbol", "?").upper()
        current_price = get_token_price(contract)

        pending_entry = {
            "pick": pick,
            "trade_setup": setups.get(symbol, {}),
            "scan_num": scan_num,
            "contract": contract,
            "symbol": symbol,
            "name": pick.get("name", "?"),
            "rec_price": safe_float(str(pick.get("entry_price", "0")).replace("$", "").replace(",", "")),
            "price_at_queue": current_price,
            "confidence": pick.get("confidence", 0),
            "queued_at": datetime.now().isoformat()[:16],
            "safety_verdict": verdict,
            "safety_score": safety_score,
            "safety_risks": risk_names[:5],
        }

        pending.append(pending_entry)
        used_contracts.add(contract)
        queued.append(pending_entry)
        slots -= 1

    pb["pending_paper_trades"] = pending
    return queued, blocked


def confirm_pending_trades(pb: dict) -> tuple[list, list]:
    """Confirm or reject pending paper trades from the previous scan."""
    pending = pb.get("pending_paper_trades", [])
    if not pending:
        return [], []

    open_trades = pb.get("paper_trades", [])
    open_contracts = {t["contract"] for t in open_trades}

    confirmed: list[dict] = []
    rejected: list[dict] = []
    scan_num = pb.get("scans", 0) + 1

    for pt in pending:
        contract = pt.get("contract", "")

        if contract in open_contracts:
            rejected.append({"name": pt["name"], "reason": "Already has open trade"})
            continue

        if len(open_trades) >= MAX_OPEN_PAPER_TRADES:
            rejected.append({"name": pt["name"], "reason": f"Trade slots full ({MAX_OPEN_PAPER_TRADES}/{MAX_OPEN_PAPER_TRADES})"})
            continue

        current_price = get_token_price(contract)
        if current_price is None:
            rejected.append({"name": pt["name"], "reason": "Token unreachable after 1 scan"})
            continue

        rec_price = pt.get("rec_price", 0)
        if rec_price <= 0:
            rejected.append({"name": pt["name"], "reason": "Invalid recommendation price"})
            continue

        price_change = ((current_price - rec_price) / rec_price) * 100
        if price_change < PRICE_DUMP_REJECT_PCT:
            rejected.append({"name": pt["name"], "reason": f"Price dumped {price_change:.1f}% since recommendation"})
            continue

        current_data = get_token_market_data(contract)
        if current_data:
            buy_ratio = current_data.get("buy_ratio_1h", 1.0)
            if buy_ratio < BUY_PRESSURE_CONFIRM_MIN:
                rejected.append({"name": pt["name"], "reason": f"Buy pressure collapsed (ratio: {buy_ratio})"})
                continue

        pick = pt.get("pick", {})
        pick["entry_price"] = current_price
        trade = create_paper_trade(pb, pick, pt.get("trade_setup", {}), scan_num)
        if trade:
            trade["safety_verdict"] = pt.get("safety_verdict", "unknown")
            trade["safety_score"] = pt.get("safety_score", 0)
            trade["confirmed_from_pending"] = True
            trade["price_at_recommendation"] = rec_price
            trade["price_change_during_confirmation"] = round(price_change, 1)

            open_trades.append(trade)
            open_contracts.add(contract)
            confirmed.append(trade)

            # ---- LIVE TRADING: Execute real buy ----
            if TRADER_AVAILABLE and is_live_trading_enabled():
                max_sol = float(os.environ.get("MAX_SOL_PER_TRADE", "0.03"))
                send_msg(
                    f"\U0001f6a8 LIVE BUY ATTEMPT\n"
                    f"Token: {trade['token_name']} ({trade['symbol']})\n"
                    f"Contract: {contract[:20]}...\n"
                    f"Amount: {max_sol} SOL\n"
                    f"Price: ${current_price:.10g}"
                )
                try:
                    buy_result = buy_token(contract, max_sol)
                    trade["live_trade"] = buy_result
                    if buy_result["success"]:
                        send_msg(
                            f"\u2705 LIVE BUY SUCCESS!\n"
                            f"{trade['token_name']} ({trade['symbol']})\n"
                            f"tx: {buy_result['signature']}\n"
                            f"https://solscan.io/tx/{buy_result['signature']}"
                        )
                        log.info(f"LIVE BUY: {trade['token_name']} | tx: {buy_result['signature']}")
                    else:
                        send_msg(
                            f"\u274c LIVE BUY FAILED!\n"
                            f"{trade['token_name']} ({trade['symbol']})\n"
                            f"Error: {buy_result['error']}"
                        )
                        log.warning(f"LIVE BUY FAILED: {trade['token_name']} | {buy_result['error']}")
                except Exception as e:
                    error_msg = str(e)[:300]
                    send_msg(
                        f"\u274c LIVE BUY CRASH!\n"
                        f"{trade['token_name']} ({trade['symbol']})\n"
                        f"Exception: {error_msg}"
                    )
                    log.error(f"LIVE BUY EXCEPTION: {trade['token_name']} | {e}")
                    trade["live_trade"] = {"success": False, "signature": None, "error": error_msg}

    pb["pending_paper_trades"] = []
    pb["paper_trades"] = open_trades
    return confirmed, rejected


def format_pending_alert(queued: list, blocked: list) -> str:
    """Format Telegram message for queued and blocked trades."""
    lines: list[str] = []
    if queued:
        lines.append(f"\u23f3 QUEUED ({len(queued)}):")
        for pt in queued:
            lines.append(
                f"  \U0001f7e1 {pt['name']} ({pt['symbol']}) "
                f"conf {pt['confidence']}/10 | ${pt['rec_price']:.10g} | "
                f"{pt.get('safety_verdict', '?')}"
            )
    if blocked:
        lines.append(f"\U0001f6ab BLOCKED ({len(blocked)}):")
        for bt in blocked:
            lines.append(f"  \u274c {bt['name']} \u2014 {bt['verdict']}")
    return "\n".join(lines)


def format_confirmation_alert(confirmed: list, rejected: list) -> str:
    """Format Telegram message for confirmed/rejected pending trades."""
    lines: list[str] = []
    if confirmed:
        lines.append(f"\u2705 CONFIRMED ({len(confirmed)}):")
        for trade in confirmed:
            pc = trade.get("price_change_during_confirmation", 0)
            live_tag = ""
            if trade.get("live_trade", {}).get("success"):
                live_tag = " | LIVE BUY \u2705"
            elif trade.get("live_trade"):
                live_tag = " | LIVE BUY \u274c"
            lines.append(
                f"  \U0001f7e2 {trade['token_name']} ({trade['symbol']}) "
                f"${trade['entry_price']:.10g} ({pc:+.1f}%){live_tag}"
            )
    if rejected:
        lines.append(f"\u274c REJECTED ({len(rejected)}):")
        for rej in rejected:
            lines.append(f"  \U0001f534 {rej['name']}: {rej['reason']}")
    return "\n".join(lines)


def format_new_trade_alert(trade: dict) -> str:
    """Format a Telegram alert for a newly opened paper trade."""
    entry = trade["entry_price"]
    return (
        f"\U0001f514 PAPER TRADE #{trade['trade_id']}\n"
        f"{trade['token_name']} ({trade['symbol']})\n"
        f"Entry ${entry:.10g} | SL ${trade['stop_loss']:.10g}\n"
        f"TP1 ${trade['tp1']:.10g} ({round((trade['tp1'] / entry - 1) * 100)}%) | "
        f"TP2 ${trade['tp2']:.10g} ({round((trade['tp2'] / entry - 1) * 100)}%) | "
        f"TP3 ${trade['tp3']:.10g} ({round((trade['tp3'] / entry - 1) * 100)}%)\n"
        f"Conf {trade.get('confidence', 0)}/10 \u2014 {trade.get('reason', '?')}"
    )


# ============================================================
# STRUCTURED STAGE 2 EXTRACTION
# ============================================================

def extract_stage2_lessons(research_text: str, pb: dict) -> None:
    """Extract structured lessons from Stage 2 research output."""
    parsed = extract_json_from_response(research_text)

    if parsed and isinstance(parsed, dict):
        lessons = parsed.get("lessons", [])
        for lesson in lessons:
            if isinstance(lesson, str) and lesson.strip():
                existing_notes = [l.get("note", "") for l in pb.get("lessons", [])[-10:]]
                if not any(lesson.strip()[:50] in existing for existing in existing_notes):
                    pb.setdefault("lessons", []).append({
                        "date": datetime.now().isoformat()[:10],
                        "note": lesson.strip()[:500],
                    })

        rule_updates = parsed.get("rule_updates", {})
        if rule_updates:
            avoid = pb.setdefault("avoid_conditions", [])
            for new_rule in rule_updates.get("add", []):
                if isinstance(new_rule, str) and new_rule.strip() and new_rule not in avoid:
                    avoid.append(new_rule.strip())
            pb["avoid_conditions"] = avoid[-TRIM["avoid_conditions"]:]

        log.info(f"Extracted {len(lessons)} lessons from Stage 2 (structured)")
        return

    if "PLAYBOOK" in research_text and "UPDATE" in research_text:
        try:
            note = research_text.split("UPDATE")[-1]
            for split_kw in ("STRATEGY", "MISTAKE", "SELF-REFLECTION"):
                if split_kw in note:
                    note = note.split(split_kw)[0]
                    break
            note = note.strip(": \n")[:500]
            if note:
                existing_notes = [l.get("note", "") for l in pb.get("lessons", [])[-10:]]
                if not any(note[:50] in existing for existing in existing_notes):
                    pb.setdefault("lessons", []).append({
                        "date": datetime.now().isoformat()[:10],
                        "note": note,
                    })
                    log.info("Extracted lesson from Stage 2 (fallback)")
        except Exception as e:
            log.warning(f"Legacy lesson extraction failed: {e}")

    pb["lessons"] = pb.get("lessons", [])[-TRIM["lessons"]:]


# ============================================================
# TELEGRAM CONVERSATION HANDLER
# ============================================================

def get_telegram_updates(offset: Optional[int] = None) -> list[dict]:
    """Fetch new messages sent to the bot since last check."""
    try:
        params: dict[str, Any] = {"timeout": 5, "allowed_updates": ["message"]}
        if offset:
            params["offset"] = offset
        r = requests.get(f"{TAPI}/getUpdates", params=params, timeout=10)
        if r.status_code == 200:
            return r.json().get("result", [])
        else:
            log.warning(f"Telegram getUpdates failed (HTTP {r.status_code})")
    except requests.exceptions.RequestException as e:
        log.error(f"Telegram getUpdates error: {e}")
    return []


def build_chat_context(pb: dict) -> str:
    """Build concise scanner state summary for AI conversation."""
    trades = pb.get("paper_trades", [])
    stats = pb.get("paper_trade_stats", {})
    history = pb.get("paper_trade_history", [])[-5:]
    perf = pb.get("performance", {})
    rules = pb.get("strategy_rules", [])
    picks = pb.get("active_picks", [])[-5:]
    pending = pb.get("pending_paper_trades", [])

    total_picks = perf.get("total_picks", 0)
    win_rate = round((perf.get("wins", 0) / max(total_picks, 1)) * 100, 1) if total_picks > 0 else 0

    lines = [
        f"Scans: {pb.get('scans', 0)} | Picks: {total_picks} | Win rate: {win_rate}%",
        f"\nOPEN TRADES ({len(trades)}/{MAX_OPEN_PAPER_TRADES}):",
    ]
    for t in trades:
        cur = t.get("current_price", t["entry_price"])
        pnl = ((cur - t["entry_price"]) / t["entry_price"]) * 100
        lines.append(
            f"  #{t['trade_id']} {t['token_name']} ({t['symbol']}) | "
            f"Entry ${t['entry_price']:.10g} | PnL {pnl:+.1f}% | SL ${t['stop_loss']:.10g}"
        )
    if not trades:
        lines.append("  (none)")

    if pending:
        lines.append(f"\nPENDING ({len(pending)}):")
        for p in pending:
            lines.append(f"  {p['name']} ({p['symbol']}) conf {p['confidence']}/10 @ ${p['rec_price']:.10g}")

    if history:
        lines.append(f"\nLAST {len(history)} CLOSED:")
        for h in history:
            lines.append(
                f"  #{h.get('trade_id', '?')} {h.get('token_name', '?')} | "
                f"{h.get('result', '?')} {h.get('return_pct', 0):+.1f}% | {h.get('reason_closed', '?')}"
            )

    lines.append(
        f"\nSTATS: {stats.get('total_trades', 0)} trades | "
        f"{stats.get('wins', 0)}W-{stats.get('losses', 0)}L | "
        f"WR {stats.get('win_rate', 0)}% | Avg {stats.get('avg_return_pct', 0):+.1f}%"
    )

    if rules:
        lines.append("\nTOP RULES:")
        for r in rules[-5:]:
            lines.append(f"  - {r}")

    return "\n".join(lines)


def handle_user_messages(pb: dict) -> None:
    """Check for user messages and reply using AI with scanner context."""
    last_offset = pb.get("last_update_id", 0)
    updates = get_telegram_updates(offset=last_offset + 1 if last_offset else None)

    if not updates:
        return

    replies_sent = 0
    for update in updates:
        pb["last_update_id"] = update["update_id"]

        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if not text or chat_id != USER_ID:
            continue

        sanitized_text = text[:1000]

        context = build_chat_context(pb)
        system = (
            "You are a Solana memecoin scanner assistant. Short, direct replies. "
            "Talk casual like a trading buddy. Use the data below to answer.\n"
            "If user asks to close a trade or change something, confirm what you'd do "
            "and note it takes effect next scan cycle.\n"
            "Keep replies under 300 words.\n\n"
            f"{context}"
        )

        reply = call_groq(system, sanitized_text, temperature=0.6, timeout=30)
        if reply:
            send_msg(f"\U0001f4ac {reply[:TELEGRAM_MSG_LIMIT]}")
            replies_sent += 1

    if replies_sent:
        send_msg(f"\u2705 Replied to {replies_sent} message(s). Continuing scan...")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    """Main scan loop - runs once per GitHub Actions invocation."""
    log.info("=" * 50)
    log.info("SCANNER START")
    log.info("=" * 50)

    playbook = load_playbook()

    # ---- STEP 0: Clean expired blacklist entries ----
    clean_expired_blacklist(playbook)

    # ---- STEP 0.5: Reply to user messages ----
    handle_user_messages(playbook)

    scan_num = playbook.get("scans", 0) + 1
    perf = playbook.get("performance", {})
    total_picks = perf.get("total_picks", 0)
    win_rate = round((perf.get("wins", 0) / max(total_picks, 1)) * 100, 1) if total_picks > 0 else 0
    pt_stats = playbook.get("paper_trade_stats", {})
    pt_open = len(playbook.get("paper_trades", []))
    pt_pending = len(playbook.get("pending_paper_trades", []))
    blacklist_count = len([
        addr for addr in playbook.get("token_blacklist", {})
        if is_blacklisted(playbook, addr)
    ])

    # ---- Wallet status ----
    wallet_info = ""
    if TRADER_AVAILABLE:
        try:
            ws = get_wallet_summary()
            if "error" not in ws:
                mode = "LIVE" if ws["live_trading"] else "PAPER"
                wallet_info = f"\n\U0001f4b0 Wallet: {ws['balance_sol']} SOL | Mode: {mode} | Max: {ws['max_per_trade']} SOL/trade"
            else:
                wallet_info = f"\n\U0001f4b0 Wallet: error - {ws['error']}"
        except Exception as e:
            wallet_info = f"\n\U0001f4b0 Wallet: exception - {str(e)[:100]}"

    if not TRADER_AVAILABLE:
        wallet_info = f"\n\u274c TRADER OFFLINE: {_TRADER_IMPORT_ERROR}"

    # ---- RPC health check ----
    rpc_url = os.environ.get("SOLANA_RPC_URL", "")
    if not rpc_url:
        wallet_info += "\n\u26a0\ufe0f WARNING: SOLANA_RPC_URL is empty! Using public RPC (unreliable for trading)"

    send_msg(
        f"\U0001f50d Scan #{scan_num}\n"
        f"\U0001f9e0 {len(playbook.get('lessons', []))}pat | {total_picks}picks {win_rate}%wr | "
        f"{len(playbook.get('trade_memory', []))}mem | {len(playbook.get('strategy_rules', []))}rules\n"
        f"\U0001f4b5 {pt_open}/{MAX_OPEN_PAPER_TRADES} open | {pt_pending} pending | "
        f"{pt_stats.get('total_trades', 0)} closed {pt_stats.get('win_rate', 0)}%wr\n"
        f"\U0001f6ab {blacklist_count} blacklisted"
        f"{wallet_info}"
    )

    # ---- STEP 1: Check past picks ----
    tracker_report = check_past_picks(playbook)
    if tracker_report:
        send_msg(tracker_report)

    # ---- STEP 1.5: Monitor open paper trades ----
    pt_report = monitor_paper_trades(playbook)
    if pt_report:
        send_msg(pt_report)

    # ---- STEP 1.6: Confirm pending paper trades ----
    if playbook.get("pending_paper_trades"):
        confirmed, rejected = confirm_pending_trades(playbook)
        conf_alert = format_confirmation_alert(confirmed, rejected)
        if conf_alert:
            send_msg(conf_alert)
        for trade in confirmed:
            send_msg(format_new_trade_alert(trade))

    # ---- STEP 1.7: Update ROI tiers and evolve strategy rules ----
    update_roi_tiers(playbook)

    if scan_num % RULE_REGEN_INTERVAL == 0 and len(playbook.get("trade_memory", [])) >= MIN_TRADES_FOR_RULES:
        evolve_strategy_rules(playbook)
        rules = playbook.get("strategy_rules", [])
        if rules:
            rules_text = "\n".join(f"  {i + 1}. {r}" for i, r in enumerate(rules[-5:]))
            send_msg(f"\U0001f504 Rules evolved:\n{rules_text}")

    # ---- STEP 2: Gather new data ----
    tokens, stats = run_full_scan()

    if not tokens:
        send_msg("\U0001f634 Nothing trending. Next scan in 15m.")
        save_playbook(playbook)
        return

    token_data = "\n\n---\n\n".join(tokens)
    track_tokens(playbook, token_data)

    send_msg(f"\U0001f4ca {len(tokens)} tokens found. Running Stage 1...")

    # ---- STEP 3: Stage 1 - Quick scan ----
    scan_prompt = build_scan_prompt(playbook)
    stage1_result = call_groq(
        scan_prompt, f"Analyze:\n\n{token_data}",
        temperature=0.5, timeout=GROQ_TIMEOUT_STAGE1, max_tokens=GROQ_MAX_TOKENS_STAGE1,
    )

    if not stage1_result:
        send_msg(f"\u26a0\ufe0f Stage 1 failed: {_last_groq_error}\nNext cycle.")
        save_playbook(playbook)
        return

    save_new_picks(playbook, stage1_result)

    picks = extract_json_from_response(stage1_result)
    if picks and isinstance(picks, list):
        picks_summary = "\n".join(
            f"#{p.get('rank', '?')} {sanitize_for_prompt(p.get('name', '?'))} "
            f"({sanitize_for_prompt(p.get('symbol', '?'), 20)}) "
            f"conf {p.get('confidence', '?')}/10 \u2014 {p.get('reason', '?')}"
            for p in picks
        )
        send_msg(f"\U0001f3af Picks:\n{picks_summary}\n\U0001f52c Stage 2 running...")
    else:
        picks_summary = stage1_result[:500]
        send_msg("\U0001f3af Picks found. Stage 2...")

    # ---- STEP 4: Stage 2 - Deep research ----
    # Only include data for picked tokens to reduce prompt size
    picked_contracts = set()
    if picks and isinstance(picks, list):
        picked_contracts = {p.get("contract", "") for p in picks if p.get("contract")}

    if picked_contracts:
        filtered_tokens = [
            t for t in tokens
            if any(c in t for c in picked_contracts)
        ]
        if not filtered_tokens:
            filtered_tokens = tokens[:3]
    else:
        filtered_tokens = tokens[:3]

    filtered_token_data = "\n\n---\n\n".join(filtered_tokens)

    research_prompt = build_research_prompt(playbook)
    deep_prompt = (
        f"Top picks:\n{picks_summary}\n\n"
        f"Token data for picks:\n\n{filtered_token_data}\n\n"
        f"Do DEEP RESEARCH on each pick. Reference your real track record and strategy rules."
    )

    research = call_groq(
        research_prompt, deep_prompt,
        temperature=0.85, timeout=GROQ_TIMEOUT_STAGE2, max_tokens=GROQ_MAX_TOKENS_STAGE2,
    )

    if research:
        extract_stage2_lessons(research, playbook)
        send_msg(f"\U0001f4ca SCAN #{scan_num} RESEARCH\n{'=' * 30}\n\n{research}")
    else:
        send_msg(f"\u26a0\ufe0f Stage 2 failed: {_last_groq_error}\nStage 1 picks still valid.")

    # ---- STEP 5: Queue paper trades ----
    queued, blocked = queue_pending_paper_trades(
        playbook, stage1_result, research or "", scan_num
    )
    pending_alert = format_pending_alert(queued, blocked)
    if pending_alert:
        send_msg(pending_alert)

    open_count = len(playbook.get("paper_trades", []))
    pending_count = len(playbook.get("pending_paper_trades", []))
    if queued:
        send_msg(f"\U0001f4b5 {open_count}/{MAX_OPEN_PAPER_TRADES} open | {pending_count} pending | +{len(queued)} queued")
    elif not blocked:
        if open_count >= MAX_OPEN_PAPER_TRADES:
            send_msg(f"\U0001f4b5 Slots full ({MAX_OPEN_PAPER_TRADES}/{MAX_OPEN_PAPER_TRADES})")
        else:
            send_msg(f"\U0001f4b5 No picks \u2265{MIN_CONFIDENCE_FOR_TRADE} confidence")

    save_playbook(playbook)
    log.info(f"Scan #{scan_num} complete")


if __name__ == "__main__":
    main()
