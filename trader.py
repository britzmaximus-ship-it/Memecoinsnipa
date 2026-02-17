"""
trader.py - Solana Trading Module for Memecoinsnipa

Uses Jupiter V6 API for token swaps on Solana.
Handles buy/sell with safety controls.
"""

import os
import time
import json
import base64
import logging
import base58
import requests

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.signature import Signature

log = logging.getLogger("trader")

# ============================================================
# CONSTANTS
# ============================================================

LAMPORTS_PER_SOL = 1_000_000_000
SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
DEFAULT_SLIPPAGE_BPS = 2500  # 25% slippage tolerance
MAX_RETRIES = 3
CONFIRM_TIMEOUT = 60
MIN_SOL_RESERVE = 0.01  # Keep this much SOL for fees


# ============================================================
# CONFIGURATION
# ============================================================

def is_live_trading_enabled() -> bool:
    return os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true"


def get_max_sol_per_trade() -> float:
    try:
        return float(os.environ.get("MAX_SOL_PER_TRADE", "0.03"))
    except ValueError:
        return 0.03


def get_rpc_url() -> str:
    return os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


# ============================================================
# WALLET
# ============================================================

def load_wallet() -> Keypair:
    pk = os.environ.get("SOLANA_PRIVATE_KEY", "")
    if not pk:
        raise EnvironmentError("SOLANA_PRIVATE_KEY not set")
    try:
        key_bytes = base58.b58decode(pk)
        return Keypair.from_bytes(key_bytes)
    except Exception as e:
        raise EnvironmentError(f"Invalid SOLANA_PRIVATE_KEY: {e}")


# ============================================================
# RPC HELPERS (using requests for reliability)
# ============================================================

def rpc_call(method: str, params: list) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    try:
        resp = requests.post(get_rpc_url(), json=payload, timeout=30)
        return resp.json()
    except Exception as e:
        log.error(f"RPC call {method} failed: {e}")
        return {"error": str(e)}


def get_sol_balance(wallet: Keypair) -> float:
    result = rpc_call("getBalance", [str(wallet.pubkey()), {"commitment": "confirmed"}])
    try:
        return result["result"]["value"] / LAMPORTS_PER_SOL
    except (KeyError, TypeError):
        log.error(f"Balance check failed: {result}")
        return 0.0


def get_token_balance(wallet: Keypair, mint_address: str) -> tuple:
    """Returns (raw_amount, decimals)."""
    result = rpc_call("getTokenAccountsByOwner", [
        str(wallet.pubkey()),
        {"mint": mint_address},
        {"encoding": "jsonParsed", "commitment": "confirmed"},
    ])
    try:
        accounts = result["result"]["value"]
        if accounts:
            info = accounts[0]["account"]["data"]["parsed"]["info"]
            amount = int(info["tokenAmount"]["amount"])
            decimals = int(info["tokenAmount"]["decimals"])
            return amount, decimals
    except (KeyError, TypeError, IndexError):
        pass
    return 0, 0


def send_raw_transaction(tx_bytes: bytes) -> str:
    tx_base64 = base64.b64encode(tx_bytes).decode()
    result = rpc_call("sendTransaction", [
        tx_base64,
        {"encoding": "base64", "skipPreflight": True, "maxRetries": 2},
    ])
    if "error" in result and isinstance(result["error"], dict):
        raise Exception(f"RPC error: {result['error'].get('message', result['error'])}")
    return result.get("result", "")


def wait_for_confirmation(signature: str, timeout: int = CONFIRM_TIMEOUT) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        result = rpc_call("getSignatureStatuses", [[signature]])
        try:
            statuses = result["result"]["value"]
            if statuses and statuses[0]:
                status = statuses[0]
                if status.get("err"):
                    log.error(f"Transaction failed on-chain: {status['err']}")
                    return False
                conf = status.get("confirmationStatus", "")
                if conf in ("confirmed", "finalized"):
                    return True
        except (KeyError, TypeError):
            pass
        time.sleep(2)
    return False


# ============================================================
# JUPITER SWAP
# ============================================================

