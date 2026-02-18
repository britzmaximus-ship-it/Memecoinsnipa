"""
trader.py — CLEAN STABLE VERSION (FIXED + DEBUG)

✅ Uses Jupiter Swap API v1 only (api.jup.ag)
✅ Sends Jupiter API key via x-api-key header (fixes 401 when required)
✅ Adds targeted debug logs (no secret values printed)
✅ Correctly signs VersionedTransaction from Jupiter using solders
"""

import os
import time
import base64
import logging
import requests
import base58

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

# ============================================================
# LOGGING
# ============================================================

log = logging.getLogger("trader")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)

# ============================================================
# CONSTANTS
# ============================================================

LAMPORTS_PER_SOL = 1_000_000_000
SOL_MINT = "So11111111111111111111111111111111111111112"

JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"

CONFIRM_TIMEOUT = 60

# ============================================================
# ENV HELPERS
# ============================================================

def is_live_trading_enabled():
    return os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true"


def get_max_sol_per_trade():
    return float(os.environ.get("MAX_SOL_PER_TRADE", "0.03"))


def get_rpc_url():
    return os.environ.get("SOLANA_RPC_URL")


def get_jupiter_api_key():
    # Railway variable must be EXACTLY JUPITER_API_KEY
    return os.environ.get("JUPITER_API_KEY", "").strip()


def jupiter_headers():
    """
    IMPORTANT: Your old code never passed headers to Jupiter.
    If your account/plan requires a key, missing headers => 401.
    """
    key = get_jupiter_api_key()
    has_key = bool(key)
    log.info("Has JUPITER_API_KEY? %s", has_key)

    headers = {"accept": "application/json"}
    if has_key:
        headers["x-api-key"] = key
    return headers


# ============================================================
# WALLET
# ============================================================

def load_wallet():
    pk = os.environ.get("WALLET_PRIVATE_KEY_BASE58")
    if not pk:
        raise EnvironmentError("WALLET_PRIVATE_KEY_BASE58 env var is missing")

    key_bytes = base58.b58decode(pk)
    return Keypair.from_bytes(key_bytes)


# ============================================================
# RPC
# ============================================================

