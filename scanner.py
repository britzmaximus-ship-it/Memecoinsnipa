import time
import logging
from typing import Dict, Any, List, Optional

from utils import setup_logging, env_int, env_float, env_str, env_bool, jitter_sleep
from telegram_client import TelegramClient
from datasource import DexScreenerClient
from storage import Storage, make_feature_buckets
from discovery import discover_solana_candidates
from trader import buy_token, sell_token, get_wallet_summary, is_live_trading_enabled

log = logging.getLogger("memecoinsnipa.scanner")

SCAN_INTERVAL_SECONDS = env_int("SCAN_INTERVAL_SECONDS", 120)

DISCOVERY_LIMIT = env_int("DISCOVERY_LIMIT", 80)

MIN_LIQUIDITY_USD = env_float("MIN_LIQUIDITY_USD", 20000.0)
MIN_MC_USD = env_float("MIN_MC_USD", 10000.0)
MAX_MC_USD = env_float("MAX_MC_USD", 3_000_000.0)
MIN_LIQ_TO_MC = env_float("MIN_LIQ_TO_MC", 0.03)

AGE_MIN_MINUTES = env_int("AGE_MIN_MINUTES", 10)
AGE_MAX_MINUTES = env_int("AGE_MAX_MINUTES", 720)

MAX_CANDIDATES_RANK = env_int("MAX_CANDIDATES_RANK", 40)

PAPER_TRADING_ENABLED = env_bool("PAPER_TRADING_ENABLED", True)
PAPER_MAX_OPEN = env_int("PAPER_MAX_OPEN", 6)

PAPER_STOP_LOSS_PCT = env_float("PAPER_STOP_LOSS_PCT", -20.0)
PAPER_TAKE_PROFIT_PCT = env_float("PAPER_TAKE_PROFIT_PCT", 40.0)
PAPER_TRAIL_DROP_PCT = env_float("PAPER_TRAIL_DROP_PCT", 15.0)
PAPER_MAX_HOLD_MIN = env_int("PAPER_MAX_HOLD_MIN", 180)

LIVE_MIRROR_ENABLED = env_bool("LIVE_MIRROR_ENABLED", True)
LIVE_SOL_PER_TRADE = env_float("MAX_SOL_PER_TRADE", 0.075)
MAX_OPEN_LIVE_TRADES = env_int("MAX_OPEN_LIVE_TRADES", 5)

# Live exits (you can tune)
LIVE_STOP_LOSS_PCT = env_float("LIVE_STOP_LOSS_PCT", -25.0)
LIVE_TAKE_PROFIT_PCT = env_float("LIVE_TAKE_PROFIT_PCT", 60.0)
LIVE_TRAIL_DROP_PCT = env_float("LIVE_TRAIL_DROP_PCT", 18.0)
LIVE_MAX_HOLD_MIN = env_int("LIVE_MAX_HOLD_MIN", 240)

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
    mint = c.get("mint")

    if not mint or len(mint) < 20:
        return False
    if age < AGE_MIN_MINUTES or age > AGE_MAX_MINUTES:
        return False
    if liq < MIN_LIQUIDITY_USD:
        return False
    if mc < MIN_MC_USD or mc > MAX_MC_USD:
        return False
    if liq_to_mc < MIN_LIQ_TO_MC:
        return False
    return True


def score_candidate(store: Storage, c: Dict[str, Any]) -> float:
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

    accel_n = min(1.0, max(0.0, (accel - 0.6) / 3.0))
    liq_n = min(1.0, max(0.0, (liq - 20000.0) / 180000.0))

    if age < 15:
        age_n = 0.2
    elif age <= 360:
        age_n = 1.0
    elif age <= 720:
        age_n = 0.6
    else:
        age_n = 0.2

    bp_n = min(1.0, max(0.0, (bp - 0.8) / 2.2))

    base = (w_vol * accel_n) + (w_liq * liq_n) + (w_age * age_n) + (w_bp * bp_n)

    buckets = make_feature_buckets(c)
    edge = 0.0
    for b in buckets:
        edge += store.bucket_edge(b)
    edge = max(-1.5, min(1.5, edge))

    return base + 0.20 * edge


def should_open_new_paper(store: Storage) -> bool:
    if not PAPER_TRADING_ENABLED:
        return False
    return len(store.get_open_paper_trades()) < PAPER_MAX_OPEN


