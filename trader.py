"""
trader.py - Solana Trading Module for Memecoinsnipa

Uses Jupiter Swap API (swap/v1) for token swaps on Solana.
Handles buy/sell with safety controls.

This version fixes:
- Missing exports expected by scanner.py: get_wallet_summary(), is_live_trading_enabled()
- Response schema: returns {"success": bool, "signature": str|None, "error": str|None, ...}
- Jupiter endpoint updates (swap/v1) and proper swap payload so swapTransaction is returned
- sell_token() now works with scanner.py calling sell_token(mint) (sells 100% by default)
"""

import os
import time
import json
import base64
import logging
import requests
import base58
from typing import Any, Dict, Optional, Tuple

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

log = logging.getLogger("trader")

# ============================================================
# CONSTANTS
# ============================================================

LAMPORTS_PER_SOL = 1_000_000_000
SOL_MINT = "So11111111111111111111111111111111111111112"

# Jupiter Swap API v1 (current)
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"

# Defaults / safety
DEFAULT_SLIPPAGE_BPS = 2500  # 25%
MAX_RETRIES = 3
CONFIRM_TIMEOUT = 60
MIN_SOL_RESERVE = 0.01  # keep some SOL for fees

# RPC
RPC_URL = os.environ.get("SOLANA_RPC_URL", "").strip() or "https://api.mainnet-beta.solana.com"

# Wallet / env flags
PRIVATE_KEY_B58 = os.environ.get("WALLET_PRIVATE_KEY_BASE58", "").strip()
LIVE_TRADING_ENABLED = os.environ.get("LIVE_TRADING_ENABLED", "false").strip().lower() in ("1", "true", "yes", "y")
MAX_SOL_PER_TRADE = float(os.environ.get("MAX_SOL_PER_TRADE", "0.03"))

# ============================================================
# RPC HELPERS
# ============================================================

