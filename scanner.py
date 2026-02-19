#!/usr/bin/env python3
"""
scanner.py

Main loop:
- Discover new pairs via DexScreener
- Filter / score via rules + (optional) LLM
- Monitor paper trades
- Send Telegram updates each scan
- Optional: performance-gated mirroring to LIVE trades (hooks left minimal)

IMPORTANT:
- DOES NOT import JupiterTrader class (your trader.py exports functions, not a class)
"""

import time
import random
import logging
import os
import json
import math
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

import requests

from telegram_client import TelegramClient
from storage import Storage

# Support both names used in your repo history:
# - some files/classes were called LLMScorer
# - you also showed a class named LLM with analyze()
try:
    from llm import LLMScorer  # type: ignore
except Exception:
    LLMScorer = None  # fallback

try:
    from llm import LLM  # type: ignore
except Exception:
    LLM = None  # fallback

# trader.py in your messages EXPORTS FUNCTIONS, not JupiterTrader.
# We import functions safely (and only use if LIVE_TRADING_ENABLED).
try:
    import trader as trader_mod  # type: ignore
except Exception:
    trader_mod = None

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
AGE_MAX_MINUTES = int(os.getenv("AGE_MAX_MINUTES", "2880"))  # you showed 10-2880m in logs

MIN_ACCEL = float(os.getenv("MIN_ACCEL", "0.9"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "0.12"))

LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"

# ============================================================
# PLAYBOOK STATS (for Telegram scan messages)
# ============================================================

PLAYBOOK_PATH = os.getenv("PLAYBOOK_PATH", "playbook.json")


