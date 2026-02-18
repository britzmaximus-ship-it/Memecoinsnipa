"""
trader.py - Solana Trading Module for Memecoinsnipa (Scanner-compatible)

Exports:
- buy_token(mint_address, sol_amount)
- sell_token(mint_address)
- get_wallet_summary()
- is_live_trading_enabled()

Implements Jupiter Swap API v1 (api.jup.ag) + solders signing.
Robust wallet loading across env var names and key formats.
"""

import os
import time
import json
import base64
import logging
import requests
import base58

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

log = logging.getLogger("trader")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)

LAMPORTS_PER_SOL = 1_000_000_000
SOL_MINT = "So11111111111111111111111111111111111111112"

JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"

CONFIRM_TIMEOUT = 60
DEFAULT_SLIPPAGE_BPS = 2500


# ============================================================
# ENV HELPERS
# ============================================================

def is_live_trading_enabled():
    return os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true"


def get_max_sol_per_trade():
    return float(os.environ.get("MAX_SOL_PER_TRADE", "0.03"))


def get_rpc_url():
    return os.environ.get("SOLANA_RPC_URL", "").strip()


def get_jupiter_api_key():
    return os.environ.get("JUPITER_API_KEY", "").strip()


def jupiter_headers():
    headers = {"accept": "application/json"}
    key = get_jupiter_api_key()
    if key:
        headers["x-api-key"] = key
    return headers


# ============================================================
# WALLET LOADING (ROBUST)
# ============================================================

def _read_private_key_string() -> str:
    """
    Tries multiple env var names so GitHub/Railway/local all work without edits.
    Priority order:
      1) SOLANA_PRIVATE_KEY  (what your scan.yml uses)
      2) WALLET_PRIVATE_KEY_BASE58
      3) WALLET_PRIVATE_KEY
    """
    pk = (
        os.environ.get("SOLANA_PRIVATE_KEY")
        or os.environ.get("WALLET_PRIVATE_KEY_BASE58")
        or os.environ.get("WALLET_PRIVATE_KEY")
    )
    if not pk:
        raise EnvironmentError(
            "Missing private key env var. Set SOLANA_PRIVATE_KEY (preferred) "
            "or WALLET_PRIVATE_KEY(_BASE58)."
        )
    return pk.strip()


def load_wallet() -> Keypair:
    """
    Supports:
      - Base58 encoded key (32-byte seed or 64-byte secret)
      - JSON array (Solana CLI style) of 64 numbers
    """
    pk = _read_private_key_string()

    # JSON array (Solana CLI keypair file contents pasted into env)
    if pk.startswith("["):
        key_bytes = bytes(json.loads(pk))
    else:
        key_bytes = base58.b58decode(pk)

    if len(key_bytes) == 64:
        kp = Keypair.from_bytes(key_bytes)
    elif len(key_bytes) == 32:
        kp = Keypair.from_seed(key_bytes)
    else:
        raise ValueError(f"Unexpected key length: {len(key_bytes)} bytes")

    return kp


# ============================================================
# RPC
# ============================================================

