"""
trader.py - Solana Trading Module for Memecoinsnipa v2.2

Uses Jupiter V6 API for token swaps on Solana.
Handles buy/sell with safety controls and detailed error reporting.

Changes from v2.1:
- Added pre-flight diagnostics (RPC reachability, wallet load, balance) with detailed errors
- Every failure path returns a specific, descriptive error string
- Added RPC URL validation and empty-URL detection
- Added timeout to all Jupiter API calls
- Improved logging at every step for GitHub Actions artifact debugging
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
RPC_TIMEOUT = 30  # Timeout for RPC calls
JUPITER_QUOTE_TIMEOUT = 15  # Timeout for Jupiter quote API
JUPITER_SWAP_TIMEOUT = 20  # Timeout for Jupiter swap API


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
    url = os.environ.get("SOLANA_RPC_URL", "").strip()
    if not url:
        log.warning("SOLANA_RPC_URL is empty! Falling back to public RPC (unreliable)")
        return "https://api.mainnet-beta.solana.com"
    return url


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
    rpc_url = get_rpc_url()
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    try:
        resp = requests.post(rpc_url, json=payload, timeout=RPC_TIMEOUT)
        if resp.status_code != 200:
            log.error(f"RPC {method} HTTP {resp.status_code}: {resp.text[:200]}")
            return {"error": f"RPC HTTP {resp.status_code}: {resp.text[:200]}"}
        return resp.json()
    except requests.exceptions.Timeout:
        log.error(f"RPC call {method} timed out after {RPC_TIMEOUT}s (url: {rpc_url[:50]}...)")
        return {"error": f"RPC timeout ({RPC_TIMEOUT}s) - check SOLANA_RPC_URL"}
    except requests.exceptions.ConnectionError as e:
        log.error(f"RPC call {method} connection failed: {e}")
        return {"error": f"RPC connection failed - check SOLANA_RPC_URL is valid"}
    except Exception as e:
        log.error(f"RPC call {method} failed: {e}")
        return {"error": str(e)}


def get_sol_balance(wallet: Keypair) -> float:
    result = rpc_call("getBalance", [str(wallet.pubkey()), {"commitment": "confirmed"}])
    if "error" in result:
        log.error(f"Balance check RPC error: {result['error']}")
        return 0.0
    try:
        return result["result"]["value"] / LAMPORTS_PER_SOL
    except (KeyError, TypeError):
        log.error(f"Balance check failed - unexpected response: {json.dumps(result)[:300]}")
        return 0.0


def get_token_balance(wallet: Keypair, mint_address: str) -> tuple:
    """Returns (raw_amount, decimals)."""
    result = rpc_call("getTokenAccountsByOwner", [
        str(wallet.pubkey()),
        {"mint": mint_address},
        {"encoding": "jsonParsed", "commitment": "confirmed"},
    ])
    if "error" in result:
        log.error(f"Token balance RPC error for {mint_address[:12]}...: {result['error']}")
        return 0, 0
    try:
        accounts = result["result"]["value"]
        if accounts:
            info = accounts[0]["account"]["data"]["parsed"]["info"]
            amount = int(info["tokenAmount"]["amount"])
            decimals = int(info["tokenAmount"]["decimals"])
            return amount, decimals
        else:
            log.info(f"No token account found for {mint_address[:12]}...")
    except (KeyError, TypeError, IndexError) as e:
        log.error(f"Token balance parse error for {mint_address[:12]}...: {e}")
    return 0, 0


def send_raw_transaction(tx_bytes: bytes) -> str:
    tx_base64 = base64.b64encode(tx_bytes).decode()
    result = rpc_call("sendTransaction", [
        tx_base64,
        {"encoding": "base64", "skipPreflight": True, "maxRetries": 2},
    ])
    if "error" in result and isinstance(result["error"], dict):
        raise Exception(f"RPC error: {result['error'].get('message', result['error'])}")
    if "error" in result and isinstance(result["error"], str):
        raise Exception(f"RPC error: {result['error']}")
    sig = result.get("result", "")
    if not sig:
        raise Exception(f"No signature in RPC response: {json.dumps(result)[:200]}")
    return sig


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
    log.warning(f"Confirmation timeout ({timeout}s) for tx: {signature}")
    return False


# ============================================================
# PRE-FLIGHT CHECK
# ============================================================

def preflight_check() -> dict:
    """
    Run diagnostics before attempting a trade.
    Returns {'ok': True/False, 'wallet': Keypair|None, 'balance': float, 'errors': [str]}
    """
    errors = []
    wallet = None
    balance = 0.0

    # Check RPC URL
    rpc_url = os.environ.get("SOLANA_RPC_URL", "").strip()
    if not rpc_url:
        errors.append("SOLANA_RPC_URL is empty - using unreliable public RPC")

    # Check wallet
    try:
        wallet = load_wallet()
        log.info(f"Wallet loaded: {str(wallet.pubkey())[:16]}...")
    except EnvironmentError as e:
        errors.append(f"Wallet load failed: {e}")
        return {"ok": False, "wallet": None, "balance": 0.0, "errors": errors}

    # Check RPC reachability via balance
    balance = get_sol_balance(wallet)
    if balance == 0.0:
        errors.append(f"Balance is 0 SOL - RPC may be unreachable or wallet is empty")

    # Check live trading flag
    if not is_live_trading_enabled():
        errors.append("LIVE_TRADING_ENABLED is not 'true'")

    ok = len(errors) == 0 or (len(errors) == 1 and "empty" in errors[0].lower() and balance > 0)
    return {"ok": wallet is not None, "wallet": wallet, "balance": balance, "errors": errors}


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
        log.info(f"Jupiter quote: {input_mint[:12]}... -> {output_mint[:12]}... amount={amount}")
        try:
            quote_resp = requests.get(JUPITER_QUOTE_URL, params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount,
                "slippageBps": DEFAULT_SLIPPAGE_BPS,
            }, timeout=JUPITER_QUOTE_TIMEOUT)
        except requests.exceptions.Timeout:
            return {"success": False, "signature": None,
                    "error": f"Jupiter quote timed out ({JUPITER_QUOTE_TIMEOUT}s)"}
        except requests.exceptions.ConnectionError as e:
            return {"success": False, "signature": None,
                    "error": f"Jupiter quote connection failed: {str(e)[:150]}"}

        if quote_resp.status_code != 200:
            return {"success": False, "signature": None,
                    "error": f"Quote failed (HTTP {quote_resp.status_code}): {quote_resp.text[:200]}"}

        quote = quote_resp.json()

        if "error" in quote:
            return {"success": False, "signature": None,
                    "error": f"Quote error: {quote['error']}"}

        out_amount = quote.get("outAmount", "?")
        log.info(f"Jupiter quote received: outAmount={out_amount}")

        # Step 2: Get swap transaction
        log.info("Requesting Jupiter swap transaction...")
        try:
            swap_resp = requests.post(JUPITER_SWAP_URL, json={
                "quoteResponse": quote,
                "userPublicKey": str(wallet.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto",
            }, timeout=JUPITER_SWAP_TIMEOUT)
        except requests.exceptions.Timeout:
            return {"success": False, "signature": None,
                    "error": f"Jupiter swap API timed out ({JUPITER_SWAP_TIMEOUT}s)"}
        except requests.exceptions.ConnectionError as e:
            return {"success": False, "signature": None,
                    "error": f"Jupiter swap connection failed: {str(e)[:150]}"}

        if swap_resp.status_code != 200:
            return {"success": False, "signature": None,
                    "error": f"Swap API failed (HTTP {swap_resp.status_code}): {swap_resp.text[:200]}"}

        swap_result = swap_resp.json()
        swap_tx_b64 = swap_result.get("swapTransaction")

        if not swap_tx_b64:
            return {"success": False, "signature": None,
                    "error": f"No swap transaction returned from Jupiter. Response: {json.dumps(swap_result)[:200]}"}

        # Step 3: Deserialize and sign
        log.info("Signing transaction...")
        try:
            tx_bytes = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [wallet])
            raw_bytes = bytes(signed_tx)
        except Exception as e:
            return {"success": False, "signature": None,
                    "error": f"Transaction signing failed: {str(e)[:200]}"}

        # Step 4: Send with retries
        for attempt in range(MAX_RETRIES):
            try:
                log.info(f"Sending transaction (attempt {attempt + 1}/{MAX_RETRIES})...")
                signature = send_raw_transaction(raw_bytes)
                if not signature:
                    raise Exception("No signature returned")

                log.info(f"Tx sent: {signature} - waiting for confirmation...")

                if wait_for_confirmation(signature):
                    log.info(f"Tx confirmed: {signature}")
                    return {"success": True, "signature": signature, "error": None}
                else:
                    log.warning(f"Tx not confirmed (attempt {attempt + 1}): {signature}")
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(3)
                        continue
                    return {"success": False, "signature": signature,
                            "error": f"Transaction sent but not confirmed after {CONFIRM_TIMEOUT}s"}

            except Exception as e:
                log.error(f"Send attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2)
                    continue
                return {"success": False, "signature": None, "error": f"Send failed after {MAX_RETRIES} attempts: {str(e)[:200]}"}

        return {"success": False, "signature": None, "error": "Max retries exceeded"}

    except Exception as e:
        log.error(f"Jupiter swap failed: {e}")
        return {"success": False, "signature": None, "error": f"Jupiter swap exception: {str(e)[:200]}"}


# ============================================================
# BUY / SELL
# ============================================================

def buy_token(mint_address: str, sol_amount: float) -> dict:
    """Buy a token with SOL via Jupiter."""
    log.info(f"=== BUY REQUEST: {mint_address[:20]}... for {sol_amount} SOL ===")

    if not is_live_trading_enabled():
        log.info(f"[DRY RUN] Would buy {mint_address[:12]}... with {sol_amount} SOL")
        return {"success": False, "signature": None, "error": "Live trading disabled (dry run)"}

    max_sol = get_max_sol_per_trade()
    if sol_amount > max_sol:
        log.warning(f"Capping trade from {sol_amount} to {max_sol} SOL")
        sol_amount = max_sol

    try:
        # Pre-flight
        pf = preflight_check()
        if pf["errors"]:
            log.warning(f"Pre-flight warnings: {pf['errors']}")
        if pf["wallet"] is None:
            return {"success": False, "signature": None,
                    "error": f"Pre-flight failed: {'; '.join(pf['errors'])}"}

        wallet = pf["wallet"]
        balance = pf["balance"]

        if balance < sol_amount + MIN_SOL_RESERVE:
            return {"success": False, "signature": None,
                    "error": f"Insufficient SOL: {balance:.4f} (need {sol_amount + MIN_SOL_RESERVE:.4f})"}

        lamports = int(sol_amount * LAMPORTS_PER_SOL)
        log.info(f"BUYING {mint_address[:12]}... with {sol_amount} SOL ({lamports} lamports) | wallet balance: {balance:.4f} SOL")

        result = jupiter_swap(SOL_MINT, mint_address, lamports, wallet)

        if result["success"]:
            log.info(f"BUY SUCCESS: {mint_address[:12]}... | tx: {result['signature']}")
        else:
            log.warning(f"BUY FAILED: {mint_address[:12]}... | {result['error']}")

        return result

    except Exception as e:
        error_msg = f"Buy exception: {str(e)[:300]}"
        log.error(error_msg)
        return {"success": False, "signature": None, "error": error_msg}


def sell_token(mint_address: str, percentage: float = 100.0) -> dict:
    """Sell a token for SOL via Jupiter."""
    log.info(f"=== SELL REQUEST: {mint_address[:20]}... ({percentage}%) ===")

    if not is_live_trading_enabled():
        log.info(f"[DRY RUN] Would sell {mint_address[:12]}... ({percentage}%)")
        return {"success": False, "signature": None, "error": "Live trading disabled (dry run)"}

    try:
        # Pre-flight
        pf = preflight_check()
        if pf["errors"]:
            log.warning(f"Pre-flight warnings: {pf['errors']}")
        if pf["wallet"] is None:
            return {"success": False, "signature": None,
                    "error": f"Pre-flight failed: {'; '.join(pf['errors'])}"}

        wallet = pf["wallet"]
        amount, decimals = get_token_balance(wallet, mint_address)

        if amount <= 0:
            return {"success": False, "signature": None,
                    "error": f"No tokens to sell (balance=0 for {mint_address[:16]}...)"}

        sell_amount = int(amount * (percentage / 100.0))
        if sell_amount <= 0:
            return {"success": False, "signature": None,
                    "error": f"Sell amount too small (raw_amount={amount}, pct={percentage}%)"}

        log.info(f"SELLING {sell_amount} of {mint_address[:12]}... ({percentage}%) | decimals={decimals}")

        result = jupiter_swap(mint_address, SOL_MINT, sell_amount, wallet)

        if result["success"]:
            log.info(f"SELL SUCCESS: {mint_address[:12]}... | tx: {result['signature']}")
        else:
            log.warning(f"SELL FAILED: {mint_address[:12]}... | {result['error']}")

        return result

    except Exception as e:
        error_msg = f"Sell exception: {str(e)[:300]}"
        log.error(error_msg)
        return {"success": False, "signature": None, "error": error_msg}


# ============================================================
# WALLET SUMMARY (for Telegram status)
# ============================================================

def get_wallet_summary() -> dict:
    try:
        wallet = load_wallet()
        balance = get_sol_balance(wallet)
        rpc_url = os.environ.get("SOLANA_RPC_URL", "").strip()
        return {
            "address": str(wallet.pubkey()),
            "balance_sol": round(balance, 4),
            "live_trading": is_live_trading_enabled(),
            "max_per_trade": get_max_sol_per_trade(),
            "rpc_configured": bool(rpc_url),
        }
    except Exception as e:
        return {"error": str(e), "live_trading": False}