def _load_playbook_json(path: str = PLAYBOOK_PATH) -> Dict[str, Any]:
    """Load playbook.json (best-effort). Returns {} if missing/bad."""
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _compute_paper_trade_stats(playbook: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute open/closed counts, win-rate, and average return% from playbook.

    Supports multiple schema versions:
    - paper_trades / paperTrades
    - paper_trade_history / paperTradeHistory
    - trades list
    """
    open_trades = playbook.get("paper_trades") or playbook.get("paperTrades") or []
    history = playbook.get("paper_trade_history") or playbook.get("paperTradeHistory") or []

    if not history and isinstance(playbook.get("trades"), list):
        maybe = [t for t in playbook.get("trades", []) if str(t.get("mode", "")).upper() == "PAPER"]
        history = maybe or playbook.get("trades", [])

    closed = [t for t in history if isinstance(t, dict)]
    wins = 0
    returns: List[float] = []

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


def _format_pick_tracker_block(stats: Dict[str, Any]) -> str:
    """Return the PICK TRACKER block (or empty string if not enough data)."""
    if not stats:
        return ""

    closed = int(stats.get("closed", 0) or 0)
    open_ = int(stats.get("open", 0) or 0)
    pending = int(stats.get("pending", 0) or 0)

    win_rate = stats.get("win_rate", None)
    avg_return = stats.get("avg_return", None)

    lines = []
    lines.append("📋 PICK TRACKER UPDATE")
    lines.append("===================================")
    lines.append(f"Active: {open_} | Pending: {pending} | Closed: {closed}")
    if win_rate is not None and avg_return is not None and closed > 0:
        lines.append(f"Win rate: {win_rate:.1f}% | Avg return: {avg_return:+.1f}%")
    return "\n".join(lines)


# ============================================================
# HELPERS
# ============================================================

def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def minutes_since(ts_ms: int) -> float:
    return (time.time() * 1000 - ts_ms) / 60000.0


def compute_accel(pair: Dict[str, Any]) -> float:
    """
    Basic 'acceleration' proxy:
    - Use price change and volume change (if available) as a heuristic.
    """
    pc = safe_float((pair.get("priceChange") or {}).get("h1", 0.0), 0.0)
    vol = safe_float((pair.get("volume") or {}).get("h1", 0.0), 0.0)
    liq = safe_float((pair.get("liquidity") or {}).get("usd", 0.0), 0.0)
    if liq <= 0:
        return 0.0
    return (abs(pc) / 100.0) + (vol / max(liq, 1.0))


def fetch_pairs(query: str) -> List[Dict[str, Any]]:
    url = f"{DEXSCREENER_SEARCH_URL}?q={query}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    pairs = data.get("pairs", []) or []
    return [p for p in pairs if p.get("chainId") == SOLANA_CHAIN_ID]


def is_pair_age_ok(pair: Dict[str, Any]) -> bool:
    created = pair.get("pairCreatedAt")
    if not created:
        return False
    try:
        age_min = minutes_since(int(created))
    except Exception:
        return False
    return AGE_MIN_MINUTES <= age_min <= AGE_MAX_MINUTES


def is_pair_liquidity_ok(pair: Dict[str, Any]) -> bool:
    liq = safe_float((pair.get("liquidity") or {}).get("usd", 0.0), 0.0)
    return liq >= MIN_LIQUIDITY_USD


def is_pair_mc_ok(pair: Dict[str, Any]) -> bool:
    fdv = safe_float(pair.get("fdv", 0.0), 0.0)
    if fdv <= 0:
        return True
    return fdv <= MAX_MC_USD


def get_pair_symbol(pair: Dict[str, Any]) -> str:
    base = (pair.get("baseToken") or {}).get("symbol") or "UNK"
    return str(base).strip()


def get_pair_mint(pair: Dict[str, Any]) -> str:
    base = (pair.get("baseToken") or {}).get("address") or ""
    return str(base).strip()


def build_llm_client():
    """
    Try to build whichever LLM implementation exists in your repo:
    - LLMScorer with score_token(...)
    - OR LLM with analyze(...)
    """
    if LLMScorer is not None:
        try:
            return LLMScorer()
        except Exception as e:
            log.warning("LLMScorer init failed: %s", e)

    if LLM is not None:
        try:
            return LLM()
        except Exception as e:
            log.warning("LLM init failed: %s", e)

    return None


# ============================================================
# MAIN
# ============================================================

def main():
    tg = TelegramClient()
    store = Storage()
    llm_client = build_llm_client()

    while True:
        scan_start = time.time()
        try:
            store.state["scans"] = int(store.state.get("scans", 0)) + 1
            scan_id = store.state["scans"]

            pairs = fetch_pairs("pump")
            random.shuffle(pairs)
            pairs = pairs[: MAX_NEW_PER_SCAN * 3]

            candidates: List[Tuple[Dict[str, Any], float]] = []
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

            scored: List[Dict[str, Any]] = []
            # If we don't have a working LLM, we still rank by accel so bot continues.
            for p, accel in candidates[:MAX_NEW_PER_SCAN]:
                symbol = get_pair_symbol(p)
                mint = get_pair_mint(p)
                fdv = safe_float(p.get("fdv", 0.0), 0.0)
                liq = safe_float((p.get("liquidity") or {}).get("usd", 0.0), 0.0)

                score = None
                if llm_client is not None:
                    try:
                        # LLMScorer path
                        if hasattr(llm_client, "score_token"):
                            score = llm_client.score_token(
                                symbol=symbol,
                                mint=mint,
                                liquidity_usd=liq,
                                fdv_usd=fdv,
                                accel=accel,
                                pair=p,
                            )
                        # LLM.analyze path (returns text). If only analyze exists, we skip numeric scoring.
                        elif hasattr(llm_client, "analyze"):
                            score = None
                    except Exception as e:
                        log.warning("LLM scoring failed: %s", e)
                        score = None

                # Fallback numeric score if none
                numeric_score = float(score) if isinstance(score, (int, float)) else accel

                if numeric_score < MIN_SCORE:
                    continue

                scored.append(
                    {
                        "symbol": symbol,
                        "mint": mint,
                        "score": numeric_score,
                        "accel": accel,
                        "liq": liq,
                        "fdv": fdv,
                        "url": p.get("url", ""),
                        "pair": p,
                    }
                )

            scored.sort(key=lambda x: x["score"], reverse=True)

            # Monitor paper trades (should be robust inside storage)
            try:
                store.monitor_paper_trades()
            except Exception as e:
                log.warning("monitor_paper_trades error: %s", e)

            # Auto-open paper trades for top scored candidates
            opened_msgs: List[str] = []
            for item in scored[:5]:
                try:
                    msg = store.try_open_paper_trade(item)
                    if msg:
                        opened_msgs.append(msg)
                except Exception as e:
                    log.warning("try_open_paper_trade error: %s", e)

            # Live gate message only (no auto trading here to avoid crashes)
            live_msgs: List[str] = []
            if LIVE_TRADING_ENABLED:
                try:
                    if hasattr(store, "evaluate_live_gate") and store.evaluate_live_gate():
                        live_msgs.append("LIVE gate: ✅ enabled (performance-gated)")
                    else:
                        live_msgs.append("LIVE gate: ❌ disabled (needs better paper performance)")
                except Exception as e:
                    live_msgs.append(f"LIVE gate: ⚠️ error ({e})")
            else:
                live_msgs.append("LIVE trading: OFF (LIVE_TRADING_ENABLED=false)")

            # Build Top picks lines
            top_lines: List[str] = []
            for i, x in enumerate(scored[:10], 1):
                top_lines.append(
                    f"{i:02d}. {x['symbol']} | score={x['score']:.3f} | accel={x['accel']:.3f} | liq=${x['liq']:.0f} | fdv=${x['fdv']:.0f}"
                )

            playbook_stats = _compute_paper_trade_stats(_load_playbook_json())
            pick_tracker_block = _format_pick_tracker_block(playbook_stats)

            # Paper counts fallback
            open_fallback = 0
            try:
                open_fallback = len(store.get_open_paper_trades())
            except Exception:
                pass

            lines: List[str] = [
                f"Scan #{scan_id}",
                f"Candidates: discovered={len(pairs)} filtered={len(candidates)} ranked={len(scored)}",
                f"Quality: MIN_ACCEL={MIN_ACCEL} MIN_SCORE={MIN_SCORE} age={AGE_MIN_MINUTES}-{AGE_MAX_MINUTES}m",
                f"Paper: open={playbook_stats.get('open', open_fallback)} closed={playbook_stats.get('closed', 0)}",
                "",
                "Top picks:",
                *(top_lines if top_lines else ["(none)"]),
            ]

            if pick_tracker_block:
                lines.append("")
                lines.append(pick_tracker_block)

            if opened_msgs:
                lines.append("")
                lines.append("Opened paper trades:")
                lines.extend([f"- {m}" for m in opened_msgs])

            if live_msgs:
                lines.append("")
                lines.append("Live trading:")
                lines.extend([f"- {m}" for m in live_msgs])

            tg.send("\n".join(lines))

            try:
                store.save()
            except Exception as e:
                log.warning("store.save error: %s", e)

        except Exception as e:
            log.exception("Scan loop error: %s", e)
            try:
                tg.send(f"Scanner error: {e}")
            except Exception:
                pass

        # sleep
        elapsed = time.time() - scan_start
        time.sleep(max(5, SCAN_INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    main()