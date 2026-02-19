#!/usr/bin/env python3
"""
scanner.py

Main loop:
- Discover new pairs via DexScreener
- Filter / score via rules + LLM
- Open paper trades + monitor paper trades
- Optional: performance-gated mirroring to LIVE trades
- Send Telegram updates each scan
"""

import time
import random
import logging
import os
import json
import math
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple

import requests

from telegram_client import TelegramClient
from storage import Storage
from llm import LLM

# IMPORTANT: trader.py exports FUNCTIONS, not a JupiterTrader class.
import trader as trader_mod

log = logging.getLogger("scanner")
logging.basicConfig(level=logging.INFO)

# ============================================================
# CONFIG
# ============================================================

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
# PLAYBOOK STATS (for Telegram scan messages)
# ============================================================

def _load_playbook_json(path: str = PLAYBOOK_PATH) -> dict:
    """Load playbook.json (best-effort). Returns {} if missing/bad."""
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _compute_paper_trade_stats(playbook: dict) -> dict:
    """Compute open/closed counts, win-rate, and average return% from playbook."""
    open_trades = playbook.get("paper_trades") or playbook.get("paperTrades") or []
    history = playbook.get("paper_trade_history") or playbook.get("paperTradeHistory") or []

    # Future-proof: some people store all trades in one list
    if not history and isinstance(playbook.get("trades"), list):
        maybe = [t for t in playbook.get("trades", []) if str(t.get("mode", "")).upper() == "PAPER"]
        history = maybe or playbook.get("trades", [])

    closed = [t for t in history if isinstance(t, dict)]
    wins = 0
    returns = []

    for t in closed:
        result = str(t.get("result", t.get("outcome", ""))).upper()
        if result.startswith("WIN") or result in {"TP", "PROFIT"}:
            wins += 1

        rp = t.get("return_pct", t.get("returnPercent", None))
        if isinstance(rp, (int, float)) and math.isfinite(rp):
            returns.append(float(rp))
        else:
            entry = t.get("entry_price", t.get("entryPrice"))
            exitp = t.get("exit_price", t.get("exitPrice", t.get("close_price", t.get("closePrice"))))
            if isinstance(entry, (int, float)) and isinstance(exitp, (int, float)) and entry:
                returns.append((float(exitp) - float(entry)) / float(entry) * 100.0)

    closed_n = len(closed)
    win_rate = (wins / closed_n * 100.0) if closed_n else None
    avg_return = (sum(returns) / len(returns)) if returns else None

    return {
        "open": len(open_trades) if isinstance(open_trades, list) else 0,
        "pending": int(playbook.get("pending_paper", playbook.get("pendingPaper", 0)) or 0),
        "closed": closed_n,
        "wins": wins,
        "win_rate": win_rate,
        "avg_return": avg_return,
    }


def _format_pick_tracker_block(stats: dict) -> str:
    """Return the PICK TRACKER block (or empty string if not enough data)."""
    if not stats:
        return ""

    closed = stats.get("closed", 0) or 0
    open_ = stats.get("open", 0) or 0
    pending = stats.get("pending", 0) or 0

    win_rate = stats.get("win_rate", None)
    avg_return = stats.get("avg_return", None)

    lines = []
    lines.append("📋 PICK TRACKER UPDATE")
    lines.append("===================================")
    lines.append(f"Active: {open_} | Pending: {pending} | Closed: {closed}")
    if win_rate is not None and avg_return is not None and closed:
        lines.append(f"Win rate: {win_rate:.1f}% | Avg return: {avg_return:+.1f}%")
    return "\n".join(lines)


# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc).isoformat()


def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def minutes_since(ts_ms: int) -> float:
    return (time.time() * 1000 - ts_ms) / 60000.0


def compute_accel(pair: dict) -> float:
    """
    Basic 'acceleration' proxy:
    - Use price change and volume change (if available) as a heuristic.
    """
    pc = safe_float(pair.get("priceChange", {}).get("h1", 0.0), 0.0)
    vol = safe_float(pair.get("volume", {}).get("h1", 0.0), 0.0)
    liq = safe_float((pair.get("liquidity") or {}).get("usd", 0.0), 0.0)
    if liq <= 0:
        return 0.0
    return (abs(pc) / 100.0) + (vol / max(liq, 1.0))


def fetch_pairs(query: str) -> List[dict]:
    url = f"{DEXSCREENER_SEARCH_URL}?q={query}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    pairs = data.get("pairs", []) or []
    return [p for p in pairs if p.get("chainId") == SOLANA_CHAIN_ID]


def is_pair_age_ok(pair: dict) -> bool:
    created = pair.get("pairCreatedAt")
    if not created:
        return False
    try:
        age_min = minutes_since(int(created))
    except Exception:
        return False
    return AGE_MIN_MINUTES <= age_min <= AGE_MAX_MINUTES


def is_pair_liquidity_ok(pair: dict) -> bool:
    liq = safe_float((pair.get("liquidity") or {}).get("usd", 0.0), 0.0)
    return liq >= MIN_LIQUIDITY_USD


def is_pair_mc_ok(pair: dict) -> bool:
    fdv = safe_float(pair.get("fdv", 0.0), 0.0)
    if fdv <= 0:
        return True
    return fdv <= MAX_MC_USD