def _pnl_pct(entry: float, now: float) -> float:
    return (now / max(1e-12, entry) - 1.0) * 100.0


def paper_exit_reason(trade: Dict[str, Any], now_price: float) -> Optional[str]:
    entry = _num(trade.get("entry_price"), 0.0)
    pnl = _pnl_pct(entry, now_price)
    peak = _num(trade.get("peak_pnl_pct"), 0.0)
    age_min = int((time.time() - int(trade.get("entry_ts", int(time.time())))) / 60)

    if pnl <= PAPER_STOP_LOSS_PCT:
        return "stop_loss"
    if pnl >= PAPER_TAKE_PROFIT_PCT:
        return "take_profit"
    if peak >= 8.0 and (peak - pnl) >= PAPER_TRAIL_DROP_PCT:
        return "trail_exit"
    if age_min >= PAPER_MAX_HOLD_MIN:
        return "time_exit"
    return None


def live_exit_reason(pos: Dict[str, Any], now_price: float) -> Optional[str]:
    entry = _num(pos.get("entry_price"), 0.0)
    pnl = _pnl_pct(entry, now_price)
    peak = _num(pos.get("peak_pnl_pct"), 0.0)
    age_min = int((time.time() - int(pos.get("entry_ts", int(time.time())))) / 60)

    if pnl <= LIVE_STOP_LOSS_PCT:
        return "stop_loss"
    if pnl >= LIVE_TAKE_PROFIT_PCT:
        return "take_profit"
    if peak >= 10.0 and (peak - pnl) >= LIVE_TRAIL_DROP_PCT:
        return "trail_exit"
    if age_min >= LIVE_MAX_HOLD_MIN:
        return "time_exit"
    return None


