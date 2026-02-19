#!/usr/bin/env python3
"""
scanner.py

Main loop:
- Discover new pairs via DexScreener
- Filter / score via rules (+ optional LLM text analysis if you want later)
- Open paper trades + monitor paper trades
- Optional: performance-gated mirroring to LIVE trades
- Send Telegram updates each scan

NOTE:
- Railway run.py imports: `from scanner import run_forever`
  so this file MUST provide run_forever().
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
from llm import LLM  # <-- FIX: your llm.py defines LLM, not LLMScorer
from trader import JupiterTrader

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
    """
    Compute open/closed counts, win-rate, and average return% from playbook.
    This is defensive because playbook schemas vary.
    """
    open_trades = playbook.get("paper_trades") or playbook.get("paperTrades") or []
    history = playbook.get("paper_trade_history") or playbook.get("paperTradeHistory") or []

    # Fallback: some builds store everything under 'trades'
    if not history and isinstance(playbook.get("trades"), list):
        maybe = [
            t for t in playbook.get("trades", [])
            if isinstance(t, dict) and str(t.get("mode", "")).upper() == "PAPER"
        ]
        history = maybe or playbook.get("trades", [])

    closed = [t for t in history if isinstance(t, dict)]

    wins = 0
    returns: List[float] = []

    for t in closed:
        # win detection
        # supports: result="WIN", win=True, outcome="TP", etc.
        if t.get("win") is True:
            wins += 1
        else:
            result = str(t.get("result", t.get("outcome", ""))).upper()
            if result.startswith("WIN") or result in {"TP", "PROFIT"}:
                wins += 1

        # return% extraction
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
    """Return a stats block for Telegram (or empty string if not enough data)."""
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
    if closed and win_rate is not None:
        if avg_return is not None:
            lines.append(f"Win rate: {win_rate:.1f}% | Avg return: {avg_return:+.1f}%")
        else:
            lines.append(f"Win rate: {win_rate:.1f}%")
    return "\n".join(lines)


# ============================================================
# HELPERS
# ============================================================

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def minutes_since(ts_ms: int) -> float:
    return (time.time() * 1000 - ts_ms) / 60000.0

def fetch_pairs(query: str) -> List[dict]:
    url = f"{DEXSCREENER_SEARCH_URL}?q={query}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    pairs = data.get("pairs", []) or []
    return [p for p in pairs if p.get("chainId") == SOLANA_CHAIN_ID]

def is_pair_age_ok(pair: dict) -> Tuple[bool, Optional[float]]:
    created = pair.get("pairCreatedAt")
    if not created:
        return False, None
    age_min = minutes_since(int(created))
    return (AGE_MIN_MINUTES <= age_min <= AGE_MAX_MINUTES), age_min

def is_pair_liquidity_ok(pair: dict) -> bool:
    liq = safe_float((pair.get("liquidity") or {}).get("usd", 0.0), 0.0)
    return liq >= MIN_LIQUIDITY_USD

def is_pair_mc_ok(pair: dict) -> bool:
    fdv = safe_float(pair.get("fdv", 0.0), 0.0)
    if fdv <= 0:
        return True
    return fdv <= MAX_MC_USD

def compute_accel(pair: dict) -> float:
    # same heuristic as before
    pc = safe_float((pair.get("priceChange") or {}).get("h1", 0.0), 0.0)
    vol = safe_float((pair.get("volume") or {}).get("h1", 0.0), 0.0)
    liq = safe_float((pair.get("liquidity") or {}).get("usd", 0.0), 0.0)
    if liq <= 0:
        return 0.0
    return (abs(pc) / 100.0) + (vol / max(liq, 1.0))

def accel_to_score(accel: float) -> float:
    """
    Convert accel into a 0..1-ish score so MIN_SCORE=0.12 makes sense.
    accel=1.0 -> 0.50
    accel=0.9 -> 0.47
    accel=2.0 -> 0.67
    """
    if accel <= 0:
        return 0.0
    return accel / (1.0 + accel)

def get_pair_symbol(pair: dict) -> str:
    base = (pair.get("baseToken") or {}).get("symbol") or "UNK"
    return str(base).strip()

def get_pair_mint(pair: dict) -> str:
    base = (pair.get("baseToken") or {}).get("address") or ""
    return str(base).strip()

# ============================================================
# LOOP
# ============================================================

def run_forever():
    tg = TelegramClient()
    store = Storage()
    llm = LLM()  # currently optional; not required to score numerically
    trader = JupiterTrader()

    while True:
        try:
            scan_start = time.time()

            store.state["scans"] = int(store.state.get("scans", 0)) + 1
            scan_id = store.state["scans"]

            # 1) Discover
            pairs = fetch_pairs("pump")
            discovered = len(pairs)

            random.shuffle(pairs)
            pairs = pairs[: MAX_NEW_PER_SCAN * 3]

            # 2) Filter
            filtered_candidates: List[Tuple[dict, float, float]] = []  # (pair, accel, age_min)
            for p in pairs:
                ok_age, age_min = is_pair_age_ok(p)
                if not ok_age:
                    continue
                if not is_pair_liquidity_ok(p):
                    continue
                if not is_pair_mc_ok(p):
                    continue

                accel = compute_accel(p)
                if accel < MIN_ACCEL:
                    continue

                filtered_candidates.append((p, accel, age_min if age_min is not None else 0.0))

            filtered = len(filtered_candidates)

            # 3) Score (numeric scoring so bot can keep running)
            scored = []
            for p, accel, age_min in filtered_candidates[:MAX_NEW_PER_SCAN]:
                symbol = get_pair_symbol(p)
                mint = get_pair_mint(p)
                fdv = safe_float(p.get("fdv", 0.0), 0.0)
                liq = safe_float((p.get("liquidity") or {}).get("usd", 0.0), 0.0)

                score = accel_to_score(accel)
                if score < MIN_SCORE:
                    continue

                scored.append({
                    "symbol": symbol,
                    "mint": mint,
                    "score": score,
                    "accel": accel,
                    "liq": liq,
                    "fdv": fdv,
                    "age_min": age_min,
                    "url": p.get("url", ""),
                    "pair": p,
                })

            scored.sort(key=lambda x: x["score"], reverse=True)
            ranked = len(scored)

            # 4) Monitor paper trades (your Storage handles logic)
            store.monitor_paper_trades()

            # 5) Auto-open paper trades for top candidates
            opened_msgs = []
            for item in scored[:5]:
                msg = store.try_open_paper_trade(item)
                if msg:
                    opened_msgs.append(msg)

            # 6) Live gate
            live_gate_ok = False
            gate_msg = ""
            if LIVE_TRADING_ENABLED:
                live_gate_ok = bool(store.evaluate_live_gate())
                gate_msg = "LIVE gate: ✅ enabled (performance-gated)" if live_gate_ok else "LIVE gate: ❌ disabled (needs better paper performance)"
            else:
                gate_msg = "LIVE gate: ❌ disabled (LIVE_TRADING_ENABLED=false)"

            # 7) Stats from playbook.json
            playbook_stats = _compute_paper_trade_stats(_load_playbook_json())
            pick_tracker_block = _format_pick_tracker_block(playbook_stats)

            # 8) Build Telegram message
            top_lines = []
            for x in scored[:10]:
                top_lines.append(
                    f"{x['symbol']} score={x['score']:.3f} age={int(x['age_min'])}m liq=${x['liq']:.0f} accel={x['accel']:.2f}"
                )

            lines = [
                f"Scan #{scan_id}",
                f"Paper: open={playbook_stats.get('open', 0)} closed={playbook_stats.get('closed', 0)}",
                f"Wallet: {store.get_wallet_balance_sol():.6f} SOL live_env={LIVE_TRADING_ENABLED} mirror={getattr(store, 'mirror_enabled', True)}",
                f"Quality: MIN_ACCEL={MIN_ACCEL} MIN_SCORE={MIN_SCORE} age={AGE_MIN_MINUTES}-{AGE_MAX_MINUTES}m",
                f"Candidates: discovered={discovered} filtered={filtered} ranked={ranked}",
            ]

            if top_lines:
                lines.append("")
                lines.append("Top:")
                lines.extend(top_lines[:3])

            if pick_tracker_block:
                lines.append("")
                lines.append(pick_tracker_block)

            if opened_msgs:
                lines.append("")
                lines.append("Updates:")
                for m in opened_msgs:
                    lines.append(f"- {m}")

            lines.append("")
            lines.append(gate_msg)

            tg.send("\n".join(lines))

            store.save()

            elapsed = time.time() - scan_start
            time.sleep(max(0, SCAN_INTERVAL_SECONDS - elapsed))

        except Exception as e:
            log.exception("Scan loop error: %s", e)
            try:
                tg.send(f"Scanner error: {e}")
            except Exception:
                pass
            time.sleep(15)


if __name__ == "__main__":
    run_forever()