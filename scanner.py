"""
scanner.py - Solana Memecoin Scanner (Working Version)

Fetches tokens from DexScreener, analyzes with Groq, sends plain-text Telegram alerts.
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
from typing import Any, Optional, List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# CONSTANTS
# ============================================================

MAX_TOKENS_PER_SCAN = 12
MIN_MARKET_CAP = 5000

API_RATE_LIMIT_DELAY = 0.4
API_MAX_RETRIES = 2
API_TIMEOUT = 10

TELEGRAM_MSG_LIMIT = 4000
TELEGRAM_SEND_TIMEOUT = 10

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

if not BOT_TOKEN or not USER_ID or not GROQ_KEY:
    raise EnvironmentError("Missing TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID or GROQ_API_KEY")

# ============================================================
# TELEGRAM (Plain text)
# ============================================================

def send_msg(text: str) -> None:
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
            log.warning(f"Timeout on {url} (attempt {attempt + 1})")
        except requests.exceptions.RequestException as e:
            log.error(f"Request failed on {url}: {e}")
            break

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

# ============================================================
# DEX DATA FETCHING
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
    except Exception as e:
        log.warning(f"DexScreener parse error for {contract[:12]}...: {e}")
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

    buy_ratio_1h = round(buys_1h / max(sells_1h, 1), 2) if sells_1h else 0

    avg_hourly = vol_24h / 24 if vol_24h > 0 else 0
    vol_spike = round(vol_1h / max(avg_hourly, 1), 2) if avg_hourly > 0 else 0

    dex_id = pair.get("dexId", "unknown")
    base_token = pair.get("baseToken", {})
    token_name = base_token.get("name", "?")
    token_symbol = base_token.get("symbol", "?")
    url = pair.get("url", "?")

    return {
        "price": price,
        "market_cap": mc,
        "liquidity": lq,
        "volume_1h": vol_1h,
        "volume_24h": vol_24h,
        "vol_spike": vol_spike,
        "buy_ratio_1h": buy_ratio_1h,
        "price_change_1h": safe_float(pc.get("h1")),
        "dex": dex_id,
        "token_name": token_name,
        "token_symbol": token_symbol,
        "url": url,
    }

def format_token_for_prompt(parsed: dict, contract: str, source: str) -> str:
    name = parsed["token_name"]
    symbol = parsed["token_symbol"]
    return (
        f"Token: {name} ({symbol})\n"
        f"Contract: {contract}\n"
        f"Source: {source}\n"
        f"Price: ${parsed['price']}\n"
        f"MC: ${parsed['market_cap']:,.0f} | Liq: ${parsed['liquidity']:,.0f}\n"
        f"Vol 1h: ${parsed['volume_1h']:,.0f} | Spike: {parsed['vol_spike']}x\n"
        f"Buy/Sell 1h: {parsed['buy_ratio_1h']}\n"
        f"URL: {parsed['url']}\n"
        "----------------------------------------\n"
    )

# ============================================================
# DATA SOURCES
# ============================================================

def fetch_boosted_tokens() -> list[dict]:
    r = rate_limited_get("https://api.dexscreener.com/token-boosts/top/v1")
    if r is None or r.status_code != 200:
        return []
    try:
        tokens = r.json()[:30]
        return [{"addr": t.get("tokenAddress", ""), "source": "boosted"} for t in tokens if t.get("chainId") == "solana" and t.get("tokenAddress")]
    except:
        return []

def fetch_latest_profiles() -> list[dict]:
    r = rate_limited_get("https://api.dexscreener.com/token-profiles/latest/v1")
    if r is None or r.status_code != 200:
        return []
    try:
        tokens = r.json()[:30]
        return [{"addr": t.get("tokenAddress", ""), "source": "profile"} for t in tokens if t.get("chainId") == "solana" and t.get("tokenAddress")]
    except:
        return []

def fetch_new_pairs() -> list[dict]:
    r = rate_limited_get("https://api.dexscreener.com/token-boosts/latest/v1")
    if r is None or r.status_code != 200:
        return []
    try:
        tokens = r.json()[:30]
        return [{"addr": t.get("tokenAddress", ""), "source": "new/pumpfun"} for t in tokens if t.get("chainId") == "solana" and t.get("tokenAddress")]
    except:
        return []

def fetch_scan_data(token_list: list[dict]) -> list[str]:
    results = []
    seen = set()

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
    seen = set()
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
# GROQ CALL
# ============================================================

def call_groq(system: str, prompt: str, temperature: float = 0.6) -> Optional[str]:
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama3-70b-8192",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt[:8000]},
                ],
                "temperature": temperature,
                "max_tokens": 400,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            log.error(f"Groq error {resp.status_code}: {resp.text[:200]}")
            return None
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"Groq request failed: {e}")
        return None

# ============================================================
# MAIN
# ============================================================

def main() -> None:
    log.info("=" * 50)
    log.info("SCANNER START")
    log.info("=" * 50)

    send_msg("Scanner started - Telegram connection OK! 🚀")

    tokens, stats = run_full_scan()

    if not tokens:
        log.info("No tokens found this scan")
        send_msg("Scan complete. No trending tokens found this time.")
        return

    token_data = "\n\n---\n\n".join(tokens)
    log.info(f"Found {len(tokens)} tokens")

    # Stage 1: Quick Groq picks
    system_prompt = "You are a sharp Solana memecoin scanner. From the list below, pick the TOP 3 tokens with the best short-term pump potential. Reply ONLY with a numbered list: 1. Name (Symbol) - Contract - Reason (short)."
    groq_response = call_groq(system_prompt, token_data, temperature=0.6)

    picks_text = groq_response.strip() if groq_response else "No picks (Groq failed)"

    send_msg(
        f"Scan complete!\n"
        f"Found {len(tokens)} promising tokens.\n\n"
        f"Groq Top Picks:\n{picks_text}\n\n"
        f"Stats: {stats}"
    )

if __name__ == "__main__":
    main()