"""
trader.py - Solana Trading Module (Jupiter v6) for Memecoinsnipa

Purpose:
- Execute REAL buys/sells on Solana via Jupiter.
- Provide wallet status for the scanner.
- Match the scanner.py expectations:
    * buy_token(mint, sol_amount) -> {"success": bool, "signature": str|None, "error": str|None}
    * sell_token(mint)            -> {"success": bool, "signature": str|None, "error": str|None}
    * get_wallet_summary()        -> {"balance_sol": float, "pubkey": str, "live_trading": bool, "max_per_trade": float} OR {"error": "..."}
    * is_live_trading_enabled()   -> bool

Key fixes (vs common broken versions):
- Uses CURRENT Jupiter endpoints:
    https://quote-api.jup.ag/v6/quote
    https://quote-api.jup.ag/v6/swap
  (Old endpoints like https://api.jup.ag/swap/v1/quote may return 401 now.)
- Passes a SINGLE selected route object to /swap (quote["data"][0]) — not the whole quote payload.
  This prevents: "No swapTransaction returned".
- Sell function auto-detects your token balance and sells 100% by default.
"""

from __future__ import annotations

import os
import time
import json
import base64
import logging
from typing import Any, Dict, Optional, Tuple

import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

log = logging.getLogger("trader")

# =========================
# Constants / Config
# =========================

LAMPORTS_PER_SOL = 1_000_000_000
SOL_MINT = "So11111111111111111111111111111111111111112"

# CURRENT Jupiter v6 endpoints
JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"

DEFAULT_SLIPPAGE_BPS = int(os.environ.get("DEFAULT_SLIPPAGE_BPS", "2500"))  # 25%
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
CONFIRM_TIMEOUT = int(os.environ.get("CONFIRM_TIMEOUT", "60"))

# Preflight ON by default (safer; fewer "ghost tx" situations)
SKIP_PREFLIGHT = os.environ.get("SKIP_PREFLIGHT", "false").lower() == "true"
PREFLIGHT_COMMITMENT = os.environ.get("PREFLIGHT_COMMITMENT", "processed")
CONFIRM_COMMITMENT = os.environ.get("CONFIRM_COMMITMENT", "confirmed")

# Optional priority fee (0 disables)
PRIORITY_FEE_LAMPORTS = int(os.environ.get("PRIORITY_FEE_LAMPORTS", "0"))

# Keep some SOL so you never hard-brick yourself
MIN_SOL_RESERVE = float(os.environ.get("MIN_SOL_RESERVE", "0.01"))

# Scanner uses this env var for sizing trades
MAX_SOL_PER_TRADE_DEFAULT = float(os.environ.get("MAX_SOL_PER_TRADE", "0.03"))

# RPC
DEFAULT_PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
RPC_TIMEOUT = int(os.environ.get("RPC_TIMEOUT", "30"))


# =========================
# Env / Feature flags
# =========================

def is_live_trading_enabled() -> bool:
    """
    Scanner gates real trades on this.
    Accepts: "true/1/yes/on" (case-insensitive).
    """
    v = (os.environ.get("LIVE_TRADING_ENABLED", "") or os.environ.get("LIVE_TRADING", "")).strip().lower()
    return v in ("1", "true", "yes", "y", "on")


# =========================
# RPC helpers
# =========================

def _rpc_url() -> str:
    # Allow scanner.py to run even if user forgot SOLANA_RPC_URL.
    # But warn: public RPC can be flaky for trading.
    url = os.environ.get("SOLANA_RPC_URL", "").strip()
    return url or DEFAULT_PUBLIC_RPC

