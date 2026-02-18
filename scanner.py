import time
import logging
from typing import Dict, Any, List, Optional

from utils import setup_logging, env_int, env_float, env_str, env_bool, jitter_sleep
from telegram_client import TelegramClient
from datasource import DexScreenerClient
from storage import Storage, make_feature_buckets
from trader import buy_token, get_wallet_summary, is_live_trading_enabled
from discovery import discover_solana_candidates

log = logging.getLogger("memecoinsnipa.scanner")


# ---------------------------
# Timing
# ---------------------------
SCAN_INTERVAL_SECONDS = env_int("SCAN_INTERVAL_SECONDS", 120)


# ---------------------------
# Discovery + Filters (aggressive but sane)
# ---------------------------
DISCOVERY_LIMIT = env_int("DISCOVERY_LIMIT", 80)

MIN_LIQUIDITY_USD = env_float("MIN_LIQUIDITY_USD", 20000.0)
MIN_MC_USD = env_float("MIN_MC_USD", 10000.0)
MAX_MC_USD = env_float("MAX_MC_USD", 3_000_000.0)
MIN_LIQ_TO_MC = env_float("MIN_LIQ_TO_MC", 0.03)

AGE_MIN_MINUTES = env_int("AGE_MIN_MINUTES", 10)     # avoid ultra-new rugs
AGE_MAX_MINUTES = env_int("AGE_MAX_MINUTES", 720)    # 12h max

MAX_CANDIDATES_RANK = env_int("MAX_CANDIDATES_RANK", 40)


# ---------------------------
# Paper trading
# ---------------------------
PAPER_TRADING_ENABLED = env_bool("PAPER_TRADING_ENABLED", True)
PAPER_MAX_OPEN = env_int("PAPER_MAX_OPEN", 6)

# Paper exits
PAPER_STOP_LOSS_PCT = env_float("PAPER_STOP_LOSS_PCT", -20.0)      # -20%
PAPER_TAKE_PROFIT_PCT = env_float("PAPER_TAKE_PROFIT_PCT", 40.0)   # +40%
PAPER_TRAIL_DROP_PCT = env_float("PAPER_TRAIL_DROP_PCT", 15.0)     # exit if drop 15% from peak after being positive
PAPER_MAX_HOLD_MIN = env_int("PAPER_MAX_HOLD_MIN", 180)            # 3 hours max hold


# ---------------------------
# Live mirroring (performance gated)
# ---------------------------
LIVE_MIRROR_ENABLED = env_bool("LIVE_MIRROR_ENABLED", True)  # still requires LIVE_TRADING_ENABLED=true too
LIVE_SOL_PER_TRADE = env_float("MAX_SOL_PER_TRADE", 0.075)
MAX_OPEN_LIVE_TRADES = env_int("MAX_OPEN_LIVE_TRADES", 5)

# Kill-switches (still important even in aggressive mode)
DAILY_LOSS_LIMIT_SOL = env_float("DAILY_LOSS_LIMIT_SOL", 0.15)
MAX_CONSEC_LIVE_LOSSES = env_int("MAX_CONSEC_LIVE_LOSSES", 4)
EMERGENCY_STOP_SOL_BAL = env_float("EMERGENCY_STOP_SOL_BAL", 0.25)


