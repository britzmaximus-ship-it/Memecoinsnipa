def sell_token_amount_raw(mint_address: str, raw_amount: int):
    """
    Sells a specific raw token amount back to SOL.
    raw_amount must be in the token's raw units (no decimals conversion needed because we pull raw balance).
    """
    if not is_live_trading_enabled():
        return {"success": False, "signature": None, "error": "Live trading disabled"}

    wallet = load_wallet()
    if raw_amount <= 0:
        return {"success": False, "signature": None, "error": "Invalid sell amount"}

    return jupiter_swap(mint_address, SOL_MINT, int(raw_amount), wallet)


def sell_token_pct(mint_address: str, pct: float):
    """
    Sells pct (0..1) of current token balance for the mint.
    Example: pct=0.5 sells ~50%.
    """
    if not is_live_trading_enabled():
        return {"success": False, "signature": None, "error": "Live trading disabled"}

    wallet = load_wallet()
    bal = get_token_balance_raw_amount(wallet, mint_address)
    if bal <= 0:
        return {"success": False, "signature": None, "error": "No tokens to sell"}

    pct = max(0.0, min(1.0, float(pct)))
    amt = int(bal * pct)

    # If rounding makes it 0, but balance exists, sell at least 1 unit
    if amt <= 0 and bal > 0:
        amt = 1

    return sell_token_amount_raw(mint_address, amt)


def sell_token(mint_address: str):
    """
    Sells full balance (100%).
    """
    return sell_token_pct(mint_address, 1.0)