def run_forever():
    setup_logging()

    tg = TelegramClient(env_str("TELEGRAM_BOT_TOKEN", ""), env_str("TELEGRAM_USER_ID", ""))
    dex = DexScreenerClient()
    store = Storage()

    tg.send("Bot restarted and running.")

    store.state.setdefault("open_live_positions", [])
    store.state.setdefault("live_risk", {"day": time.strftime("%Y-%m-%d"), "loss_sol": 0.0, "consec_losses": 0})

    while True:
        try:
            store.increment_scan()
            scan_id = int(store.state.get("scans", 0))

            today = time.strftime("%Y-%m-%d")
            lr = store.state.setdefault("live_risk", {"day": today, "loss_sol": 0.0, "consec_losses": 0})
            if lr.get("day") != today:
                lr["day"] = today
                lr["loss_sol"] = 0.0
                lr["consec_losses"] = 0

            wallet = get_wallet_summary()
            live_env = is_live_trading_enabled()
            gate = store.evaluate_live_gate()
            eligible = bool(gate.get("eligible"))

            # Emergency stop
            bal = wallet.get("balance_sol")
            if bal is not None and _num(bal, 0.0) < EMERGENCY_STOP_SOL_BAL:
                live_env = False

            closed_msgs: List[str] = []
            opened_msgs: List[str] = []

            # -----------------------
            # A) Monitor open LIVE positions -> auto-sell
            # -----------------------
            open_live = store.state.get("open_live_positions") or []
            still_open: List[Dict[str, Any]] = []

            for pos in open_live:
                mint = pos.get("mint")
                if not mint:
                    continue

                tok = dex.get_token(mint)
                price = _num(tok.get("price"), 0.0)
                if price <= 0:
                    still_open.append(pos)
                    continue

                pnl = _pnl_pct(_num(pos.get("entry_price"), 0.0), price)
                pos["last_price"] = price
                pos["pnl_pct"] = pnl
                pos["peak_pnl_pct"] = max(_num(pos.get("peak_pnl_pct"), 0.0), pnl)

                reason = live_exit_reason(pos, price)
                if reason and live_env:
                    res = sell_token(mint)
                    store.state.setdefault("live_trades", []).append({
                        "ts": int(time.time()),
                        "mint": mint,
                        "type": "SELL",
                        "reason": reason,
                        "result": res,
                        "pnl_est_pct": pnl,
                    })
                    closed_msgs.append(f"Live sell attempted {mint[:6]} reason={reason} success={res.get('success')} pnl_est={pnl:.2f}%")

                    # very rough risk tracking: count a loss if pnl_est < 0
                    if pnl < 0:
                        lr["consec_losses"] = int(lr.get("consec_losses", 0)) + 1
                        lr["loss_sol"] = float(lr.get("loss_sol", 0.0)) + float(LIVE_SOL_PER_TRADE)
                    else:
                        lr["consec_losses"] = 0

                    # Do not keep open
                    continue

                still_open.append(pos)

            store.state["open_live_positions"] = still_open

            # -----------------------
            # B) Monitor open PAPER trades -> close & learn
            # -----------------------
            open_papers = store.get_open_paper_trades()
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

            # -----------------------
            # C) Discover + rank
            # -----------------------
            discovered = discover_solana_candidates(limit=DISCOVERY_LIMIT)

            candidates: List[Dict[str, Any]] = []
            for c in discovered:
                mint = c.get("mint")
                if not mint:
                    continue
                if store.is_blacklisted(mint):
                    continue
                if filter_candidate(c):
                    c["score"] = score_candidate(store, c)
                    c["buckets"] = make_feature_buckets(c)
                    candidates.append(c)

            candidates.sort(key=lambda x: _num(x.get("score"), 0.0), reverse=True)
            ranked = candidates[:MAX_CANDIDATES_RANK]

            # -----------------------
            # D) Open paper trade + maybe mirror live buy
            # -----------------------
            if ranked and should_open_new_paper(store):
                top = ranked[0]
                mint = top["mint"]

                already_open_paper = any(t.get("mint") == mint for t in store.get_open_paper_trades())
                already_open_live = any(p.get("mint") == mint for p in (store.state.get("open_live_positions") or []))

                if not already_open_paper:
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
                        opened_msgs.append(f"Paper opened {tid} {mint[:6]} score={_num(top.get('score'),0):.3f} price={price}")

                        # Live mirror buy
                        if LIVE_MIRROR_ENABLED and live_env and eligible and not already_open_live:
                            if float(lr.get("loss_sol", 0.0)) >= DAILY_LOSS_LIMIT_SOL:
                                opened_msgs.append("Live blocked: daily loss limit reached")
                            elif int(lr.get("consec_losses", 0)) >= MAX_CONSEC_LIVE_LOSSES:
                                opened_msgs.append("Live blocked: consecutive loss limit reached")
                            elif len(store.state.get("open_live_positions") or []) >= MAX_OPEN_LIVE_TRADES:
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
                                opened_msgs.append(f"Live buy attempted {mint[:6]} success={res.get('success')}")

                                # Track open position if buy succeeded
                                if res.get("success"):
                                    store.state.setdefault("open_live_positions", []).append({
                                        "mint": mint,
                                        "entry_price": price,  # Dex price proxy
                                        "entry_ts": int(time.time()),
                                        "last_price": price,
                                        "pnl_pct": 0.0,
                                        "peak_pnl_pct": 0.0,
                                        "source_paper_trade": tid,
                                    })

            store.save()

            # -----------------------
            # E) Telegram status
            # -----------------------
            lines = [
                f"Scan #{scan_id}",
                f"Paper: open={len(store.get_open_paper_trades())} closed={(store.state.get('stats') or {}).get('paper', {}).get('closed', 0)}",
                f"Live: open={len(store.state.get('open_live_positions') or [])} eligible={eligible} env={is_live_trading_enabled()} mirror={LIVE_MIRROR_ENABLED}",
                f"Gate: {gate.get('reason')}",
                f"Wallet: {wallet.get('balance_sol')} SOL",
                f"Candidates: discovered={len(discovered)} filtered={len(candidates)} ranked={len(ranked)}",
            ]

            if opened_msgs:
                lines.append("Actions:")
                lines.extend(opened_msgs)

            if closed_msgs:
                lines.append("Updates:")
                lines.extend(closed_msgs[:8])

            if ranked:
                lines.append("Top:")
                for c in ranked[:3]:
                    lines.append(
                        f"{c.get('symbol','UNK')} {c.get('mint','')[:6]} score={_num(c.get('score'),0):.3f} "
                        f"age={c.get('age_min')}m liq=${_num(c.get('liq'),0):.0f} accel={_num(c.get('vol_accel'),0):.2f}"
                    )

            tg.send("\n".join(lines))

        except Exception as e:
            log.exception(f"Scan loop error: {e}")
            try:
                tg.send(f"Scan error: {str(e)[:180]}")
            except Exception:
                pass

        jitter_sleep(SCAN_INTERVAL_SECONDS, 0.1)