import time
import logging
from typing import Dict, Any, List, Optional

from utils import setup_logging, env_int, env_float, env_str, env_bool, jitter_sleep
from telegram_client import TelegramClient
from datasource import DexScreenerClient
from storage import Storage, make_feature_buckets
from discovery import discover_solana_candidates
from trader import buy_token, sell_token, sell_token_pct, get_wallet_summary, is_live_trading_enabled

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

# Live exits (base)
LIVE_STOP_LOSS_PCT = env_float("LIVE_STOP_LOSS_PCT", -25.0)
LIVE_TRAIL_DROP_PCT = env_float("LIVE_TRAIL_DROP_PCT", 18.0)
LIVE_MAX_HOLD_MIN = env_int("LIVE_MAX_HOLD_MIN", 240)

# Partial take profits (B)
LIVE_TP1_PCT = env_float("LIVE_TP1_PCT", 25.0)              # trigger +25%
LIVE_TP1_SELL_FRAC = env_float("LIVE_TP1_SELL_FRAC", 0.40)  # sell 40% balance

LIVE_TP2_PCT = env_float("LIVE_TP2_PCT", 60.0)              # trigger +60%
LIVE_TP2_SELL_FRAC = env_float("LIVE_TP2_SELL_FRAC", 0.30)  # sell 30% balance

# Kill switches
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
    w_bp = _num(w.get("buy_pressure"), 0.15)

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

            bal = wallet.get("balance_sol")
            if bal is not None and _num(bal, 0.0) < EMERGENCY_STOP_SOL_BAL:
                live_env = False

            opened_msgs: List[str] = []
            update_msgs: List[str] = []

            # -----------------------
            # A) Monitor open LIVE positions -> partial exits + trailing + SL/time
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

                entry = _num(pos.get("entry_price"), 0.0)
                pnl = _pnl_pct(entry, price)
                pos["last_price"] = price
                pos["pnl_pct"] = pnl
                pos["peak_pnl_pct"] = max(_num(pos.get("peak_pnl_pct"), 0.0), pnl)
                stage = int(pos.get("stage", 0))
                age_min = int((time.time() - int(pos.get("entry_ts", int(time.time())))) / 60)

                # Hard exits (sell all)
                hard_reason = None
                if pnl <= LIVE_STOP_LOSS_PCT:
                    hard_reason = "stop_loss"
                elif age_min >= LIVE_MAX_HOLD_MIN:
                    hard_reason = "time_exit"
                else:
                    # trailing after some gains
                    peak = _num(pos.get("peak_pnl_pct"), 0.0)
                    if peak >= 10.0 and (peak - pnl) >= LIVE_TRAIL_DROP_PCT:
                        hard_reason = "trail_exit"

                if hard_reason and live_env:
                    res = sell_token(mint)
                    store.state.setdefault("live_trades", []).append({
                        "ts": int(time.time()),
                        "mint": mint,
                        "type": "SELL_ALL",
                        "reason": hard_reason,
                        "result": res,
                        "pnl_est_pct": pnl,
                    })
                    update_msgs.append(
                        f"Live sell-all {mint[:6]} reason={hard_reason} success={res.get('success')} pnl_est={pnl:.2f}%"
                    )

                    if pnl < 0:
                        lr["consec_losses"] = int(lr.get("consec_losses", 0)) + 1
                        lr["loss_sol"] = float(lr.get("loss_sol", 0.0)) + float(LIVE_SOL_PER_TRADE)
                    else:
                        lr["consec_losses"] = 0

                    continue  # closed

                # Partial exits (TP1, TP2) - only if still live-enabled
                did_partial = False
                if live_env:
                    if stage == 0 and pnl >= LIVE_TP1_PCT:
                        res = sell_token_pct(mint, LIVE_TP1_SELL_FRAC)
                        store.state.setdefault("live_trades", []).append({
                            "ts": int(time.time()),
                            "mint": mint,
                            "type": "SELL_PARTIAL",
                            "reason": "tp1",
                            "pct": LIVE_TP1_SELL_FRAC,
                            "result": res,
                            "pnl_est_pct": pnl,
                        })
                        pos["stage"] = 1
                        did_partial = True
                        update_msgs.append(
                            f"Live TP1 partial {mint[:6]} sell={LIVE_TP1_SELL_FRAC:.2f} success={res.get('success')} pnl_est={pnl:.2f}%"
                        )

                    elif stage == 1 and pnl >= LIVE_TP2_PCT:
                        res = sell_token_pct(mint, LIVE_TP2_SELL_FRAC)
                        store.state.setdefault("live_trades", []).append({
                            "ts": int(time.time()),
                            "mint": mint,
                            "type": "SELL_PARTIAL",
                            "reason": "tp2",
                            "pct": LIVE_TP2_SELL_FRAC,
                            "result": res,
                            "pnl_est_pct": pnl,
                        })
                        pos["stage"] = 2
                        did_partial = True
                        update_msgs.append(
                            f"Live TP2 partial {mint[:6]} sell={LIVE_TP2_SELL_FRAC:.2f} success={res.get('success')} pnl_est={pnl:.2f}%"
                        )

                # Keep position open after partials; trailing handles the rest
                # (Optional improvement later: if stage==2 and pnl collapses, exit remainder faster)
                if did_partial:
                    # After partial profit taking, reset loss streak
                    lr["consec_losses"] = 0

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
                    win, pnl2 = store.close_paper_trade(t["id"], price, reason)
                    update_msgs.append(f"Paper closed {t['id']} pnl={pnl2:.2f}% reason={reason} win={win}")

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

                        # Live mirror buy (only if eligible + enabled)
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

                                if res.get("success"):
                                    store.state.setdefault("open_live_positions", []).append({
                                        "mint": mint,
                                        "entry_price": price,
                                        "entry_ts": int(time.time()),
                                        "last_price": price,
                                        "pnl_pct": 0.0,
                                        "peak_pnl_pct": 0.0,
                                        "stage": 0,
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
                f"Risk: daily_loss_sol={_num(lr.get('loss_sol'),0):.3f} consec_losses={int(lr.get('consec_losses',0))}",
                f"Wallet: {wallet.get('balance_sol')} SOL",
                f"Candidates: discovered={len(discovered)} filtered={len(candidates)} ranked={len(ranked)}",
            ]

            if opened_msgs:
                lines.append("Actions:")
                lines.extend(opened_msgs)

            if update_msgs:
                lines.append("Updates:")
                lines.extend(update_msgs[:10])

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