def jupiter_swap(input_mint: str, output_mint: str, amount: int, wallet: Keypair) -> dict:
    """
    Execute a swap via Jupiter V6 API.
    Returns {'success': bool, 'signature': str|None, 'error': str|None}
    """
    try:
        # Step 1: Get quote
        quote_resp = requests.get(JUPITER_QUOTE_URL, params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount,
            "slippageBps": DEFAULT_SLIPPAGE_BPS,
        }, timeout=10)

        if quote_resp.status_code != 200:
            return {"success": False, "signature": None,
                    "error": f"Quote failed (HTTP {quote_resp.status_code}): {quote_resp.text[:200]}"}

        quote = quote_resp.json()

        if "error" in quote:
            return {"success": False, "signature": None,
                    "error": f"Quote error: {quote['error']}"}

        # Step 2: Get swap transaction
        swap_resp = requests.post(JUPITER_SWAP_URL, json={
            "quoteResponse": quote,
            "userPublicKey": str(wallet.pubkey()),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        }, timeout=15)

        if swap_resp.status_code != 200:
            return {"success": False, "signature": None,
                    "error": f"Swap API failed (HTTP {swap_resp.status_code}): {swap_resp.text[:200]}"}

        swap_result = swap_resp.json()
        swap_tx_b64 = swap_result.get("swapTransaction")

        if not swap_tx_b64:
            return {"success": False, "signature": None,
                    "error": "No swap transaction returned from Jupiter"}

        # Step 3: Deserialize and sign
        tx_bytes = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(tx_bytes)
        signed_tx = VersionedTransaction(tx.message, [wallet])
        raw_bytes = bytes(signed_tx)

        # Step 4: Send with retries
        for attempt in range(MAX_RETRIES):
            try:
                signature = send_raw_transaction(raw_bytes)
                if not signature:
                    raise Exception("No signature returned")

                log.info(f"Tx sent: {signature}")

                if wait_for_confirmation(signature):
                    log.info(f"Tx confirmed: {signature}")
                    return {"success": True, "signature": signature, "error": None}
                else:
                    log.warning(f"Tx not confirmed (attempt {attempt + 1}): {signature}")
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(3)
                        continue
                    return {"success": False, "signature": signature,
                            "error": "Transaction sent but not confirmed"}

            except Exception as e:
                log.error(f"Send attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2)
                    continue
                return {"success": False, "signature": None, "error": str(e)}

        return {"success": False, "signature": None, "error": "Max retries exceeded"}

    except Exception as e:
        log.error(f"Jupiter swap failed: {e}")
        return {"success": False, "signature": None, "error": str(e)}


# ============================================================
# BUY / SELL
# ============================================================

def buy_token(mint_address: str, sol_amount: float) -> dict:
    """Buy a token with SOL via Jupiter."""
    if not is_live_trading_enabled():
        log.info(f"[DRY RUN] Would buy {mint_address[:12]}... with {sol_amount} SOL")
        return {"success": False, "signature": None, "error": "Live trading disabled (dry run)"}

    max_sol = get_max_sol_per_trade()
    if sol_amount > max_sol:
        log.warning(f"Capping trade from {sol_amount} to {max_sol} SOL")
        sol_amount = max_sol

    try:
        wallet = load_wallet()
        balance = get_sol_balance(wallet)

        if balance < sol_amount + MIN_SOL_RESERVE:
            return {"success": False, "signature": None,
                    "error": f"Insufficient SOL: {balance:.4f} (need {sol_amount + MIN_SOL_RESERVE:.4f})"}

        lamports = int(sol_amount * LAMPORTS_PER_SOL)
        log.info(f"BUYING {mint_address[:12]}... with {sol_amount} SOL ({lamports} lamports)")

        result = jupiter_swap(SOL_MINT, mint_address, lamports, wallet)

        if result["success"]:
            log.info(f"BUY SUCCESS: {mint_address[:12]}... | tx: {result['signature']}")
        else:
            log.warning(f"BUY FAILED: {mint_address[:12]}... | {result['error']}")

        return result

    except Exception as e:
        log.error(f"Buy error: {e}")
        return {"success": False, "signature": None, "error": str(e)}


def sell_token(mint_address: str, percentage: float = 100.0) -> dict:
    """Sell a token for SOL via Jupiter."""
    if not is_live_trading_enabled():
        log.info(f"[DRY RUN] Would sell {mint_address[:12]}... ({percentage}%)")
        return {"success": False, "signature": None, "error": "Live trading disabled (dry run)"}

    try:
        wallet = load_wallet()
        amount, decimals = get_token_balance(wallet, mint_address)

        if amount <= 0:
            return {"success": False, "signature": None,
                    "error": "No tokens to sell"}

        sell_amount = int(amount * (percentage / 100.0))
        if sell_amount <= 0:
            return {"success": False, "signature": None,
                    "error": "Sell amount too small"}

        log.info(f"SELLING {sell_amount} of {mint_address[:12]}... ({percentage}%)")

        result = jupiter_swap(mint_address, SOL_MINT, sell_amount, wallet)

        if result["success"]:
            log.info(f"SELL SUCCESS: {mint_address[:12]}... | tx: {result['signature']}")
        else:
            log.warning(f"SELL FAILED: {mint_address[:12]}... | {result['error']}")

        return result

    except Exception as e:
        log.error(f"Sell error: {e}")
        return {"success": False, "signature": None, "error": str(e)}


# ============================================================
# WALLET SUMMARY (for Telegram status)
# ============================================================

def get_wallet_summary() -> dict:
    try:
        wallet = load_wallet()
        balance = get_sol_balance(wallet)
        return {
            "address": str(wallet.pubkey()),
            "balance_sol": round(balance, 4),
            "live_trading": is_live_trading_enabled(),
            "max_per_trade": get_max_sol_per_trade(),
        }
    except Exception as e:
        return {"error": str(e), "live_trading": False}
