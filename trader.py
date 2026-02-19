"""
trader.py - Solana trading module (Jupiter swap) with live buy + partial/full sells.

Exports:
- is_live_trading_enabled()
- get_wallet_summary()
- buy_token(mint_address, sol_amount)
- sell_token(mint_address)              # sells 100%
- sell_token_pct(mint_address, pct)     # sells pct (0..1)
- sell_token_amount_raw(mint_address, raw_amount)  # sells raw amount

Also provides:
- class JupiterTrader (wrapper) for compatibility with scanner.py imports

Env:
- LIVE_TRADING_ENABLED=true/false
- SOLANA_RPC_URL
- SOLANA_PRIVATE_KEY (or WALLET_PRIVATE_KEY(_BASE58))
- MAX_SOL_PER_TRADE
- MIN_SOL_RESERVE
- DEFAULT_SLIPPAGE_BPS
- CONFIRM_TIMEOUT
- JUPITER_QUOTE_URL (optional)
- JUPITER_SWAP_URL  (optional)
"""

import os
import time
import json
import base64
import logging
from typing import Dict, Any, Optional

import requests
import base58

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

log = logging.getLogger("memecoinsnipa.trader")

LAMPORTS_PER_SOL = 1_000_000_000
SOL_MINT = "So11111111111111111111111111111111111111112"

JUPITER_QUOTE_URL = os.environ.get("JUPITER_QUOTE_URL", "https://quote-api.jup.ag/v6/quote")
JUPITER_SWAP_URL = os.environ.get("JUPITER_SWAP_URL", "https://quote-api.jup.ag/v6/swap")

CONFIRM_TIMEOUT = int(os.environ.get("CONFIRM_TIMEOUT", "60"))
DEFAULT_SLIPPAGE_BPS = int(os.environ.get("DEFAULT_SLIPPAGE_BPS", "2500"))
MIN_SOL_RESERVE = float(os.environ.get("MIN_SOL_RESERVE", "0.02"))


def is_live_trading_enabled() -> bool:
    return os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true"


def get_max_sol_per_trade() -> float:
    try:
        return float(os.environ.get("MAX_SOL_PER_TRADE", "0.03"))
    except Exception:
        return 0.03


def get_rpc_url() -> str:
    return os.environ.get("SOLANA_RPC_URL", "").strip()


def _read_private_key_string() -> str:
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
    pk = _read_private_key_string()

    if pk.startswith("["):
        key_bytes = bytes(json.loads(pk))
    else:
        key_bytes = base58.b58decode(pk)

    if len(key_bytes) == 64:
        return Keypair.from_bytes(key_bytes)
    if len(key_bytes) == 32:
        return Keypair.from_seed(key_bytes)

    raise ValueError(f"Unexpected key length: {len(key_bytes)} bytes")


def rpc_call(method: str, params: list) -> Dict[str, Any]:
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


def jupiter_swap(
    input_mint: str,
    output_mint: str,
    amount: int,
    wallet: Keypair,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
) -> Dict[str, Any]:
    try:
        quote_params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": int(amount),
            "slippageBps": int(slippage_bps),
            "swapMode": "ExactIn",
        }

        quote_resp = requests.get(JUPITER_QUOTE_URL, params=quote_params, timeout=20)
        quote_resp.raise_for_status()
        quote = quote_resp.json()

        swap_payload = {
            "quoteResponse": quote,
            "userPublicKey": str(wallet.pubkey()),
            "wrapAndUnwrapSol": True,
        }

        swap_resp = requests.post(JUPITER_SWAP_URL, json=swap_payload, timeout=20)
        swap_resp.raise_for_status()
        swap_data = swap_resp.json()

        tx_b64 = swap_data.get("swapTransaction")
        if not tx_b64:
            return {
                "success": False,
                "signature": None,
                "error": f"No swapTransaction returned: {str(swap_data)[:250]}",
            }

        vt = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        sig = wallet.sign_message(bytes(vt.message))
        signed_vt = VersionedTransaction.populate(vt.message, [sig])
        raw_tx = bytes(signed_vt)

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

        confirmed = wait_for_confirmation(signature)
        if confirmed:
            return {"success": True, "signature": signature, "error": None}

        return {"success": False, "signature": signature, "error": "Transaction not confirmed"}

    except requests.exceptions.HTTPError as e:
        resp = getattr(e, "response", None)
        body = resp.text[:250] if resp is not None else ""
        return {"success": False, "signature": None, "error": f"{str(e)} | body={body}"}
    except Exception as e:
        return {"success": False, "signature": None, "error": str(e)}


