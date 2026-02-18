import time
import logging
from typing import Dict, Any, List

from utils import setup_logging, env_int, env_float, env_str, jitter_sleep
from telegram_client import TelegramClient
from datasource import DexScreenerClient
from storage import Storage
from llm import LLM
from trader import buy_token, get_wallet_summary, is_live_trading_enabled

log = logging.getLogger("memecoinsnipa.scanner")

SCAN_INTERVAL_SECONDS = env_int("SCAN_INTERVAL_SECONDS", 120)

MIN_LIQUIDITY_USD = env_float("MIN_LIQUIDITY_USD", 8000.0)
MIN_MC_USD = env_float("MIN_MC_USD", 10_000.0)
MAX_MC_USD = env_float("MAX_MC_USD", 2_000_000.0)
MIN_LIQ_TO_MC = env_float("MIN_LIQ_TO_MC", 0.04)

OPEN_PICKS_LIMIT = env_int("OPEN_PICKS_LIMIT", 6)
MAX_SOL_PER_TRADE = env_float("MAX_SOL_PER_TRADE", 0.075)
TOKENS_TO_CHECK = env_int("TOKENS_TO_CHECK", 20)


def heuristic_score(d: Dict[str, Any]) -> float:
    score = 0.0
    liq = float(d.get("liquidity", 0) or 0)
    mc = float(d.get("market_cap", 0) or 0)
    vol = float(d.get("volume_24h", 0) or 0)
    chg = float(d.get("price_change_24h", 0) or 0)

    liq_to_mc = (liq / mc) if mc > 0 else 0.0

    if liq >= 50_000:
        score += 2
    elif liq >= 20_000:
        score += 1
    elif liq >= 10_000:
        score += 0.5
    else:
        score -= 1

    if liq_to_mc >= 0.12:
        score += 2
    elif liq_to_mc >= 0.06:
        score += 1
    elif liq_to_mc >= MIN_LIQ_TO_MC:
        score += 0.5
    else:
        score -= 1

    if vol >= 200_000:
        score += 1
    elif vol >= 50_000:
        score += 0.5

    if 5 < chg < 120:
        score += 0.5
    elif chg >= 200:
        score -= 1

    return score


def filter_token(symbol: str, data: Dict[str, Any]) -> bool:
    mc = float(data.get("market_cap", 0) or 0)
    liq = float(data.get("liquidity", 0) or 0)

    if liq < MIN_LIQUIDITY_USD:
        return False
    if mc < MIN_MC_USD or mc > MAX_MC_USD:
        return False
    if mc > 0 and (liq / mc) < MIN_LIQ_TO_MC:
        return False

    return True


def run_forever():
    setup_logging()

    tg = TelegramClient(env_str("TELEGRAM_BOT_TOKEN", ""), env_str("TELEGRAM_USER_ID", ""))
    store = Storage()
    dex = DexScreenerClient()
    llm = LLM()

    tg.send("Bot restarted and running.")

    while True:
        try:
            store.increment_scan()
            scan_id = store.state["scans"]

            wallet = get_wallet_summary()
            live = is_live_trading_enabled()

            token_list_raw = env_str("TOKENS_TO_SCAN", "")
            if not token_list_raw:
                tg.send("TOKENS_TO_SCAN environment variable is empty.")
                jitter_sleep(SCAN_INTERVAL_SECONDS, 0.1)
                continue

            token_addresses = [x.strip() for x in token_list_raw.split(",") if x.strip()]
            token_addresses = token_addresses[:TOKENS_TO_CHECK]

            results = dex.bulk_fetch(token_addresses)

            candidates: List[Dict[str, Any]] = []
            for token, data in results.items():
                if not data:
                    continue
                if store.is_blacklisted(token):
                    continue

                symbol = token[:6]
                if filter_token(symbol, data):
                    candidates.append({"token": token, "symbol": symbol, **data})

            candidates.sort(key=heuristic_score, reverse=True)

            payload = {"scan": scan_id, "candidates": candidates[:10]}
            analysis = llm.analyze(payload)

            msg_lines = [
                f"Scan #{scan_id}",
                f"Wallet: {wallet.get('balance_sol')} SOL | LIVE: {live}",
                f"Candidates: {len(candidates)}",
            ]

            if analysis:
                msg_lines.append("AI Analysis:")
                msg_lines.append(analysis[:1200])
            else:
                msg_lines.append("AI skipped (cooldown or unavailable). Using heuristic ranking.")

            open_picks = [p for p in store.state.get("picks", []) if p.get("status") == "OPEN"]

            if len(open_picks) < OPEN_PICKS_LIMIT and candidates:
                best = candidates[0]
                pick = {
                    "status": "OPEN",
                    "token": best["token"],
                    "score": heuristic_score(best),
                    "market_cap": best.get("market_cap"),
                    "liquidity": best.get("liquidity"),
                    "url": best.get("url"),
                }

                store.add_pick(pick)

                if live:
                    res = buy_token(best["token"], MAX_SOL_PER_TRADE)
                    store.add_trade({"type": "BUY", "token": best["token"], "result": res})
                    msg_lines.append(
                        f"LIVE BUY attempted: {best['token'][:6]} success={res.get('success')}"
                    )
                else:
                    msg_lines.append(
                        f"Paper pick opened: {best['token'][:6]} score={pick['score']:.2f}"
                    )

            store.save()
            tg.send("\n".join(msg_lines))

        except Exception as e:
            log.exception(f"Scan error: {e}")
            tg.send(f"Scan error: {str(e)[:180]}")

        jitter_sleep(SCAN_INTERVAL_SECONDS, 0.1)