def _rpc(method: str, params: list) -> Dict[str, Any]:
    r = requests.post(
        RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["result"]

def _get_sol_balance(pubkey: str) -> float:
    res = _rpc("getBalance", [pubkey, {"commitment": "confirmed"}])
    lamports = int(res.get("value", 0))
    return lamports / LAMPORTS_PER_SOL

def _get_token_account_for_mint(owner: str, mint: str) -> Optional[str]:
    # Use jsonParsed to easily read mint + amounts
    res = _rpc(
        "getTokenAccountsByOwner",
        [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    )
    value = res.get("value", [])
    if not value:
        return None
    # choose the first account (should be ATA); if multiple, pick the one with largest balance
    best = None
    best_amt = -1
    for acc in value:
        try:
            info = acc["account"]["data"]["parsed"]["info"]
            amt = int(info["tokenAmount"]["amount"])
            if amt > best_amt:
                best_amt = amt
                best = acc["pubkey"]
        except Exception:
            continue
    return best

def _get_token_balance_raw(owner: str, mint: str) -> Tuple[int, int]:
    token_acc = _get_token_account_for_mint(owner, mint)
    if not token_acc:
        return 0, 0
    bal = _rpc("getTokenAccountBalance", [token_acc, {"commitment": "confirmed"}])
    val = bal.get("value", {})
    amount = int(val.get("amount", "0"))
    decimals = int(val.get("decimals", 0))
    return amount, decimals

# ============================================================
# KEYPAIR / TX HELPERS
# ============================================================

def _load_keypair() -> Keypair:
    if not PRIVATE_KEY_B58:
        raise RuntimeError("WALLET_PRIVATE_KEY_BASE58 env var is missing")
    try:
        secret = base58.b58decode(PRIVATE_KEY_B58)
        return Keypair.from_bytes(secret)
    except Exception as e:
        raise RuntimeError(f"Failed to load wallet private key: {e}")

def _send_and_confirm_raw_tx(b64_tx: str, timeout_s: int = CONFIRM_TIMEOUT) -> str:
    # Send
    sig = _rpc("sendTransaction", [b64_tx, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
    if not isinstance(sig, str):
        raise RuntimeError(f"Unexpected sendTransaction result: {sig}")

    # Confirm
    start = time.time()
    while time.time() - start < timeout_s:
        st = _rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
        v = (st.get("value") or [None])[0]
        if v and v.get("confirmationStatus") in ("confirmed", "finalized"):
            err = v.get("err")
            if err:
                raise RuntimeError(f"Transaction failed: {err}")
            return sig
        time.sleep(1.5)

    raise RuntimeError(f"Timed out confirming tx {sig}")

# ============================================================
# JUPITER HELPERS
# ============================================================

def _jup_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> Dict[str, Any]:
    # amount is integer raw units (lamports for SOL; base units for SPL token)
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(int(amount)),
        "slippageBps": str(int(slippage_bps)),
    }
    r = requests.get(JUPITER_QUOTE_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    # On v1, quote returns an object (not a list) containing routePlan, outAmount, etc.
    if not isinstance(data, dict) or not data.get("routePlan"):
        # Sometimes Jupiter returns {"error": "..."}
        raise RuntimeError(data.get("error", "Quote returned no routePlan"))
    return data

def _jup_swap_tx(quote_response: Dict[str, Any], user_pubkey: str) -> str:
    payload = {
        "quoteResponse": quote_response,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        # Priority fee helps memecoins, but keep conservative unless user sets env
        "prioritizationFeeLamports": int(os.environ.get("PRIORITY_FEE_LAMPORTS", "0") or 0),
    }
    r = requests.post(JUPITER_SWAP_URL, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    swap_tx = data.get("swapTransaction")
    if not swap_tx:
        raise RuntimeError(data.get("error", "No swapTransaction returned"))
    return swap_tx

# ============================================================
# PUBLIC API (scanner.py expects these)
# ============================================================

def is_live_trading_enabled() -> bool:
    return bool(LIVE_TRADING_ENABLED)

def get_wallet_summary() -> Dict[str, Any]:
    """Return wallet + mode info for scanner status line."""
    try:
        kp = _load_keypair()
        pub = str(kp.pubkey())
        bal = _get_sol_balance(pub)
        return {
            "balance_sol": round(bal, 6),
            "live_trading": is_live_trading_enabled(),
            "max_per_trade": float(os.environ.get("MAX_SOL_PER_TRADE", str(MAX_SOL_PER_TRADE))),
            "rpc_url": RPC_URL,
        }
    except Exception as e:
        return {"error": str(e)[:300]}

def buy_token(token_mint: str, sol_amount: float, slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> Dict[str, Any]:
    """
    Swap SOL -> token_mint using Jupiter.
    Returns: {"success": bool, "signature": str|None, "error": str|None, "details": {...}}
    """
    if not is_live_trading_enabled():
        return {"success": False, "signature": None, "error": "LIVE_TRADING_ENABLED is false", "details": {}}

    try:
        kp = _load_keypair()
        user_pub = str(kp.pubkey())

        sol_balance = _get_sol_balance(user_pub)
        if sol_balance - MIN_SOL_RESERVE <= 0:
            return {"success": False, "signature": None, "error": "Insufficient SOL for fees", "details": {"balance_sol": sol_balance}}

        spend = min(sol_amount, max(sol_balance - MIN_SOL_RESERVE, 0))
        if spend <= 0:
            return {"success": False, "signature": None, "error": "Spend amount too small after reserve", "details": {"balance_sol": sol_balance}}

        amount_lamports = int(spend * LAMPORTS_PER_SOL)

        quote = _jup_quote(SOL_MINT, token_mint, amount_lamports, slippage_bps)
        swap_tx_b64 = _jup_swap_tx(quote, user_pub)

        # Sign tx
        tx = VersionedTransaction.from_bytes(base64.b64decode(swap_tx_b64))
        signed = VersionedTransaction(tx.message, [kp])
        sig = _send_and_confirm_raw_tx(base64.b64encode(bytes(signed)).decode("utf-8"))

        return {"success": True, "signature": sig, "error": None, "details": {"spent_sol": spend, "slippage_bps": slippage_bps}}
    except Exception as e:
        return {"success": False, "signature": None, "error": str(e)[:300], "details": {}}

def sell_token(token_mint: str, pct: float = 1.0, slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> Dict[str, Any]:
    """
    Swap token_mint -> SOL using Jupiter.
    Default pct=1.0 sells 100% of the token balance (scanner calls sell_token(mint) with 1 arg).
    """
    if not is_live_trading_enabled():
        return {"success": False, "signature": None, "error": "LIVE_TRADING_ENABLED is false", "details": {}}

    try:
        kp = _load_keypair()
        user_pub = str(kp.pubkey())

        raw_amt, decimals = _get_token_balance_raw(user_pub, token_mint)
        if raw_amt <= 0:
            return {"success": False, "signature": None, "error": "No token balance to sell", "details": {"decimals": decimals}}

        pct = max(0.0, min(float(pct), 1.0))
        sell_amt = int(raw_amt * pct)
        if sell_amt <= 0:
            return {"success": False, "signature": None, "error": "Sell amount rounds to 0", "details": {"raw_amt": raw_amt, "pct": pct}}

        quote = _jup_quote(token_mint, SOL_MINT, sell_amt, slippage_bps)
        swap_tx_b64 = _jup_swap_tx(quote, user_pub)

        tx = VersionedTransaction.from_bytes(base64.b64decode(swap_tx_b64))
        signed = VersionedTransaction(tx.message, [kp])
        sig = _send_and_confirm_raw_tx(base64.b64encode(bytes(signed)).decode("utf-8"))

        return {"success": True, "signature": sig, "error": None, "details": {"sold_pct": pct, "sold_raw": sell_amt, "decimals": decimals}}
    except Exception as e:
        return {"success": False, "signature": None, "error": str(e)[:300], "details": {}}