def rpc_call(method, params):
    rpc_url = get_rpc_url()
    if not rpc_url:
        raise EnvironmentError("SOLANA_RPC_URL is missing")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }

    r = requests.post(rpc_url, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()

    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")

    return data


def wait_for_confirmation(signature):
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

def jupiter_swap(input_mint, output_mint, amount, wallet):
    """
    amount is raw integer amount:
      - If input is SOL, amount is lamports
      - If input is token, amount is raw token units (already decimals-adjusted)
    """
    try:
        # ---- STEP 1: QUOTE ----
        quote_params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": "2500",
            "swapMode": "ExactIn",
        }

        log.info("Jupiter quote URL: %s", JUPITER_QUOTE_URL)
        quote_resp = requests.get(
            JUPITER_QUOTE_URL,
            params=quote_params,
            headers=jupiter_headers(),
            timeout=20
        )

        log.info("Jupiter quote status: %s", quote_resp.status_code)

        if quote_resp.status_code == 401:
            # Print a small slice of body for debug (no secrets)
            log.error("Jupiter QUOTE 401 Unauthorized. Body: %s", quote_resp.text[:300])
            return {"success": False, "signature": None, "error": "Jupiter 401 Unauthorized (quote)"}

        quote_resp.raise_for_status()
        quote = quote_resp.json()

        if isinstance(quote, dict) and "error" in quote:
            return {"success": False, "signature": None, "error": quote["error"]}

        # ---- STEP 2: SWAP TX ----
        swap_payload = {
            "quoteResponse": quote,
            "userPublicKey": str(wallet.pubkey()),
            "wrapAndUnwrapSol": True,
            # You can enable these later if you want better landing:
            # "dynamicComputeUnitLimit": True,
            # "computeUnitPriceMicroLamports": 20000,
        }

        log.info("Jupiter swap URL: %s", JUPITER_SWAP_URL)
        swap_resp = requests.post(
            JUPITER_SWAP_URL,
            json=swap_payload,
            headers=jupiter_headers(),
            timeout=20
        )

        log.info("Jupiter swap status: %s", swap_resp.status_code)

        if swap_resp.status_code == 401:
            log.error("Jupiter SWAP 401 Unauthorized. Body: %s", swap_resp.text[:300])
            return {"success": False, "signature": None, "error": "Jupiter 401 Unauthorized (swap)"}

        swap_resp.raise_for_status()
        swap_data = swap_resp.json()

        tx_b64 = swap_data.get("swapTransaction")
        if not tx_b64:
            return {"success": False, "signature": None, "error": f"No swapTransaction returned: {str(swap_data)[:300]}"}

        # ---- STEP 3: SIGN (CORRECT WAY) ----
        tx_bytes = base64.b64decode(tx_b64)
        vt = VersionedTransaction.from_bytes(tx_bytes)

        # Sign the message bytes
        sig = wallet.sign_message(bytes(vt.message))

        # Populate with our signature
        signed_vt = VersionedTransaction.populate(vt.message, [sig])
        raw_tx = bytes(signed_vt)

        # ---- STEP 4: SEND ----
        send_result = rpc_call("sendTransaction", [
            base64.b64encode(raw_tx).decode(),
            {
                "encoding": "base64",
                "skipPreflight": False,
                "maxRetries": 3
            }
        ])

        signature = send_result.get("result")
        if not signature:
            return {"success": False, "signature": None, "error": f"No signature returned: {send_result}"}

        # ---- STEP 5: CONFIRM ----
        confirmed = wait_for_confirmation(signature)

        if confirmed:
            return {"success": True, "signature": signature, "error": None}
        else:
            return {"success": False, "signature": signature, "error": "Transaction not confirmed"}

    except requests.exceptions.HTTPError as e:
        # Include short response text if available
        resp = getattr(e, "response", None)
        body = ""
        if resp is not None:
            body = resp.text[:300]
        return {"success": False, "signature": None, "error": f"{str(e)} | body={body}"}

    except Exception as e:
        return {"success": False, "signature": None, "error": str(e)}


# ============================================================
# BUY / SELL
# ============================================================

def buy_token(mint_address, sol_amount):
    if not is_live_trading_enabled():
        return {"success": False, "signature": None, "error": "Live trading disabled"}

    wallet = load_wallet()
    lamports = int(sol_amount * LAMPORTS_PER_SOL)

    return jupiter_swap(SOL_MINT, mint_address, lamports, wallet)


def sell_token(mint_address):
    if not is_live_trading_enabled():
        return {"success": False, "signature": None, "error": "Live trading disabled"}

    wallet = load_wallet()

    # get token balance (raw)
    token_accounts = rpc_call("getTokenAccountsByOwner", [
        str(wallet.pubkey()),
        {"mint": mint_address},
        {"encoding": "jsonParsed"}
    ])

    accounts = token_accounts["result"]["value"]
    if not accounts:
        return {"success": False, "signature": None, "error": "No tokens to sell"}

    amount = int(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])

    return jupiter_swap(mint_address, SOL_MINT, amount, wallet)


# ============================================================
# WALLET SUMMARY
# ============================================================

def get_wallet_summary():
    try:
        wallet = load_wallet()
        balance_data = rpc_call("getBalance", [str(wallet.pubkey())])
        balance = balance_data["result"]["value"] / LAMPORTS_PER_SOL

        return {
            "address": str(wallet.pubkey()),
            "balance_sol": round(balance, 4),
            "live_trading": is_live_trading_enabled(),
            "max_per_trade": get_max_sol_per_trade(),
            "has_jupiter_key": bool(get_jupiter_api_key()),
        }

    except Exception as e:
        return {"error": str(e)}