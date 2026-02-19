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

import requests

from telegram_client import TelegramClient
from storage import Storage
from llm import LLMScorer
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

# ============================================================
# PLAYBOOK STATS (for Telegram scan messages)
# ============================================================

PLAYBOOK_PATH = os.getenv("PLAYBOOK_PATH", "playbook.json")


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

    # Some implementations store closed trades inside a generic list; keep this future-proof.
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
            # Fallback: compute from prices if present
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
    # ts_ms in milliseconds
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
    return (abs(pc) / 100.0) + (vol / max(liq, 1.0))  # heuristic


def fetch_pairs(query: str):
    url = f"{DEXSCREENER_SEARCH_URL}?q={query}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    pairs = data.get("pairs", []) or []
    # Filter to Solana
    return [p for p in pairs if p.get("chainId") == SOLANA_CHAIN_ID]


def is_pair_age_ok(pair: dict) -> bool:
    created = pair.get("pairCreatedAt")
    if not created:
        return False
    age_min = minutes_since(int(created))
    return AGE_MIN_MINUTES <= age_min <= AGE_MAX_MINUTES


def is_pair_liquidity_ok(pair: dict) -> bool:
    liq = safe_float((pair.get("liquidity") or {}).get("usd", 0.0), 0.0)
    return liq >= MIN_LIQUIDITY_USD


def is_pair_mc_ok(pair: dict) -> bool:
    fdv = safe_float(pair.get("fdv", 0.0), 0.0)
    if fdv <= 0:
        return True  # some pairs don't provide
    return fdv <= MAX_MC_USD


def get_pair_symbol(pair: dict) -> str:
    base = (pair.get("baseToken") or {}).get("symbol") or "UNK"
    return str(base).strip()


def get_pair_mint(pair: dict) -> str:
    base = (pair.get("baseToken") or {}).get("address") or ""
    return str(base).strip()


# ============================================================
# MAIN
# ============================================================


def main():
    tg = TelegramClient()
    store = Storage()
    llm = LLMScorer()
    trader = JupiterTrader()

    while True:
        try:
            scan_start = time.time()
            store.state["scans"] = int(store.state.get("scans", 0)) + 1
            scan_id = store.state["scans"]

            # Pull candidates
            pairs = fetch_pairs("pump")  # broad query
            random.shuffle(pairs)
            pairs = pairs[: MAX_NEW_PER_SCAN * 3]  # extra for filtering

            candidates = []
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

            # Score candidates via LLM
            scored = []
            for p, accel in candidates[:MAX_NEW_PER_SCAN]:
                symbol = get_pair_symbol(p)
                mint = get_pair_mint(p)
                fdv = safe_float(p.get("fdv", 0.0), 0.0)
                liq = safe_float((p.get("liquidity") or {}).get("usd", 0.0), 0.0)

                score = llm.score_token(
                    symbol=symbol,
                    mint=mint,
                    liquidity_usd=liq,
                    fdv_usd=fdv,
                    accel=accel,
                    pair=p,
                )

                if score < MIN_SCORE:
                    continue

                scored.append(
                    {
                        "symbol": symbol,
                        "mint": mint,
                        "score": score,
                        "accel": accel,
                        "liq": liq,
                        "fdv": fdv,
                        "url": p.get("url", ""),
                        "pair": p,
                    }
                )

            scored.sort(key=lambda x: x["score"], reverse=True)

            # Monitor paper trades
            store.monitor_paper_trades()

            # Auto-open paper trades for top scored candidates
            opened_msgs = []
            for item in scored[:5]:
                msg = store.try_open_paper_trade(item)
                if msg:
                    opened_msgs.append(msg)

            # Optional live gate + mirroring
            live_msgs = []
            if LIVE_TRADING_ENABLED and store.evaluate_live_gate():
                live_msgs.append("LIVE gate: ✅ enabled (performance-gated)")
                # If your implementation mirrors automatically, keep it there.
                # Otherwise, you'd trigger trader actions here.
            else:
                live_msgs.append("LIVE gate: ❌ disabled (needs better paper performance)")

            # Build Telegram message
            top_lines = []
            for i, x in enumerate(scored[:10], 1):
                top_lines.append(
                    f"{i:02d}. {x['symbol']} | score={x['score']:.3f} | accel={x['accel']:.3f} | liq=${x['liq']:.0f} | fdv=${x['fdv']:.0f}"
                )

            playbook_stats = _compute_paper_trade_stats(_load_playbook_json())
            pick_tracker_block = _format_pick_tracker_block(playbook_stats)

            lines = [
                f"Scan #{scan_id}",
                f"Candidates: {len(candidates)} | Scored: {len(scored)} | Filters: age {AGE_MIN_MINUTES}-{AGE_MAX_MINUTES}m, liq>={MIN_LIQUIDITY_USD}, mc<={MAX_MC_USD}, accel>={MIN_ACCEL}, score>={MIN_SCORE}",
                f"Paper: open={playbook_stats.get('open', len(store.get_open_paper_trades()))} closed={playbook_stats.get('closed', (store.state.get('stats') or {}).get('paper', {}).get('closed', 0))}",
                "",
                "Top picks:",
                *top_lines if top_lines else ["(none)"],
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

            msg = "\n".join(lines)
            tg.send(msg)

            store.save()

            elapsed = time.time() - scan_start
            sleep_for = max(0, SCAN_INTERVAL_SECONDS - elapsed)
            time.sleep(sleep_for)

        except Exception as e:
            log.exception("Scan loop error: %s", e)
            try:
                tg.send(f"Scanner error: {e}")
            except Exception:
                pass
            time.sleep(15)


if __name__ == "__main__":
    main()