def rpc_call(method, params):
    rpc_url = get_rpc_url()
    if not rpc_url:
        raise EnvironmentError("SOLANA_RPC_URL is missing")

    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = requests.post(rpc_url, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()

    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")

    return data


def wait_for_confirmation(signature: str) -> bool:
    start = time.time()
    while time.time() - start < CONFIRM_TIMEOUT:
        result = rpc_call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        status = result["result"]["value"][0]
        if status:
            if status.get("err"):
                return False
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                return True
        time.sleep(2)
    return False


# ============================================================
# JUPITER SWAP
# ============================================================

def jupiter_swap(input_mint: str, output_mint: str, amount: int, wallet: Keypair, slippage_bps: int = DEFAULT_SLIPPAGE_BPS):
    """
    amount is raw integer:
      - If input is SOL: lamports
      - If input is token: raw token units (already decimals-adjusted)
    """
    try:
        # 1) QUOTE
        quote_params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            "swapMode": "ExactIn",
        }

        quote_resp = requests.get(
            JUPITER_QUOTE_URL,
            params=quote_params,
            headers=jupiter_headers(),
            timeout=20,
        )

        if quote_resp.status_code == 401:
            return {"success": False, "signature": None, "error": "Jupiter 401 Unauthorized (quote)"}

        quote_resp.raise_for_status()
        quote = quote_resp.json()

        # 2) SWAP TX
        swap_payload = {
            "quoteResponse": quote,
            "userPublicKey": str(wallet.pubkey()),
            "wrapAndUnwrapSol": True,
        }

        swap_resp = requests.post(
            JUPITER_SWAP_URL,
            json=swap_payload,
            headers=jupiter_headers(),
            timeout=20,
        )

        if swap_resp.status_code == 401:
            return {"success": False, "signature": None, "error": "Jupiter 401 Unauthorized (swap)"}

        swap_resp.raise_for_status()
        swap_data = swap_resp.json()

        tx_b64 = swap_data.get("swapTransaction")
        if not tx_b64:
            return {"success": False, "signature": None, "error": f"No swapTransaction returned: {str(swap_data)[:300]}"}

        # 3) DESERIALIZE + SIGN
        tx_bytes = base64.b64decode(tx_b64)
        vt = VersionedTransaction.from_bytes(tx_bytes)

        sig = wallet.sign_message(bytes(vt.message))
        signed_vt = VersionedTransaction.populate(vt.message, [sig])
        raw_tx = bytes(signed_vt)

        # 4) SEND
        send_result = rpc_call(
            "sendTransaction",
            [
                base64.b64encode(raw_tx).decode(),
                {"encoding": "base64", "skipPreflight": False, "maxRetries": 3},
            ],
        )

        signature = send_result.get("result")
        if not signature:
            return {"success": False, "signature": None, "error": f"No signature returned: {send_result}"}

        # 5) CONFIRM
        confirmed = wait_for_confirmation(signature)
        if confirmed:
            return {"success": True, "signature": signature, "error": None}
        return {"success": False, "signature": signature, "error": "Transaction not confirmed"}

    except requests.exceptions.HTTPError as e:
        resp = getattr(e, "response", None)
        body = resp.text[:300] if resp is not None else ""
        return {"success": False, "signature": None, "error": f"{str(e)} | body={body}"}
    except Exception as e:
        return {"success": False, "signature": None, "error": str(e)}


# ============================================================
# BUY / SELL (Scanner expects these exact names)
# ============================================================

def buy_token(mint_address: str, sol_amount: float):
    if not is_live_trading_enabled():
        return {"success": False, "signature": None, "error": "Live trading disabled"}

    wallet = load_wallet()
    lamports = int(sol_amount * LAMPORTS_PER_SOL)
    return jupiter_swap(SOL_MINT, mint_address, lamports, wallet)


def sell_token(mint_address: str):
    if not is_live_trading_enabled():
        return {"success": False, "signature": None, "error": "Live trading disabled"}

    wallet = load_wallet()

    token_accounts = rpc_call(
        "getTokenAccountsByOwner",
        [str(wallet.pubkey()), {"mint": mint_address}, {"encoding": "jsonParsed"}],
    )

    accounts = token_accounts["result"]["value"]
    if not accounts:
        return {"success": False, "signature": None, "error": "No tokens to sell"}

    amount = int(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    return jupiter_swap(mint_address, SOL_MINT, amount, wallet)


# ============================================================
# WALLET SUMMARY (Scanner uses this)
# ============================================================

def get_wallet_summary():
    try:
        wallet = load_wallet()
        balance_data = rpc_call("getBalance", [str(wallet.pubkey())])
        balance = balance_data["result"]["value"] / LAMPORTS_PER_SOL

        return {
            "address": str(wallet.pubkey()),
            "balance_sol": round(balance, 6),
            "live_trading": is_live_trading_enabled(),
            "max_per_trade": get_max_sol_per_trade(),
            "has_jupiter_key": bool(get_jupiter_api_key()),
        }
    except Exception as e:
        return {"error": str(e)}