def get_pair_symbol(pair: dict) -> str:
    base = (pair.get("baseToken") or {}).get("symbol") or "UNK"
    return str(base).strip()


def get_pair_mint(pair: dict) -> str:
    base = (pair.get("baseToken") or {}).get("address") or ""
    return str(base).strip()


def safe_tg_send(tg: TelegramClient, text: str):
    try:
        tg.send(text)
    except Exception as e:
        log.warning("Telegram send failed: %s", e)


# ============================================================
# MAIN
# ============================================================

def main():
    tg = TelegramClient()
    store = Storage()
    llm = LLM()

    log.info("scanner main() started OK")

    while True:
        scan_start = time.time()
        try:
            store.state["scans"] = int(store.state.get("scans", 0)) + 1
            scan_id = store.state["scans"]

            # Pull candidates
            pairs = fetch_pairs("pump")
            random.shuffle(pairs)
            pairs = pairs[: MAX_NEW_PER_SCAN * 3]

            candidates: List[Tuple[dict, float]] = []
            for p in pairs:
                if not is_pair_age_ok(p):
                    continue
                if not is_pair_liquidity_ok(p):
                    continue
                if not is_pair_mc_ok(p):
                    continue

                accel = compute_accel(p)
                if accel < MIN_ACCEL:
                    continue

                candidates.append((p, accel))

            # Build a compact payload for the LLM
            compact_candidates = []
            for p, accel in candidates[:MAX_NEW_PER_SCAN]:
                compact_candidates.append({
                    "symbol": get_pair_symbol(p),
                    "mint": get_pair_mint(p),
                    "accel": round(accel, 4),
                    "liq": int(safe_float((p.get("liquidity") or {}).get("usd", 0.0), 0.0)),
                    "fdv": int(safe_float(p.get("fdv", 0.0), 0.0)),
                    "url": p.get("url", ""),
                })

            # LLM analysis (non-fatal)
            llm_text = None
            try:
                llm_text = llm.analyze({
                    "scan": scan_id,
                    "filters": {
                        "age_min": AGE_MIN_MINUTES,
                        "age_max": AGE_MAX_MINUTES,
                        "min_liq": MIN_LIQUIDITY_USD,
                        "max_mc": MAX_MC_USD,
                        "min_accel": MIN_ACCEL,
                        "min_score": MIN_SCORE,
                    },
                    "candidates": compact_candidates[:20],
                })
            except Exception as e:
                log.warning("LLM analyze failed: %s", e)
                llm_text = None

            # Monitor paper trades (should not crash scanner)
            try:
                store.monitor_paper_trades()
            except Exception as e:
                log.warning("monitor_paper_trades failed: %s", e)

            # Score candidates (your Storage likely does internal scoring/opening;
            # if not, keep your existing store.try_open_paper_trade logic)
            opened_msgs = []
            try:
                # If you have your own scoring elsewhere, keep it there.
                # Here we do a simple “open from top candidates” hook if your Storage supports it.
                for item in compact_candidates[:5]:
                    # Fake score placeholder so your existing function can work
                    item2 = dict(item)
                    item2["score"] = item2.get("score", 0.0)
                    msg = store.try_open_paper_trade(item2)
                    if msg:
                        opened_msgs.append(msg)
            except Exception as e:
                log.warning("try_open_paper_trade failed: %s", e)

            # Live gate summary (non-fatal)
            gate_text = ""
            try:
                wallet = trader_mod.get_wallet_summary()
                gate_text = f"Wallet: {wallet.get('balance_sol','?')} SOL live_env={trader_mod.is_live_trading_enabled()}"
            except Exception as e:
                gate_text = f"Wallet: error ({e})"

            # Playbook stats
            playbook_stats = _compute_paper_trade_stats(_load_playbook_json())
            pick_tracker_block = _format_pick_tracker_block(playbook_stats)

            # Message body
            lines = [
                f"Scan #{scan_id}",
                f"Paper: open={playbook_stats.get('open', 0)} closed={playbook_stats.get('closed', 0)}",
                gate_text,
                f"Quality: MIN_ACCEL={MIN_ACCEL} MIN_SCORE={MIN_SCORE} age={AGE_MIN_MINUTES}-{AGE_MAX_MINUTES}m",
                f"Candidates: discovered={len(pairs)} filtered={len(candidates)}",
            ]

            if pick_tracker_block:
                lines.append("")
                lines.append(pick_tracker_block)

            if llm_text:
                lines.append("")
                lines.append("🧠 LLM:")
                lines.append(llm_text.strip()[:1200])

            if opened_msgs:
                lines.append("")
                lines.append("Opened paper trades:")
                lines.extend([f"- {m}" for m in opened_msgs])

            safe_tg_send(tg, "\n".join(lines))

            # Save state (non-fatal)
            try:
                store.save()
            except Exception as e:
                log.warning("store.save failed: %s", e)

        except Exception as e:
            log.exception("Scan loop error: %s", e)
            safe_tg_send(tg, f"Scanner error: {e}")

        elapsed = time.time() - scan_start
        sleep_for = max(1, SCAN_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
    def run_forever():
    # run.py expects this symbol
    main()