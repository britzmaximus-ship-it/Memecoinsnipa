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

LAMPORTS_PER_SOL = 1_000_000_000
SOL_MINT = "So11111111111111111111111111111111111111112"

JUPITER_BASE = "https://api.jup.ag/swap/v1"
QUOTE_URL = f"{JUPITER_BASE}/quote"
SWAP_URL = f"{JUPITER_BASE}/swap"

CONFIRM_TIMEOUT = 180
MAX_RETRIES = 5


# ============================================================
# ENV FLAGS
# ============================================================

def is_live_trading_enabled() -> bool:
    return os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"


def get_max_sol_per_trade() -> float:
    return float(os.getenv("MAX_SOL_PER_TRADE", "0.03"))


def get_rpc_url() -> str:
    return os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


# ============================================================
# WALLET
# ============================================================

def load_wallet() -> Keypair:
    pk = os.getenv("SOLANA_PRIVATE_KEY")
    if not pk:
        raise EnvironmentError("SOLANA_PRIVATE_KEY not set")
    return Keypair.from_bytes(base58.b58decode(pk))


def rpc_call(method: str, params: list) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = requests.post(get_rpc_url(), json=payload, timeout=30)
    return r.json()


def get_sol_balance(wallet: Keypair) -> float:
    result = rpc_call("getBalance", [str(wallet.pubkey())])
    return result.get("result", {}).get("value", 0) / LAMPORTS_PER_SOL


# ============================================================
# WALLET SUMMARY (FIX FOR SCANNER IMPORT)
# ============================================================

def get_wallet_summary():
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
        return {"error": str(e)[:200]}


# ============================================================
# TX SEND + CONFIRM
# ============================================================

def send_raw_transaction(tx_bytes: bytes) -> str:
    tx_b64 = base64.b64encode(tx_bytes).decode()
    result = rpc_call(
        "sendTransaction",
        [tx_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 5}],
    )
    if "error" in result:
        raise Exception(str(result["error"]))
    return result.get("result")


def wait_for_confirmation(signature: str) -> bool:
    start = time.time()
    while time.time() - start < CONFIRM_TIMEOUT:
        result = rpc_call("getSignatureStatuses", [[signature]])
        status = result.get("result", {}).get("value", [None])[0]
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

def jupiter_swap(input_mint: str, output_mint: str, amount: int, wallet: Keypair) -> dict:
    try:
        quote = requests.get(
            QUOTE_URL,
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount,
                "slippageBps": 2500,
            },
            timeout=20,
        ).json()

        swap = requests.post(
            SWAP_URL,
            json={
                "quoteResponse": quote,
                "userPublicKey": str(wallet.pubkey()),
                "wrapAndUnwrapSol": True,
                "prioritizationFeeLamports": 300000,
            },
            timeout=30,
        ).json()

        swap_tx = swap.get("swapTransaction")
        if not swap_tx:
            return {"success": False, "signature": None, "error": "No swapTransaction returned"}

        tx = VersionedTransaction.from_bytes(base64.b64decode(swap_tx))
        signed = VersionedTransaction(tx.message, [wallet])
        raw = bytes(signed)

        for attempt in range(MAX_RETRIES):
            try:
                sig = send_raw_transaction(raw)
                if wait_for_confirmation(sig):
                    return {"success": True, "signature": sig, "error": None}
            except Exception:
                time.sleep(2)

        return {"success": False, "signature": None, "error": "Not confirmed"}

    except Exception as e:
        return {"success": False, "signature": None, "error": str(e)[:200]}


# ============================================================
# BUY / SELL
# ============================================================

def buy_token(mint_address: str, sol_amount: float) -> dict:
    if not is_live_trading_enabled():
        return {"success": False, "signature": None, "error": "Live trading disabled"}

    wallet = load_wallet()
    balance = get_sol_balance(wallet)

    if balance < sol_amount:
        return {"success": False, "signature": None, "error": "Insufficient SOL"}

    lamports = int(sol_amount * LAMPORTS_PER_SOL)
    return jupiter_swap(SOL_MINT, mint_address, lamports, wallet)


def sell_token(mint_address: str) -> dict:
    if not is_live_trading_enabled():
        return {"success": False, "signature": None, "error": "Live trading disabled"}

    wallet = load_wallet()

    result = rpc_call(
        "getTokenAccountsByOwner",
        [
            str(wallet.pubkey()),
            {"mint": mint_address},
            {"encoding": "jsonParsed"},
        ],
    )

    accounts = result.get("result", {}).get("value", [])
    if not accounts:
        return {"success": False, "signature": None, "error": "No tokens to sell"}

    info = accounts[0]["account"]["data"]["parsed"]["info"]
    amount = int(info["tokenAmount"]["amount"])

    return jupiter_swap(mint_address, SOL_MINT, amount, wallet)