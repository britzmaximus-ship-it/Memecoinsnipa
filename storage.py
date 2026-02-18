import time
import logging
from typing import Dict, Any, List, Tuple
from utils import safe_json_load, safe_json_write_atomic, env_int, env_float

log = logging.getLogger("memecoinsnipa.storage")

PLAYBOOK_FILE = "playbook.json"

# Aggressive defaults (you chose Mode A)
MIN_CLOSED_PAPER_TRADES = env_int("MIN_CLOSED_PAPER_TRADES", 20)
PAPER_WINRATE_MIN = env_float("PAPER_WINRATE_MIN", 0.33)
PAPER_AVG_RETURN_MIN = env_float("PAPER_AVG_RETURN_MIN", 0.02)

# Learning update speed (higher = faster adaptation)
LEARN_RATE = env_float("LEARN_RATE", 0.15)

DEFAULT_STATE: Dict[str, Any] = {
    "scans": 0,
    "picks": [],
    "paper_trades": [],   # list of paper trade dicts
    "live_trades": [],    # list of live trade attempts/results (audit trail)
    "blacklist": [],
    "cooldowns": {},

    # self-learning model
    "model": {
        "weights": {
            "vol_accel": 0.40,
            "liq": 0.25,
            "age": 0.20,
            "buy_pressure": 0.15,
        },
        "bucket_stats": {
            # example keys:
            # "age:15-60": {"n":0,"wins":0,"sum":0.0}
        }
    },

    "stats": {
        "paper": {"open": 0, "closed": 0, "wins": 0, "losses": 0, "sum_return": 0.0},
        "live":  {"open": 0, "closed": 0, "wins": 0, "losses": 0, "sum_return": 0.0},
    },

    "live_gate": {
        "eligible": False,     # becomes True when paper performance passes thresholds
        "reason": "not evaluated",
        "last_eval_ts": 0
    },
}


def _bucket_age(age_min: int) -> str:
    if age_min < 15:
        return "0-15"
    if age_min < 60:
        return "15-60"
    if age_min < 180:
        return "60-180"
    if age_min < 360:
        return "180-360"
    if age_min < 720:
        return "360-720"
    return "720+"

def _bucket_liq(liq: float) -> str:
    if liq < 20_000:
        return "<20k"
    if liq < 50_000:
        return "20-50k"
    if liq < 100_000:
        return "50-100k"
    if liq < 250_000:
        return "100-250k"
    return "250k+"

def _bucket_accel(accel: float) -> str:
    if accel < 0.8:
        return "<0.8"
    if accel < 1.2:
        return "0.8-1.2"
    if accel < 2.0:
        return "1.2-2.0"
    if accel < 3.5:
        return "2.0-3.5"
    return "3.5+"

def _bucket_buy_pressure(buys_1h: int, sells_1h: int) -> str:
    denom = max(1, sells_1h)
    r = buys_1h / denom
    if r < 0.9:
        return "<0.9"
    if r < 1.2:
        return "0.9-1.2"
    if r < 2.0:
        return "1.2-2.0"
    return "2.0+"

def make_feature_buckets(cand: Dict[str, Any]) -> List[str]:
    """
    Produces bucket keys stored at entry and used for learning updates.
    cand should include: age_min, liq, vol_accel, buys_1h, sells_1h
    """
    age = int(cand.get("age_min", 10**9))
    liq = float(cand.get("liq", 0.0) or 0.0)
    accel = float(cand.get("vol_accel", 0.0) or 0.0)
    buys = int(cand.get("buys_1h", 0) or 0)
    sells = int(cand.get("sells_1h", 0) or 0)

    return [
        f"age:{_bucket_age(age)}",
        f"liq:{_bucket_liq(liq)}",
        f"accel:{_bucket_accel(accel)}",
        f"bp:{_bucket_buy_pressure(buys, sells)}",
    ]


