"""
trader.py - Solana Trading Module for Memecoinsnipa

Uses Jupiter V6 API for token swaps on Solana.
Handles buy/sell with safety controls.

Fixes:
- Avoids "ghost" txs by NOT skipping preflight by default
- Stronger confirmation logic (getSignatureStatuses + getTransaction fallback)
- Retries with a fresh Jupiter swap each attempt (prevents expired blockhash issues)
"""

import os
import time
import json
import base64
import logging
import requests

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

log = logging.getLogger("trader")

# ============================================================
# CONSTANTS
# ============================================================

LAMPORTS_PER_SOL = 1_000_000_000
SOL_MINT = "So11111111111111111111111111111111111111112"

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"

DEFAULT_SLIPPAGE_BPS = int(os.environ.get("DEFAULT_SLIPPAGE_BPS", "2500"))  # 25%
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
CONFIRM_TIMEOUT = int(os.environ.get("CONFIRM_TIMEOUT", "60"))

# IMPORTANT: default is FALSE (preflight ON)
SKIP_PREFLIGHT = os.environ.get("SKIP_PREFLIGHT", "false").lower() == "true"

# Commitments: processed is fastest, finalized is strictest
PREFLIGHT_COMMITMENT = os.environ.get("PREFLIGHT_COMMITMENT", "processed")
CONFIRM_COMMITMENT   = os.environ.get("CONFIRM_COMMITMENT", "confirmed")

# Optional priority fee (set 0 to disable)
PRIORITY_FEE_LAMPORTS = int(os.environ.get("PRIORITY_FEE_LAMPORTS", "0"))

# Safety reserve to avoid going to 0 SOL
MIN_SOL_RESERVE = float(os.environ.get("MIN_SOL_RESERVE", "0.01"))

# ============================================================
# HELPERS
# ============================================================

def _rpc_url() -> str:
    url = os.environ.get("SOLANA_RPC_URL", "").strip()
    if not url:
        raise RuntimeError("SOLANA_RPC_URL is not set")
    return url

