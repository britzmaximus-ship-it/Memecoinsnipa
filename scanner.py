#!/usr/bin/env python3
"""
scanner.py
"""

import time
import random
import logging
import os
import json
import math
from datetime import datetime, timezone
import requests

from telegram_client import TelegramClient
from storage import Storage
from llm import LLMScorer
from trader import JupiterTrader


log = logging.getLogger("scanner")
logging.basicConfig(level=logging.INFO)

DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
SOLANA_CHAIN_ID = "solana"

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "600"))
MAX_NEW_PER_SCAN = int(os.getenv("MAX_NEW_PER_SCAN", "30"))

MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
MAX_MC_USD = float(os.getenv("MAX_MC_USD", "5000000"))

AGE_MIN_MINUTES = int(os.getenv("AGE_MIN_MINUTES", "10"))
AGE_MAX_MINUTES = int(os.getenv("AGE_MAX_MINUTES", "1440"))

MIN_ACCEL = float(os.getenv("MIN_ACCEL", "0.9"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "0.12"))

LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"

PLAYBOOK_PATH = os.getenv("PLAYBOOK_PATH", "playbook.json")


# ============================================================
# PLAYBOOK STATS
# ============================================================

def load_playbook():
    try:
        if not os.path.exists(PLAYBOOK_PATH):
            return {}
        with open(PLAYBOOK_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def compute_stats(playbook):
    open_trades = playbook.get("paper_trades", [])
    history = playbook.get("paper_trade_history", [])

    closed = [t for t in history if isinstance(t, dict)]

    wins = 0
    returns = []

    for t in closed:
        result = str(t.get("result", "")).upper()
        if result.startswith("WIN") or result in {"TP", "PROFIT"}:
            wins += 1

        rp = t.get("return_pct")
        if isinstance(rp, (int, float)) and math.isfinite(rp):
            returns.append(float(rp))
        else:
            entry = t.get("entry_price")
            exitp = t.get("exit_price")
            if isinstance(entry, (int, float)) and isinstance(exitp, (int, float)) and entry:
                returns.append((exitp - entry) / entry * 100)

    total_closed = len(closed)
    win_rate = (wins / total_closed * 100) if total_closed else 0.0
    avg_return = (sum(returns) / len(returns)) if returns else 0.0

    return {
        "open": len(open_trades),
        "closed": total_closed,
        "wins": wins,
        "win_rate": win_rate,
        "avg_return": avg_return
    }


def format_stats(stats):
    return (
        "\n📋 PICK TRACKER UPDATE\n"
        "===================================\n"
        f"Active: {stats['open']} | Closed: {stats['closed']}\n"
        f"Wins: {stats['wins']}\n"
        f"Win rate: {stats['win_rate']:.1f}%\n"
        f"Avg return: {stats['avg_return']:+.1f}%\n"
        "===================================\n"
    )


# ============================================================
# HELPERS
# ============================================================

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def minutes_since(ts_ms):
    return (time.time() * 1000 - ts_ms) / 60000.0


def compute_accel(pair):
    pc = safe_float(pair.get("priceChange", {}).get("h1", 0.0))
    vol = safe_float(pair.get("volume", {}).get("h1", 0.0))
    liq = safe_float((pair.get("liquidity") or {}).get("usd", 0.0))
    if liq <= 0:
        return 0.0
    return (abs(pc) / 100.0) + (vol / max(liq, 1.0))


def fetch_pairs(query):
    url = f"{DEXSCREENER_SEARCH_URL}?q={query}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    return [p for p in data.get("pairs", []) if p.get("chainId") == SOLANA_CHAIN_ID]


def is_pair_valid(p):
    created = p.get("pairCreatedAt")
    if not created:
        return False

    age_min = minutes_since(int(created))
    if not (AGE_MIN_MINUTES <= age_min <= AGE_MAX_MINUTES):
        return False

    liq = safe_float((p.get("liquidity") or {}).get("usd", 0.0))
    if liq < MIN_LIQUIDITY_USD:
        return False

    fdv = safe_float(p.get("fdv", 0.0))
    if fdv > MAX_MC_USD:
        return False

    accel = compute_accel(p)
    if accel < MIN_ACCEL:
        return False

    return True


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    tg = TelegramClient()
    store = Storage()
    llm = LLMScorer()
    trader = JupiterTrader()

    while True:
        try:
            store.state["scans"] = int(store.state.get("scans", 0)) + 1
            scan_id = store.state["scans"]

            pairs = fetch_pairs("pump")
            random.shuffle(pairs)
            pairs = pairs[: MAX_NEW_PER_SCAN * 3]

            candidates = []
            for p in pairs:
                if is_pair_valid(p):
                    candidates.append(p)

            scored = []
            for p in candidates[:MAX_NEW_PER_SCAN]:
                symbol = p.get("baseToken", {}).get("symbol", "UNK")
                mint = p.get("baseToken", {}).get("address", "")
                liq = safe_float((p.get("liquidity") or {}).get("usd", 0.0))
                fdv = safe_float(p.get("fdv", 0.0))
                accel = compute_accel(p)

                score = llm.score_token(
                    symbol=symbol,
                    mint=mint,
                    liquidity_usd=liq,
                    fdv_usd=fdv,
                    accel=accel,
                    pair=p,
                )

                if score >= MIN_SCORE:
                    scored.append({
                        "symbol": symbol,
                        "score": score,
                        "accel": accel,
                        "liq": liq,
                        "fdv": fdv
                    })

            scored.sort(key=lambda x: x["score"], reverse=True)

            store.monitor_paper_trades()

            opened_msgs = []
            for item in scored[:5]:
                msg = store.try_open_paper_trade(item)
                if msg:
                    opened_msgs.append(msg)

            stats = compute_stats(load_playbook())

            lines = []
            lines.append(f"Scan #{scan_id}")
            lines.append(f"Paper: open={stats['open']} closed={stats['closed']}")
            lines.append(
                f"Quality: MIN_ACCEL={MIN_ACCEL} MIN_SCORE={MIN_SCORE} age={AGE_MIN_MINUTES}-{AGE_MAX_MINUTES}m"
            )
            lines.append(f"Candidates: discovered={len(pairs)} filtered={len(candidates)} ranked={len(scored)}")
            lines.append("")

            if scored:
                lines.append("Top:")
                top = scored[0]
                lines.append(
                    f"{top['symbol']} score={top['score']:.3f} "
                    f"liq=${int(top['liq'])} accel={top['accel']:.2f}"
                )
            else:
                lines.append("Top: none")

            lines.append(format_stats(stats))

            msg = "\n".join(lines)
            tg.send(msg)

            store.save()

            time.sleep(SCAN_INTERVAL_SECONDS)

        except Exception as e:
            log.exception("Scan loop error: %s", e)
            try:
                tg.send(f"Scanner error: {e}")
            except Exception:
                pass
            time.sleep(15)


if __name__ == "__main__":
    main()