class Storage:
    def __init__(self):
        self.state: Dict[str, Any] = safe_json_load(PLAYBOOK_FILE, DEFAULT_STATE)
        # Backfill keys if an old playbook exists
        for k, v in DEFAULT_STATE.items():
            if k not in self.state:
                self.state[k] = v

    def save(self):
        safe_json_write_atomic(PLAYBOOK_FILE, self.state)

    def increment_scan(self):
        self.state["scans"] = int(self.state.get("scans", 0)) + 1

    # -------------------------
    # Cooldowns / blacklist
    # -------------------------
    def set_cooldown(self, key: str, seconds: int):
        self.state.setdefault("cooldowns", {})
        self.state["cooldowns"][key] = int(time.time()) + int(seconds)

    def cooldown_active(self, key: str) -> bool:
        until = (self.state.get("cooldowns") or {}).get(key)
        if not until:
            return False
        return time.time() < float(until)

    def add_blacklist(self, mint: str):
        bl = self.state.setdefault("blacklist", [])
        if mint not in bl:
            bl.append(mint)

    def is_blacklisted(self, mint: str) -> bool:
        return mint in (self.state.get("blacklist") or [])

    # -------------------------
    # Paper trades
    # -------------------------
    def open_paper_trade(self, mint: str, entry_price: float, meta: Dict[str, Any]) -> str:
        """
        meta should include candidate features + buckets
        """
        trade_id = f"PT-{int(time.time())}-{mint[:6]}"
        trade = {
            "id": trade_id,
            "mint": mint,
            "status": "OPEN",
            "entry_price": float(entry_price),
            "entry_ts": int(time.time()),
            "last_price": float(entry_price),
            "pnl_pct": 0.0,
            "peak_pnl_pct": 0.0,
            "meta": meta,
        }
        self.state.setdefault("paper_trades", []).append(trade)
        self._recount_paper_stats(open_delta=1, closed_delta=0)
        return trade_id

    def get_open_paper_trades(self) -> List[Dict[str, Any]]:
        return [t for t in (self.state.get("paper_trades") or []) if t.get("status") == "OPEN"]

    def update_paper_trade_mark(self, trade_id: str, price: float) -> None:
        for t in (self.state.get("paper_trades") or []):
            if t.get("id") == trade_id and t.get("status") == "OPEN":
                t["last_price"] = float(price)
                pnl = (float(price) / max(1e-12, float(t["entry_price"])) - 1.0) * 100.0
                t["pnl_pct"] = pnl
                t["peak_pnl_pct"] = max(float(t.get("peak_pnl_pct", 0.0)), pnl)
                return

    def close_paper_trade(self, trade_id: str, exit_price: float, reason: str) -> Tuple[bool, float]:
        """
        Returns (win, pnl_pct)
        """
        for t in (self.state.get("paper_trades") or []):
            if t.get("id") == trade_id and t.get("status") == "OPEN":
                t["status"] = "CLOSED"
                t["exit_price"] = float(exit_price)
                t["exit_ts"] = int(time.time())
                t["close_reason"] = str(reason)

                pnl = (float(exit_price) / max(1e-12, float(t["entry_price"])) - 1.0) * 100.0
                t["pnl_pct"] = pnl
                win = pnl > 0

                # stats
                self._recount_paper_stats(open_delta=-1, closed_delta=1, win=win, pnl_pct=pnl)

                # learning
                buckets = (t.get("meta") or {}).get("buckets") or []
                self._learn_from_trade(buckets=buckets, win=win, pnl_pct=pnl)

                # gate re-eval
                self.evaluate_live_gate()

                return win, pnl
        return False, 0.0

    def _recount_paper_stats(self, open_delta=0, closed_delta=0, win: bool = None, pnl_pct: float = 0.0):
        s = self.state.setdefault("stats", {}).setdefault("paper", {"open": 0, "closed": 0, "wins": 0, "losses": 0, "sum_return": 0.0})
        s["open"] = max(0, int(s.get("open", 0)) + int(open_delta))
        s["closed"] = max(0, int(s.get("closed", 0)) + int(closed_delta))
        if win is True:
            s["wins"] = int(s.get("wins", 0)) + 1
        if win is False:
            s["losses"] = int(s.get("losses", 0)) + 1
        if closed_delta:
            s["sum_return"] = float(s.get("sum_return", 0.0)) + float(pnl_pct)

    # -------------------------
    # Learning model updates
    # -------------------------
    def _learn_from_trade(self, buckets: List[str], win: bool, pnl_pct: float):
        model = self.state.setdefault("model", {})
        bucket_stats = model.setdefault("bucket_stats", {})

        for b in buckets:
            st = bucket_stats.setdefault(b, {"n": 0, "wins": 0, "sum": 0.0})
            st["n"] = int(st["n"]) + 1
            if win:
                st["wins"] = int(st["wins"]) + 1
            st["sum"] = float(st["sum"]) + float(pnl_pct)

        # Optional: slowly bias weights based on overall performance
        # Keep weights normalized and stable
        w = model.setdefault("weights", {"vol_accel": 0.40, "liq": 0.25, "age": 0.20, "buy_pressure": 0.15})

        # If trade was a solid win, slightly increase vol_accel/age emphasis; if loss, slightly reduce
        delta = LEARN_RATE * (1.0 if win else -1.0)
        w["vol_accel"] = max(0.05, float(w["vol_accel"]) + 0.06 * delta)
        w["age"] = max(0.05, float(w["age"]) + 0.03 * delta)
        w["liq"] = max(0.05, float(w["liq"]) - 0.04 * delta)  # inverse shift
        w["buy_pressure"] = max(0.05, float(w["buy_pressure"]) - 0.05 * delta)

        # Renormalize to sum to 1.0
        s = sum(float(x) for x in w.values())
        for k in list(w.keys()):
            w[k] = float(w[k]) / max(1e-9, s)

    def bucket_edge(self, bucket_key: str) -> float:
        """
        Returns an "edge score" based on bucket win rate and avg return.
        Positive edge -> boost. Negative edge -> penalty.
        """
        st = (self.state.get("model") or {}).get("bucket_stats", {}).get(bucket_key)
        if not st:
            return 0.0
        n = int(st.get("n", 0))
        if n < 5:
            return 0.0  # too little data
        wins = int(st.get("wins", 0))
        wr = wins / max(1, n)
        avg = float(st.get("sum", 0.0)) / max(1, n)

        # Edge: prioritize win rate, then avg return
        return (wr - 0.5) * 2.0 + (avg / 20.0)  # scale avg return to ~[-1,1] range

    # -------------------------
    # Live gate evaluation
    # -------------------------
    def evaluate_live_gate(self) -> Dict[str, Any]:
        """
        Sets live_gate.eligible based on paper trade performance.
        """
        paper = (self.state.get("stats") or {}).get("paper") or {}
        closed = int(paper.get("closed", 0))
        wins = int(paper.get("wins", 0))
        sum_ret = float(paper.get("sum_return", 0.0))

        gate = self.state.setdefault("live_gate", {"eligible": False, "reason": "not evaluated", "last_eval_ts": 0})
        gate["last_eval_ts"] = int(time.time())

        if closed < MIN_CLOSED_PAPER_TRADES:
            gate["eligible"] = False
            gate["reason"] = f"need_more_paper_closed ({closed}/{MIN_CLOSED_PAPER_TRADES})"
            return gate

        wr = wins / max(1, closed)
        avg = sum_ret / max(1, closed) / 100.0  # convert pct to fraction

        if wr < PAPER_WINRATE_MIN:
            gate["eligible"] = False
            gate["reason"] = f"paper_winrate_low (wr={wr:.2f} < {PAPER_WINRATE_MIN:.2f})"
            return gate

        if avg < PAPER_AVG_RETURN_MIN:
            gate["eligible"] = False
            gate["reason"] = f"paper_avg_return_low (avg={avg:.3f} < {PAPER_AVG_RETURN_MIN:.3f})"
            return gate

        gate["eligible"] = True
        gate["reason"] = f"eligible (wr={wr:.2f}, avg={avg:.3f}, closed={closed})"
        return gate