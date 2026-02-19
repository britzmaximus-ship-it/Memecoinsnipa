#!/usr/bin/env python3
"""
scanner.py

Main loop:
- Discover new pairs via DexScreener
- Filter / score via rules + LLM
- Open paper trades + monitor paper trades
- Optional: performance-gated mirroring to LIVE trades
- Send Telegram updates each scan

This version includes:
- Full traceback reporting to Telegram on errors
- Extra safety wrappers so one bad token/trade can't kill the scan
"""

import time
import random
import logging
import os
import json
import math
import traceback
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

PLAYBOOK_PATH = os.getenv("PLAYBOOK_PATH", "playbook.json")


# ============================================================
# PLAYBOOK STATS
# ============================================================

def _load_playbook_json(path: str = PLAYBOOK_PATH) -> dict:
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _compute_paper_trade_stats(playbook: dict) -> dict:
    open_trades = playbook.get("paper_trades") or playbook.get("paperTrades") or []
    history = playbook.get("paper_trade_history") or playbook.get("paperTradeHistory") or []

    if not history and isinstance(playbook.get("trades"), list):
        maybe = [t for t in playbook.get("trades", []) if str(t.get("mode", "")).upper() == "PAPER"]
        history = maybe or playbook.get("trades", [])

    closed = [t for t in history if isinstance(t, dict)]
    wins = 0
    returns = []

    for t in closed:
        try:
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
        except Exception:
            # ignore one bad trade record
            continue

    closed_n = len(closed)
    win_rate = (wins / closed_n * 100.0) if closed_n else 0.0
    avg_return = (sum(returns) / len(returns)) if returns else 0.0

    return {
        "open": len(open_trades) if isinstance(open_trades, list) else 0,
        "pending": int(playbook.get("pending_paper", playbook.get("pendingPaper", 0)) or 0),
        "closed": closed_n,
        "wins": wins,
        "win_rate": win_rate,
        "avg_return": avg_return,
    }


def _format_pick_tracker_block(stats: dict) -> str:
    open_ = int((stats or {}).get("open", 0) or 0)
    pending = int((stats or {}).get("pending", 0) or 0)
    closed = int((stats or {}).get("closed", 0) or 0)
    wins = int((stats or {}).get("wins", 0) or 0)
    win_rate = float((stats or {}).get("win_rate", 0.0) or 0.0)
    avg_return = float((stats or {}).get("avg_return", 0.0) or 0.0)

    return "\n".join(
        [
            "📋 PICK TRACKER UPDATE",
            "===================================",
            f"Active: {open_} | Pending: {pending} | Closed: {closed}",
            f"Wins: {wins} | Win rate: {win_rate:.1f}% | Avg return: {avg_return:+.1f}%",
            "===================================",
        ]
    )


# ============================================================
# HELPERS
# ============================================================

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def minutes_since(ts_ms: int) -> float:
    return (time.time() * 1000 - ts_ms) / 60000.0


def compute_accel(pair: dict) -> float:
    pc = safe_float(pair.get("priceChange", {}).get("h1", 0.0), 0.0)
    vol = safe_float(pair.get("volume", {}).get("h1", 0.0), 0.0)
    liq = safe_float((pair.get("liquidity") or {}).get("usd", 0.0), 0.0)
    if liq <= 0:
        return 0.0
    return (abs(pc) / 100.0) + (vol / max(liq, 1.0))


def fetch_pairs(query: str):
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
    return str(((pair.get("baseToken") or {}).get("symbol")) or "UNK").strip()


def get_pair_mint(pair: dict) -> str:
    return str(((pair.get("baseToken") or {}).get("address")) or "").strip()


# ============================================================
# MAIN
# ============================================================

def main():
    tg = TelegramClient()
    store = Storage()
    llm = LLMScorer()
    _trader = JupiterTrader()  # keep for compatibility

    # Startup ping so you know it booted
    try:
        tg.send("✅ scanner.py booted (with traceback reporting)")
    except Exception:
        pass

    while True:
        scan_start = time.time()
        try:
            store.state["scans"] = int(store.state.get("scans", 0)) + 1
            scan_id = store.state["scans"]

            pairs = fetch_pairs("pump")
            random.shuffle(pairs)
            pairs = pairs[: MAX_NEW_PER_SCAN * 3]

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

            scored = []
            for p, accel in candidates[:MAX_NEW_PER_SCAN]:
                try:
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
                except Exception:
                    # one bad token shouldn't kill the scan
                    continue

            scored.sort(key=lambda x: x["score"], reverse=True)

            # These are common crash points, so isolate them:
            try:
                store.monitor_paper_trades()
            except Exception:
                log.exception("monitor_paper_trades failed")

            opened_msgs = []
            for item in scored[:5]:
                try:
                    msg = store.try_open_paper_trade(item)
                    if msg:
                        opened_msgs.append(msg)
                except Exception:
                    log.exception("try_open_paper_trade failed")

            live_msgs = []
            try:
                if LIVE_TRADING_ENABLED and store.evaluate_live_gate():
                    live_msgs.append("LIVE gate: ✅ enabled (performance-gated)")
                else:
                    live_msgs.append("LIVE gate: ❌ disabled (needs better paper performance)")
            except Exception:
                log.exception("evaluate_live_gate failed")
                live_msgs.append("LIVE gate: ⚠️ error (see logs)")

            top_lines = []
            for i, x in enumerate(scored[:10], 1):
                top_lines.append(
                    f"{i:02d}. {x['symbol']} | score={x['score']:.3f} | accel={x['accel']:.3f} | liq=${x['liq']:.0f} | fdv=${x['fdv']:.0f}"
                )

            playbook_stats = _compute_paper_trade_stats(_load_playbook_json())
            pick_tracker_block = _format_pick_tracker_block(playbook_stats)

            lines = [
                f"Scan #{scan_id}",
                f"Quality: MIN_ACCEL={MIN_ACCEL} MIN_SCORE={MIN_SCORE} age={AGE_MIN_MINUTES}-{AGE_MAX_MINUTES}m",
                f"Candidates: discovered={len(pairs)} filtered={len(candidates)} ranked={len(scored)}",
                f"Paper: open={playbook_stats.get('open', 0)} closed={playbook_stats.get('closed', 0)}",
                "",
                "Top picks:",
                *top_lines if top_lines else ["(none)"],
                "",
                pick_tracker_block,
            ]

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
            except Exception:
                log.exception("store.save failed")

        except Exception:
            tb = traceback.format_exc()
            log.error("Scan loop error:\n%s", tb)

            # Telegram tracebacks can be long; send a trimmed version.
            try:
                trimmed = tb[-3500:] if len(tb) > 3500 else tb
                tg.send("❌ Scanner crashed with traceback:\n\n" + trimmed)
            except Exception:
                pass

            time.sleep(15)

        # normal sleep
        elapsed = time.time() - scan_start
        sleep_for = max(0, SCAN_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()