def _rpc(method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = requests.post(_rpc_url(), json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"RPC error {data['error']}")
    return data["result"]

def _load_wallet() -> Keypair:
    """
    Expected env: SOLANA_PRIVATE_KEY_B58 (base58-encoded 64-byte secret key)
    or SOLANA_PRIVATE_KEY (JSON array of ints).
    """
    b58 = os.environ.get("SOLANA_PRIVATE_KEY_B58", "").strip()
    if b58:
        import base58  # only import if needed
        secret = base58.b58decode(b58)
        kp = Keypair.from_bytes(secret)
        log.info(f"Wallet loaded: {str(kp.pubkey())}")
        return kp

    raw = os.environ.get("SOLANA_PRIVATE_KEY", "").strip()
    if raw:
        arr = json.loads(raw)
        secret = bytes(arr)
        kp = Keypair.from_bytes(secret)
        log.info(f"Wallet loaded: {str(kp.pubkey())}")
        return kp

    raise RuntimeError("Missing wallet key. Set SOLANA_PRIVATE_KEY_B58 or SOLANA_PRIVATE_KEY")

def _sol_balance(pubkey_str: str) -> float:
    res = _rpc("getBalance", [pubkey_str, {"commitment": "processed"}])
    lamports = res["value"]
    return lamports / LAMPORTS_PER_SOL

def _get_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int):
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(slippage_bps),
        "onlyDirectRoutes": "false",
    }
    r = requests.get(JUPITER_QUOTE_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def _get_swap_tx(quote_response: dict, user_pubkey: str):
    body = {
        "quoteResponse": quote_response,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
    }

    # Optional priority fee
    if PRIORITY_FEE_LAMPORTS > 0:
        body["prioritizationFeeLamports"] = PRIORITY_FEE_LAMPORTS

    r = requests.post(JUPITER_SWAP_URL, json=body, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "swapTransaction" not in data:
        raise RuntimeError(f"Jupiter swap response missing swapTransaction: {data}")
    return data["swapTransaction"]

def _send_raw_tx(b64_tx: str) -> str:
    """
    Send with preflight ON by default to prevent ghost txs.
    """
    cfg = {
        "encoding": "base64",
        "skipPreflight": SKIP_PREFLIGHT,
        "preflightCommitment": PREFLIGHT_COMMITMENT,
        "maxRetries": 3,
    }
    sig = _rpc("sendTransaction", [b64_tx, cfg])
    return sig

def _confirm_sig(sig: str, timeout_s: int) -> bool:
    """
    Confirm using getSignatureStatuses.
    Also tries getTransaction as a fallback (sometimes status lags).
    """
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            st = _rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
            val = st["value"][0]

            # Not seen yet
            if val is None:
                time.sleep(2)
                continue

            # RPC explicitly reports an error
            if val.get("err") is not None:
                log.warning(f"Tx {sig} failed with err: {val.get('err')}")
                return False

            conf = val.get("confirmationStatus")
            # confirmationStatus can be: processed/confirmed/finalized
            if conf in ("confirmed", "finalized"):
                return True

            # If confirmations is None but status exists, keep waiting
            time.sleep(2)
        except Exception as e:
            # fallback: try getTransaction (may exist even if statuses is flaky)
            try:
                tx = _rpc("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed"}])
                if tx:
                    return True
            except Exception:
                pass
            log.warning(f"Confirm check error for {sig}: {e}")
            time.sleep(2)

    # Final fallback
    try:
        tx = _rpc("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        if tx:
            return True
    except Exception:
        pass

    return False

# ============================================================
# PUBLIC API
# ============================================================

def buy_token(output_mint: str, sol_amount: float, slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> dict:
    """
    Buys output_mint using SOL.

    Returns dict:
      { ok: bool, signature: str|None, error: str|None }
    """
    kp = _load_wallet()
    user_pubkey = str(kp.pubkey())

    # Safety: keep some SOL in wallet
    bal = _sol_balance(user_pubkey)
    if bal - sol_amount < MIN_SOL_RESERVE:
        return {"ok": False, "signature": None, "error": f"Not enough SOL. Balance={bal:.4f}, requested={sol_amount}, reserve={MIN_SOL_RESERVE}"}

    lamports_in = int(sol_amount * LAMPORTS_PER_SOL)

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Always fetch a fresh quote + swap tx each attempt (prevents expired blockhash)
            quote = _get_quote(SOL_MINT, output_mint, lamports_in, slippage_bps)
            swap_tx_b64 = _get_swap_tx(quote, user_pubkey)

            raw = base64.b64decode(swap_tx_b64)
            vtx = VersionedTransaction.from_bytes(raw)

            # Sign
            signed = VersionedTransaction(vtx.message, [kp])
            signed_b64 = base64.b64encode(bytes(signed)).decode("utf-8")

            sig = _send_raw_tx(signed_b64)
            log.info(f"Tx sent: {sig} (attempt {attempt}/{MAX_RETRIES}) - waiting for confirmation...")

            ok = _confirm_sig(sig, CONFIRM_TIMEOUT)
            if ok:
                log.info(f"Tx confirmed: {sig}")
                return {"ok": True, "signature": sig, "error": None}

            # If not confirmed, treat as dropped/pending-too-long and retry
            last_err = f"Transaction sent but not confirmed after {CONFIRM_TIMEOUT}s (sig={sig})"
            log.warning(last_err)

            # Small backoff before retry
            time.sleep(2 + attempt)

        except Exception as e:
            last_err = str(e)
            log.warning(f"Buy attempt {attempt} failed: {last_err}")
            time.sleep(2 + attempt)

    return {"ok": False, "signature": None, "error": last_err}


def sell_token(input_mint: str, token_amount_raw: int, slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> dict:
    """
    Sells input_mint into SOL.

    token_amount_raw = amount in the token's smallest units (raw integer).
    """
    kp = _load_wallet()
    user_pubkey = str(kp.pubkey())

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            quote = _get_quote(input_mint, SOL_MINT, token_amount_raw, slippage_bps)
            swap_tx_b64 = _get_swap_tx(quote, user_pubkey)

            raw = base64.b64decode(swap_tx_b64)
            vtx = VersionedTransaction.from_bytes(raw)

            signed = VersionedTransaction(vtx.message, [kp])
            signed_b64 = base64.b64encode(bytes(signed)).decode("utf-8")

            sig = _send_raw_tx(signed_b64)
            log.info(f"Tx sent: {sig} (attempt {attempt}/{MAX_RETRIES}) - waiting for confirmation...")

            ok = _confirm_sig(sig, CONFIRM_TIMEOUT)
            if ok:
                log.info(f"Tx confirmed: {sig}")
                return {"ok": True, "signature": sig, "error": None}

            last_err = f"Transaction sent but not confirmed after {CONFIRM_TIMEOUT}s (sig={sig})"
            log.warning(last_err)
            time.sleep(2 + attempt)

        except Exception as e:
            last_err = str(e)
            log.warning(f"Sell attempt {attempt} failed: {last_err}")
            time.sleep(2 + attempt)

    return {"ok": False, "signature": None, "error": last_err}