def get_wallet_summary() -> Dict[str, Any]:
    try:
        wallet = load_wallet()
        balance_data = rpc_call("getBalance", [str(wallet.pubkey())])
        balance = balance_data["result"]["value"] / LAMPORTS_PER_SOL
        return {
            "address": str(wallet.pubkey()),
            "balance_sol": round(balance, 6),
            "live_trading": is_live_trading_enabled(),
            "max_per_trade": get_max_sol_per_trade(),
        }
    except Exception as e:
        return {
            "error": str(e),
            "live_trading": is_live_trading_enabled(),
            "max_per_trade": get_max_sol_per_trade(),
        }


def get_token_balance_raw_amount(wallet: Keypair, mint_address: str) -> int:
    token_accounts = rpc_call(
        "getTokenAccountsByOwner",
        [str(wallet.pubkey()), {"mint": mint_address}, {"encoding": "jsonParsed"}],
    )
    accounts = token_accounts["result"]["value"]
    if not accounts:
        return 0

    info = accounts[0]["account"]["data"]["parsed"]["info"]
    amt = info["tokenAmount"]["amount"]
    try:
        return int(amt)
    except Exception:
        return 0


def buy_token(mint_address: str, sol_amount: float) -> Dict[str, Any]:
    if not is_live_trading_enabled():
        return {"success": False, "signature": None, "error": "Live trading disabled"}

    ws = get_wallet_summary()
    bal = ws.get("balance_sol")
    if bal is not None and (float(bal) - float(sol_amount)) < MIN_SOL_RESERVE:
        return {
            "success": False,
            "signature": None,
            "error": f"Insufficient SOL reserve. Balance={bal} reserve={MIN_SOL_RESERVE}",
        }

    wallet = load_wallet()
    lamports = int(float(sol_amount) * LAMPORTS_PER_SOL)
    return jupiter_swap(SOL_MINT, mint_address, lamports, wallet)


def sell_token_amount_raw(mint_address: str, raw_amount: int) -> Dict[str, Any]:
    if not is_live_trading_enabled():
        return {"success": False, "signature": None, "error": "Live trading disabled"}

    wallet = load_wallet()
    if raw_amount <= 0:
        return {"success": False, "signature": None, "error": "Invalid sell amount"}

    return jupiter_swap(mint_address, SOL_MINT, int(raw_amount), wallet)


def sell_token_pct(mint_address: str, pct: float) -> Dict[str, Any]:
    if not is_live_trading_enabled():
        return {"success": False, "signature": None, "error": "Live trading disabled"}

    wallet = load_wallet()
    bal = get_token_balance_raw_amount(wallet, mint_address)
    if bal <= 0:
        return {"success": False, "signature": None, "error": "No tokens to sell"}

    pct = max(0.0, min(1.0, float(pct)))
    amt = int(bal * pct)

    if amt <= 0 and bal > 0:
        amt = 1

    return sell_token_amount_raw(mint_address, amt)


def sell_token(mint_address: str) -> Dict[str, Any]:
    return sell_token_pct(mint_address, 1.0)


# ============================================================
# Compatibility wrapper for scanner.py
# ============================================================

class JupiterTrader:
    """
    Thin wrapper so code can do:
        from trader import JupiterTrader
        trader = JupiterTrader()
        trader.buy_token(...)
    while keeping your existing functional API intact.
    """

    def __init__(self):
        # Keep these on the instance in case you want to override per-trader later.
        self.default_slippage_bps = DEFAULT_SLIPPAGE_BPS

    def is_live_trading_enabled(self) -> bool:
        return is_live_trading_enabled()

    def get_wallet_summary(self) -> Dict[str, Any]:
        return get_wallet_summary()

    def buy_token(self, mint_address: str, sol_amount: float, slippage_bps: Optional[int] = None) -> Dict[str, Any]:
        # Note: your current buy_token() does not accept slippage override; this wrapper keeps signature flexible.
        # If you later want slippage overrides, we can thread it through jupiter_swap.
        return buy_token(mint_address, sol_amount)

    def sell_token(self, mint_address: str) -> Dict[str, Any]:
        return sell_token(mint_address)

    def sell_token_pct(self, mint_address: str, pct: float) -> Dict[str, Any]:
        return sell_token_pct(mint_address, pct)

    def sell_token_amount_raw(self, mint_address: str, raw_amount: int) -> Dict[str, Any]:
        return sell_token_amount_raw(mint_address, raw_amount)