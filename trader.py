"""
trader.py – Solana Jupiter Swap Module (Production Safe)

Handles:
- Wallet loading (base58 or JSON array)
- 32 vs 64 byte key handling
- Jupiter quote + swap
- VersionedTransaction signing
- Raw transaction submission
"""

import os
import json
import base64
import base58
import requests
import logging

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

log = logging.getLogger("trader")
logging.basicConfig(level=logging.INFO)

LAMPORTS_PER_SOL = 1_000_000_000

JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"


# ============================================================
# WALLET LOADING
# ============================================================

def load_keypair():
    raw_key = (
        os.environ.get("SOLANA_PRIVATE_KEY")
        or os.environ.get("WALLET_PRIVATE_KEY_BASE58")
    )

    if not raw_key:
        raise Exception("No private key found in environment variables.")

    raw_key = raw_key.strip()

    # Try JSON array format first (Solana CLI export)
    if raw_key.startswith("["):
        key_bytes = bytes(json.loads(raw_key))
    else:
        key_bytes = base58.b58decode(raw_key)

    if len(key_bytes) == 64:
        kp = Keypair.from_bytes(key_bytes)
    elif len(key_bytes) == 32:
        kp = Keypair.from_seed(key_bytes)
    else:
        raise Exception(f"Unexpected private key length: {len(key_bytes)}")

    log.info(f"Loaded wallet pubkey: {kp.pubkey()}")
    return kp


# ============================================================
# JUPITER SWAP
# ============================================================

def execute_swap(
    input_mint: str,
    output_mint: str,
    amount_sol: float,
    slippage_bps: int = 2500,
):

    kp = load_keypair()
    user_pubkey = str(kp.pubkey())

    rpc_url = os.environ.get("SOLANA_RPC_URL")
    if not rpc_url:
        raise Exception("SOLANA_RPC_URL not set.")

    jupiter_api_key = os.environ.get("JUPITER_API_KEY")
    if not jupiter_api_key:
        raise Exception("JUPITER_API_KEY not set.")

    amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)

    # 1. GET QUOTE
    quote_params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount_lamports,
        "slippageBps": slippage_bps,
    }

    quote_headers = {
        "x-api-key": jupiter_api_key
    }

    quote_res = requests.get(
        JUPITER_QUOTE_URL,
        params=quote_params,
        headers=quote_headers,
        timeout=15,
    )

    if quote_res.status_code != 200:
        raise Exception(f"Quote failed: {quote_res.text}")

    quote_json = quote_res.json()

    # 2. GET SWAP TX
    swap_payload = {
        "quoteResponse": quote_json,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
    }

    swap_headers = {
        "Content-Type": "application/json",
        "x-api-key": jupiter_api_key,
    }

    swap_res = requests.post(
        JUPITER_SWAP_URL,
        headers=swap_headers,
        json=swap_payload,
        timeout=20,
    )

    if swap_res.status_code != 200:
        raise Exception(f"Swap failed: {swap_res.text}")

    swap_json = swap_res.json()

    if "swapTransaction" not in swap_json:
        raise Exception("No swapTransaction returned from Jupiter.")

    swap_tx_b64 = swap_json["swapTransaction"]

    # 3. DESERIALIZE
    tx_bytes = base64.b64decode(swap_tx_b64)
    versioned_tx = VersionedTransaction.from_bytes(tx_bytes)

    # 4. SIGN
    signed_tx = VersionedTransaction(versioned_tx.message, [kp])
    raw_signed_tx = bytes(signed_tx)

    # 5. SEND TO RPC
    rpc_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendRawTransaction",
        "params": [
            base64.b64encode(raw_signed_tx).decode(),
            {"skipPreflight": False}
        ],
    }

    rpc_res = requests.post(rpc_url, json=rpc_payload, timeout=20)

    if rpc_res.status_code != 200:
        raise Exception(f"RPC send failed: {rpc_res.text}")

    rpc_json = rpc_res.json()

    if "error" in rpc_json:
        raise Exception(f"RPC error: {rpc_json['error']}")

    txid = rpc_json["result"]
    log.info(f"Transaction submitted: {txid}")

    return txid