def _rpc(method: str, params: list) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = requests.post(_rpc_url(), json=payload, timeout=RPC_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data["result"]

def _sol_balance(pubkey_str: str) -> float:
    res = _rpc("getBalance", [pubkey_str, {"commitment": "processed"}])
    return float(res["value"]) / LAMPORTS_PER_SOL


# =========================
# Wallet loading
# =========================

def _load_wallet() -> Keypair:
    """
    Supports multiple env var names (so you don't have to rename things again):

    Preferred (base58 64-byte secret key):
      - WALLET_PRIVATE_KEY_BASE58   (what scanner.py expects)
      - SOLANA_PRIVATE_KEY_B58
      - SOLANA_PRIVATE_KEY_BASE58

    Also supports JSON array of ints (Phantom export style sometimes):
      - SOLANA_PRIVATE_KEY          (e.g. "[12,34,...]")
    """
    # Base58 variants
    b58 = (
        os.environ.get("WALLET_PRIVATE_KEY_BASE58", "")
        or os.environ.get("SOLANA_PRIVATE_KEY_B58", "")
        or os.environ.get("SOLANA_PRIVATE_KEY_BASE58", "")
    ).strip()

    if b58:
        try:
            import base58
            secret = base58.b58decode(b58)
            kp = Keypair.from_bytes(secret)
            log.info(f"Wallet loaded: {kp.pubkey()}")
            return kp
        except Exception as e:
            raise RuntimeError(f"Failed to load base58 key: {e}")

    # JSON array variant
    raw = os.environ.get("SOLANA_PRIVATE_KEY", "").strip()
    if raw:
        try:
            arr = json.loads(raw)
            secret = bytes(arr)
            kp = Keypair.from_bytes(secret)
            log.info(f"Wallet loaded: {kp.pubkey()}")
            return kp
        except Exception as e:
            raise RuntimeError(f"Failed to load JSON key array: {e}")

    raise RuntimeError("WALLET_PRIVATE_KEY_BASE58 env var is missing (or SOLANA_PRIVATE_KEY_B58 / SOLANA_PRIVATE_KEY_BASE58 / SOLANA_PRIVATE_KEY)")


# =========================
# Jupiter helpers
# =========================

def _http_get(url: str, params: dict, timeout: int = 30) -> dict:
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

def _http_post(url: str, body: dict, timeout: int = 30) -> dict:
    r = requests.post(url, json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()

def _get_quote_route(input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> dict:
    """
    Returns a SINGLE selected route object (quote['data'][0]).
    Passing the whole quote payload to /swap is a common bug.
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(slippage_bps),
        "onlyDirectRoutes": "false",
    }
    quote = _http_get(JUPITER_QUOTE_URL, params=params, timeout=30)

    routes = quote.get("data") if isinstance(quote, dict) else None
    if not routes:
        raise RuntimeError(f"Jupiter quote returned no routes: {str(quote)[:300]}")
    return routes[0]

def _get_swap_tx(route: dict, user_pubkey: str) -> str:
    """
    Returns base64 swapTransaction string.
    """
    body: Dict[str, Any] = {
        "quoteResponse": route,        # IMPORTANT: one route object
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
    }
    if PRIORITY_FEE_LAMPORTS > 0:
        body["prioritizationFeeLamports"] = PRIORITY_FEE_LAMPORTS

    data = _http_post(JUPITER_SWAP_URL, body=body, timeout=30)
    swap_tx = data.get("swapTransaction")
    if not swap_tx:
        # Provide a tight error message for Telegram
        raise RuntimeError(f"No swapTransaction returned. Keys={list(data.keys())} body_error={str(data)[:250]}")
    return swap_tx

def _send_raw_tx(b64_tx: str) -> str:
    cfg = {
        "encoding": "base64",
        "skipPreflight": SKIP_PREFLIGHT,
        "preflightCommitment": PREFLIGHT_COMMITMENT,
        "maxRetries": 3,
    }
    return _rpc("sendTransaction", [b64_tx, cfg])

def _confirm_sig(sig: str, timeout_s: int) -> bool:
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            st = _rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
            val = st["value"][0]
            if val is None:
                time.sleep(2)
                continue

            if val.get("err") is not None:
                log.warning(f"Tx {sig} failed: {val.get('err')}")
                return False

            conf = val.get("confirmationStatus")
            if conf in ("confirmed", "finalized"):
                return True

            time.sleep(2)
        except Exception as e:
            log.warning(f"Confirm status error for {sig}: {e}")
            time.sleep(2)

    # last chance
    try:
        tx = _rpc("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": CONFIRM_COMMITMENT}])
        return bool(tx)
    except Exception:
        return False


# =========================
# Token balance helpers (for sells)
# =========================

def _get_token_balance_raw(owner_pubkey: str, mint: str) -> Tuple[int, int]:
    """
    Returns (amount_raw, decimals) aggregated across token accounts.
    """
    res = _rpc(
        "getTokenAccountsByOwner",
        [
            owner_pubkey,
            {"mint": mint},
            {"encoding": "jsonParsed", "commitment": "processed"},
        ],
    )

    amount_raw = 0
    decimals = 0
    for item in res.get("value", []):
        info = (
            item.get("account", {})
            .get("data", {})
            .get("parsed", {})
            .get("info", {})
        )
        ta = info.get("tokenAmount", {})
        amt_str = ta.get("amount", "0")
        dec = ta.get("decimals", 0)
        try:
            amount_raw += int(amt_str)
            decimals = int(dec)
        except Exception:
            continue

    return amount_raw, decimals


# =========================
# Public API expected by scanner.py
# =========================

def get_wallet_summary() -> dict:
    """
    Used by scanner.py to print wallet + mode.
    """
    try:
        kp = _load_wallet()
        pubkey = str(kp.pubkey())
        bal = _sol_balance(pubkey)
        return {
            "pubkey": pubkey,
            "balance_sol": round(bal, 6),
            "live_trading": is_live_trading_enabled(),
            "max_per_trade": float(os.environ.get("MAX_SOL_PER_TRADE", str(MAX_SOL_PER_TRADE_DEFAULT))),
            "rpc": _rpc_url(),
        }
    except Exception as e:
        return {"error": str(e)}


def buy_token(output_mint: str, sol_amount: float, slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> dict:
    """
    Buy `output_mint` spending `sol_amount` SOL.

    Returns:
      {"success": bool, "signature": str|None, "error": str|None}
    """
    try:
        kp = _load_wallet()
        user_pubkey = str(kp.pubkey())

        bal = _sol_balance(user_pubkey)
        if bal - sol_amount < MIN_SOL_RESERVE:
            return {
                "success": False,
                "signature": None,
                "error": f"Not enough SOL. Balance={bal:.4f}, requested={sol_amount:.4f}, reserve={MIN_SOL_RESERVE:.4f}",
            }

        lamports_in = int(sol_amount * LAMPORTS_PER_SOL)
        last_err: Optional[str] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                route = _get_quote_route(SOL_MINT, output_mint, lamports_in, slippage_bps)
                swap_tx_b64 = _get_swap_tx(route, user_pubkey)

                raw = base64.b64decode(swap_tx_b64)
                vtx = VersionedTransaction.from_bytes(raw)

                signed = VersionedTransaction(vtx.message, [kp])
                signed_b64 = base64.b64encode(bytes(signed)).decode("utf-8")

                sig = _send_raw_tx(signed_b64)
                log.info(f"BUY sent: {sig} (attempt {attempt}/{MAX_RETRIES})")

                if _confirm_sig(sig, CONFIRM_TIMEOUT):
                    return {"success": True, "signature": sig, "error": None}

                last_err = f"Sent but not confirmed after {CONFIRM_TIMEOUT}s (sig={sig})"
                time.sleep(2 + attempt)
            except requests.exceptions.HTTPError as he:
                # Tighten HTTP errors for Telegram (e.g. 401 from old endpoints)
                last_err = f"HTTP error: {str(he)[:200]}"
                time.sleep(2 + attempt)
            except Exception as e:
                last_err = str(e)[:400]
                time.sleep(2 + attempt)

        return {"success": False, "signature": None, "error": last_err or "Unknown error"}

    except Exception as e:
        return {"success": False, "signature": None, "error": str(e)[:400]}


def sell_token(input_mint: str, slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> dict:
    """
    Sell 100% of your balance of `input_mint` into SOL.

    Returns:
      {"success": bool, "signature": str|None, "error": str|None}
    """
    try:
        kp = _load_wallet()
        user_pubkey = str(kp.pubkey())

        amount_raw, decimals = _get_token_balance_raw(user_pubkey, input_mint)
        if amount_raw <= 0:
            return {"success": False, "signature": None, "error": f"No token balance found for mint {input_mint}"}

        last_err: Optional[str] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                route = _get_quote_route(input_mint, SOL_MINT, amount_raw, slippage_bps)
                swap_tx_b64 = _get_swap_tx(route, user_pubkey)

                raw = base64.b64decode(swap_tx_b64)
                vtx = VersionedTransaction.from_bytes(raw)

                signed = VersionedTransaction(vtx.message, [kp])
                signed_b64 = base64.b64encode(bytes(signed)).decode("utf-8")

                sig = _send_raw_tx(signed_b64)
                log.info(f"SELL sent: {sig} (attempt {attempt}/{MAX_RETRIES})")

                if _confirm_sig(sig, CONFIRM_TIMEOUT):
                    return {"success": True, "signature": sig, "error": None}

                last_err = f"Sent but not confirmed after {CONFIRM_TIMEOUT}s (sig={sig})"
                time.sleep(2 + attempt)

            except Exception as e:
                last_err = str(e)[:400]
                time.sleep(2 + attempt)

        return {"success": False, "signature": None, "error": last_err or "Unknown error"}

    except Exception as e:
        return {"success": False, "signature": None, "error": str(e)[:400]}