def _num(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def filter_candidate(c: Dict[str, Any]) -> bool:
    age = int(c.get("age_min", 10**9))
    liq = _num(c.get("liq"), 0.0)
    mc = _num(c.get("mc"), 0.0)
    liq_to_mc = _num(c.get("liq_to_mc"), 0.0)

    if age < AGE_MIN_MINUTES or age > AGE_MAX_MINUTES:
        return False
    if liq < MIN_LIQUIDITY_USD:
        return False
    if mc < MIN_MC_USD or mc > MAX_MC_USD:
        return False
    if liq_to_mc < MIN_LIQ_TO_MC:
        return False

    mint = c.get("mint")
    if not mint or len(mint) < 20:
        return False

    return True


def score_candidate(store: Storage, c: Dict[str, Any]) -> float:
    """
    Weighted combo + self-learning bucket edge.
    """
    w = (store.state.get("model") or {}).get("weights") or {}
    w_vol = _num(w.get("vol_accel"), 0.40)
    w_liq = _num(w.get("liq"), 0.25)
    w_age = _num(w.get("age"), 0.20)
    w_bp  = _num(w.get("buy_pressure"), 0.15)

    age = int(c.get("age_min", 10**9))
    liq = _num(c.get("liq"), 0.0)
    accel = _num(c.get("vol_accel"), 0.0)
    buys = int(c.get("buys_1h", 0) or 0)
    sells = int(c.get("sells_1h", 0) or 0)
    bp = buys / max(1, sells)

    # Normalize-ish features into 0..1 bands
    # Aggressive favors accel + decent liq + sweet age + buy pressure
    accel_n = min(1.0, max(0.0, (accel - 0.6) / 3.0))  # accel ~0.6..3.6
    liq_n = min(1.0, max(0.0, (liq - 20000.0) / 180000.0))  # 20k..200k
    # Age sweet spot: 15..360 is best
    if age < 15:
        age_n = 0.2
    elif age <= 360:
        age_n = 1.0
    elif age <= 720:
        age_n = 0.6
    else:
        age_n = 0.2
    bp_n = min(1.0, max(0.0, (bp - 0.8) / 2.2))  # bp ~0.8..3.0

    base = (w_vol * accel_n) + (w_liq * liq_n) + (w_age * age_n) + (w_bp * bp_n)

    # Self-learning bucket edge adjustment
    buckets = make_feature_buckets(c)
    edge = 0.0
    for b in buckets:
        edge += store.bucket_edge(b)
    edge = max(-1.5, min(1.5, edge))  # clamp

    return base + 0.20 * edge


def should_open_new_paper(store: Storage) -> bool:
    if not PAPER_TRADING_ENABLED:
        return False
    open_trades = store.get_open_paper_trades()
    return len(open_trades) < PAPER_MAX_OPEN


def paper_exit_reason(trade: Dict[str, Any], now_price: float) -> Optional[str]:
    entry = _num(trade.get("entry_price"), 0.0)
    if entry <= 0:
        return "bad_entry"

    pnl = (now_price / entry - 1.0) * 100.0
    peak = _num(trade.get("peak_pnl_pct"), 0.0)

    age_min = int((time.time() - int(trade.get("entry_ts", int(time.time())))) / 60)

    if pnl <= PAPER_STOP_LOSS_PCT:
        return "stop_loss"
    if pnl >= PAPER_TAKE_PROFIT_PCT:
        return "take_profit"
    # trailing: if had a meaningful peak, and drops from peak by trail amount
    if peak >= 8.0 and (peak - pnl) >= PAPER_TRAIL_DROP_PCT:
        return "trail_exit"
    if age_min >= PAPER_MAX_HOLD_MIN:
        return "time_exit"

    return None


def run_forever():
    setup_logging()

    tg = TelegramClient(env_str("TELEGRAM_BOT_TOKEN", ""), env_str("TELEGRAM_USER_ID", ""))
    dex = DexScreenerClient()
    store = Storage()

    tg.send("Bot restarted and running.")

    # For live loss tracking (simple daily reset)
    day_key = time.strftime("%Y-%m-%d")
    store.state.setdefault("live_risk", {"day": day_key, "loss_sol": 0.0, "consec_losses": 0, "open_live": 0})

    while True:
        try:
            store.increment_scan()
            scan_id = int(store.state.get("scans", 0))

            # Reset daily live counters if day changes
            today = time.strftime("%Y-%m-%d")
            lr = store.state.setdefault("live_risk", {"day": today, "loss_sol": 0.0, "consec_losses": 0, "open_live": 0})
            if lr.get("day") != today:
                lr["day"] = today
                lr["loss_sol"] = 0.0
                lr["consec_losses"] = 0

            wallet = get_wallet_summary()
            live_enabled_env = is_live_trading_enabled()
            live_gate = store.evaluate_live_gate()
            live_eligible = bool(live_gate.get("eligible"))

            # Emergency stop if SOL too low
            bal = wallet.get("balance_sol")
            if bal is not None and _num(bal, 0.0) < EMERGENCY_STOP_SOL_BAL:
                live_enabled_env = False

            # 1) Monitor open paper trades
            open_papers = store.get_open_paper_trades()
            closed_msgs: List[str] = []

            for t in open_papers:
                mint = t.get("mint")
                if not mint:
                    continue

                tok = dex.get_token(mint)
                price = _num(tok.get("price"), 0.0)
                if price <= 0:
                    continue

                store.update_paper_trade_mark(t["id"], price)
                reason = paper_exit_reason(t, price)
                if reason:
                    win, pnl = store.close_paper_trade(t["id"], price, reason)
                    closed_msgs.append(f"Paper closed {t['id']} pnl={pnl:.2f}% reason={reason} win={win}")

            # 2) Discover candidates
            discovered = discover_solana_candidates(limit=DISCOVERY_LIMIT)

            candidates: List[Dict[str, Any]] = []
            for c in discovered:
                mint = c.get("mint")
                if not mint:
                    continue
                if store.is_blacklisted(mint):
                    continue
                if filter_candidate(c):
                    candidates.append(c)

            # 3) Score + rank with learning
            for c in candidates:
                c["score"] = score_candidate(store, c)
                c["buckets"] = make_feature_buckets(c)

            candidates.sort(key=lambda x: _num(x.get("score"), 0.0), reverse=True)
            ranked = candidates[:MAX_CANDIDATES_RANK]

            # 4) Open new paper trades from top candidates
            opened_msgs: List[str] = []
            if should_open_new_paper(store) and ranked:
                # Open up to 1 per scan (aggressive but avoids spam)
                top = ranked[0]
                mint = top["mint"]

                # Avoid duplicate open trade on same mint
                already_open = any(t.get("mint") == mint for t in store.get_open_paper_trades())
                if not already_open:
                    tok = dex.get_token(mint)
                    price = _num(tok.get("price"), 0.0)
                    if price > 0:
                        meta = {
                            "symbol": top.get("symbol"),
                            "score": top.get("score"),
                            "age_min": top.get("age_min"),
                            "liq": top.get("liq"),
                            "mc": top.get("mc"),
                            "liq_to_mc": top.get("liq_to_mc"),
                            "vol_accel": top.get("vol_accel"),
                            "buys_1h": top.get("buys_1h"),
                            "sells_1h": top.get("sells_1h"),
                            "url": top.get("url"),
                            "buckets": top.get("buckets"),
                        }
                        tid = store.open_paper_trade(mint, price, meta)
                        opened_msgs.append(f"Paper opened {tid} mint={mint[:6]} score={_num(top.get('score'),0):.3f} price={price}")

                        # 5) Live mirroring (only if eligible + enabled)
                        if LIVE_MIRROR_ENABLED and live_enabled_env and live_eligible:
                            # Risk checks
                            if _num(lr.get("loss_sol"), 0.0) >= DAILY_LOSS_LIMIT_SOL:
                                opened_msgs.append("Live blocked: daily loss limit reached")
                            elif int(lr.get("consec_losses", 0)) >= MAX_CONSEC_LIVE_LOSSES:
                                opened_msgs.append("Live blocked: consecutive loss limit reached")
                            elif int(lr.get("open_live", 0)) >= MAX_OPEN_LIVE_TRADES:
                                opened_msgs.append("Live blocked: max open live trades reached")
                            else:
                                res = buy_token(mint, LIVE_SOL_PER_TRADE)
                                store.state.setdefault("live_trades", []).append({
                                    "ts": int(time.time()),
                                    "mint": mint,
                                    "type": "BUY",
                                    "sol": LIVE_SOL_PER_TRADE,
                                    "result": res,
                                    "source_paper_trade": tid,
                                })
                                opened_msgs.append(f"Live buy attempted mint={mint[:6]} success={res.get('success')}")

            # 6) Save + notify
            store.save()

            lines = [
                f"Scan #{scan_id}",
                f"Paper: open={len(store.get_open_paper_trades())} closed={(store.state.get('stats') or {}).get('paper', {}).get('closed', 0)}",
                f"Gate: eligible={live_eligible} reason={live_gate.get('reason')}",
                f"Wallet: {wallet.get('balance_sol')} SOL live_env={is_live_trading_enabled()} mirror={LIVE_MIRROR_ENABLED}",
                f"Candidates: discovered={len(discovered)} filtered={len(candidates)} ranked={len(ranked)}",
            ]

            if opened_msgs:
                lines.append("Actions:")
                lines.extend(opened_msgs)

            if closed_msgs:
                lines.append("Closures:")
                # Keep message size safe
                lines.extend(closed_msgs[:6])

            # Include top 3 snapshot (no spam)
            if ranked:
                top3 = ranked[:3]
                lines.append("Top:")
                for c in top3:
                    lines.append(
                        f"{c.get('symbol','UNK')} {c.get('mint','')[:6]} "
                        f"score={_num(c.get('score'),0):.3f} age={c.get('age_min')}m "
                        f"liq=${_num(c.get('liq'),0):.0f} accel={_num(c.get('vol_accel'),0):.2f}"
                    )

            tg.send("\n".join(lines))

        except Exception as e:
            log.exception(f"Scan loop error: {e}")
            try:
                tg.send(f"Scan error: {str(e)[:180]}")
            except Exception:
                pass

        jitter_sleep(SCAN_INTERVAL_SECONDS, 0.1)