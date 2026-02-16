import os, re, json, requests, math, random
from datetime import datetime
from collections import Counter

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
USER_ID = os.environ.get("TELEGRAM_USER_ID", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")

TAPI = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ============================================================
# STAGE 1: Quick scan prompt (now with pattern-aware filtering)
# ============================================================
def build_scan_prompt(playbook):
    """Build Stage 1 prompt that's informed by learned strategy rules."""
    base = """You are a sharp Solana memecoin scanner. Analyze the token data and quickly identify
the TOP 3 tokens with the best 2x-10x potential.

For each pick, respond in this EXACT JSON format and nothing else:
```json
[
  {"rank": 1, "name": "TOKEN_NAME", "symbol": "SYMBOL", "contract": "ADDRESS", "entry_price": "current_price_number", "confidence": 7, "reason": "Brief reason"},
  {"rank": 2, "name": "TOKEN_NAME", "symbol": "SYMBOL", "contract": "ADDRESS", "entry_price": "current_price_number", "confidence": 6, "reason": "Brief reason"},
  {"rank": 3, "name": "TOKEN_NAME", "symbol": "SYMBOL", "contract": "ADDRESS", "entry_price": "current_price_number", "confidence": 5, "reason": "Brief reason"}
]
```

Use the ACTUAL current price from the data as entry_price (just the number, no $ sign).
confidence = 1-10 based on how closely the token matches your winning criteria.
"""

    # Inject dynamic strategy rules if they exist
    rules = playbook.get("strategy_rules", [])
    if rules:
        base += "\n--- YOUR LEARNED STRATEGY RULES (follow these, they come from your real results) ---\n"
        for rule in rules[-15:]:
            base += f"- {rule}\n"
        base += "\nPrioritize tokens that match your WINNING rules. Avoid tokens that match LOSING rules.\n"
    else:
        base += """
Default criteria (will be replaced by learned rules once you have data):
- Buy/sell ratio > 1.3 in last hour (whale accumulation)
- Volume spike above average (momentum building)
- Market cap under $5M (room to run 2-10x)
- Strong recent price action but NOT already pumped 500%+ in 24h
- Good liquidity relative to market cap
- NEW PAIRS and PUMP.FUN graduates get bonus points
"""

    # Inject failure patterns to avoid
    avoid_patterns = playbook.get("avoid_conditions", [])
    if avoid_patterns:
        base += "\n--- RED FLAGS (these conditions caused losses - AVOID) ---\n"
        for ap in avoid_patterns[-10:]:
            base += f"- {ap}\n"

    base += "\nSkip: already-pumped tokens, dead volume, MC > $10M\nOnly output the JSON, nothing else."
    return base


# ============================================================
# STAGE 2: Deep research prompt (built dynamically with performance data)
# ============================================================
def build_research_prompt(playbook):
    """Build the deep research prompt with real performance data."""
    prompt = """You are a sharp, street-smart Solana memecoin trading AI that LEARNS FROM REAL RESULTS.
You talk like a real trading partner - casual, direct, hyped when something looks good, honest when it doesn't.

Your personality:
- Talk like you're texting a friend who trades
- Use emojis naturally but don't overdo it
- Be decisive - strong opinions backed by data
- Reference your ACTUAL track record when making calls
- Admit when your past picks were wrong and explain what you learned

IMPORTANT: Only pick coins that realistically have 2x-10x potential.
Skip anything that looks pumped out or has no room to run.
"""

    # Inject REAL performance data
    stats = playbook.get("performance", {})
    total_picks = stats.get("total_picks", 0)
    if total_picks > 0:
        wins = stats.get("wins", 0)
        losses = stats.get("losses", 0)
        avg_return = stats.get("avg_return_pct", 0)
        best = stats.get("best_pick", {})
        worst = stats.get("worst_pick", {})
        win_rate = round((wins / max(total_picks, 1)) * 100, 1)

        prompt += f"""
YOUR REAL TRACK RECORD (from watching your past picks):
- Total picks tracked: {total_picks}
- Wins (went up): {wins} | Losses (went down): {losses}
- Win rate: {win_rate}%
- Average return: {avg_return:+.1f}%
"""
        if best.get("name"):
            prompt += f"- Best pick: {best['name']} ({best.get('return_pct', 0):+.1f}%)\n"
        if worst.get("name"):
            prompt += f"- Worst pick: {worst['name']} ({worst.get('return_pct', 0):+.1f}%)\n"

        prompt += "\nUSE THIS DATA. If certain types of tokens tend to win/lose, adjust your picks accordingly.\n"

    # Inject ROI tier analysis
    roi_tiers = playbook.get("roi_tiers", {})
    if roi_tiers:
        prompt += "\n--- ROI ANALYSIS BY SETUP TYPE ---\n"
        for tier_name, tier_data in roi_tiers.items():
            avg_roi = tier_data.get("avg_roi", 0)
            count = tier_data.get("count", 0)
            prompt += f"- {tier_name}: avg ROI {avg_roi:+.1f}% across {count} picks\n"
        prompt += "PRIORITIZE setup types with the highest historical ROI.\n"

    # Inject winning/losing patterns
    win_patterns = playbook.get("win_patterns", [])
    lose_patterns = playbook.get("lose_patterns", [])

    if win_patterns:
        prompt += "\nPATTERNS THAT LED TO WINS:\n"
        for p in win_patterns[-10:]:
            prompt += f"- {p}\n"

    if lose_patterns:
        prompt += "\nPATTERNS THAT LED TO LOSSES:\n"
        for p in lose_patterns[-10:]:
            prompt += f"- {p}\n"

    # Inject dynamic strategy rules
    rules = playbook.get("strategy_rules", [])
    if rules:
        prompt += "\n--- YOUR STRATEGY RULES (learned from real results, FOLLOW THESE) ---\n"
        for r in rules[-15:]:
            prompt += f"- {r}\n"

    # Inject avoid conditions
    avoid_conditions = playbook.get("avoid_conditions", [])
    if avoid_conditions:
        prompt += "\n--- CONDITIONS TO AVOID (caused losses) ---\n"
        for ac in avoid_conditions[-10:]:
            prompt += f"- {ac}\n"

    # Inject mistake post-mortems
    mistakes = playbook.get("mistake_log", [])
    if mistakes:
        prompt += "\n--- RECENT MISTAKES & LESSONS ---\n"
        for m in mistakes[-5:]:
            prompt += f"- [{m.get('date', '')}] {m.get('token', '')}: {m.get('lesson', '')}\n"

    # Inject learned playbook
    if playbook.get("lessons"):
        prompt += "\n--- YOUR LEARNED PLAYBOOK ---\n"
        for l in playbook["lessons"][-20:]:
            prompt += f"- [{l.get('date','')}] {l.get('note','')}\n"

    # Inject repeat sightings
    recent_tokens = playbook.get("tokens_seen", [])[-50:]
    if recent_tokens:
        token_names = [t["name"] for t in recent_tokens]
        repeats = {n: c for n, c in Counter(token_names).items() if c >= 2}
        if repeats:
            prompt += "\n--- REPEAT SIGHTINGS ---\n"
            for name, count in sorted(repeats.items(), key=lambda x: x[1], reverse=True)[:10]:
                prompt += f"- {name}: seen {count} times\n"

    # Inject paper trade performance if available
    pt_stats = playbook.get("paper_trade_stats", {})
    pt_total = pt_stats.get("total_trades", 0)
    if pt_total > 0:
        pt_wr = pt_stats.get("win_rate", 0)
        pt_avg = pt_stats.get("avg_return_pct", 0)
        prompt += f"""
--- PAPER TRADE PERFORMANCE (simulated real trades) ---
- Paper trades completed: {pt_total}
- Paper trade win rate: {pt_wr}%
- Paper trade avg return: {pt_avg:+.1f}%
USE THIS to validate whether your picks actually make money when traded.\n"""

    prompt += f"""
Scan #{playbook.get('scans', 0) + 1}. You've been learning for {playbook.get('scans', 0)} scans.

For EACH pick provide:

PICK #[number]: [TOKEN NAME] ([SYMBOL])
Contract: [address]

DEEP RESEARCH:
- What the data tells us (reference specific numbers)
- Volume pattern analysis
- Buy pressure analysis (retail or whales?)
- Market cap trajectory - where could this realistically go?
- How new is this token?
- Does this match any of your WINNING or LOSING patterns?
- Similarity score to past winners (how closely does this resemble your best picks?)

TRADE SETUP:
Entry Price: $[specific price]
Stop Loss: $[20-30% below entry]
TP1 (Safe): $[~2x from entry]
TP2 (Mid): $[~3-5x from entry]
TP3 (Moon): $[~5-10x from entry]

Strategy: [quick flip / swing / hold]
Risk Level: LOW / MEDIUM / HIGH / DEGEN
Time Outlook: [specific timeframe]
Confidence: [1-10] (based on similarity to past winners and how well this matches your learned strategy rules)

After all picks:
WHALE WATCH: Unusual whale activity
AVOID LIST: Tokens that look like traps (explain WHY using your learned avoid conditions)
MARKET VIBE: Overall Solana memecoin sentiment

PLAYBOOK UPDATE: (2-3 specific lessons - reference your win/loss data)
STRATEGY RULE UPDATE: Based on your results, what rules should be ADDED, MODIFIED, or REMOVED?
MISTAKE REFLECTION: What recent mistakes did you make? What specific conditions will you watch for to avoid them?
SELF-REFLECTION: What did you learn from your past picks' performance? What will you do differently?

Not financial advice."""

    return prompt


# ============================================================
# TELEGRAM
# ============================================================

def send_msg(text):
    for i in range(0, len(text), 4000):
        chunk = text[i:i+4000]
        try:
            requests.post(f"{TAPI}/sendMessage", json={
                "chat_id": USER_ID, "text": chunk
            }, timeout=10)
        except:
            pass


# ============================================================
# DATA SOURCES
# ============================================================

def fetch_boosted_tokens():
    try:
        r = requests.get("https://api.dexscreener.com/token-boosts/top/v1", timeout=10)
        tokens = r.json()[:30]
        return [
            {"addr": t.get("tokenAddress", ""), "source": "boosted"}
            for t in tokens
            if t.get("chainId") == "solana" and t.get("tokenAddress")
        ]
    except:
        return []


def fetch_latest_profiles():
    try:
        r = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10)
        tokens = r.json()[:30]
        return [
            {"addr": t.get("tokenAddress", ""), "source": "profile"}
            for t in tokens
            if t.get("chainId") == "solana" and t.get("tokenAddress")
        ]
    except:
        return []


def fetch_new_pairs():
    try:
        r = requests.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=10)
        tokens = r.json()[:30]
        return [
            {"addr": t.get("tokenAddress", ""), "source": "new/pumpfun"}
            for t in tokens
            if t.get("chainId") == "solana" and t.get("tokenAddress")
        ]
    except:
        return []


def get_token_price(contract_addr):
    """Get current price for a single token."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/tokens/v1/solana/{contract_addr}",
            timeout=8
        )
        data = r.json()
        pairs = data if isinstance(data, list) else data.get("pairs", [])
        if not pairs:
            return None
        p = sorted(
            [x for x in pairs if x],
            key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0),
            reverse=True
        )[0]
        price = p.get("priceUsd")
        return float(price) if price else None
    except:
        return None


def get_token_full_data(contract_addr):
    """Get full market data for a token (used for detailed trade memory)."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/tokens/v1/solana/{contract_addr}",
            timeout=8
        )
        data = r.json()
        pairs = data if isinstance(data, list) else data.get("pairs", [])
        if not pairs:
            return None
        p = sorted(
            [x for x in pairs if x],
            key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0),
            reverse=True
        )[0]

        mc = float(p.get("marketCap", 0) or 0)
        lq = float(p.get("liquidity", {}).get("usd", 0) or 0)
        vol_1h = float(p.get("volume", {}).get("h1", 0) or 0)
        vol_24h = float(p.get("volume", {}).get("h24", 0) or 0)
        tx_h1 = p.get("txns", {}).get("h1", {})
        buys_1h = int(tx_h1.get("buys", 0) or 0)
        sells_1h = int(tx_h1.get("sells", 0) or 0)
        buy_ratio_1h = round(buys_1h / max(sells_1h, 1), 2)
        price = float(p.get("priceUsd", 0) or 0)
        pc = p.get("priceChange", {})
        dex_id = p.get("dexId", "unknown")

        # Determine age
        pair_created = p.get("pairCreatedAt", "")
        hours_old = None
        if pair_created:
            try:
                created_ts = int(pair_created) / 1000
                hours_old = round((datetime.now().timestamp() - created_ts) / 3600, 1)
            except:
                pass

        # Categorize market cap tier
        if mc < 100000:
            mc_tier = "micro (<100k)"
        elif mc < 500000:
            mc_tier = "small (100k-500k)"
        elif mc < 2000000:
            mc_tier = "mid (500k-2M)"
        elif mc < 5000000:
            mc_tier = "large (2M-5M)"
        else:
            mc_tier = "mega (5M+)"

        # Categorize volume tier
        avg_hourly = vol_24h / 24 if vol_24h > 0 else 0
        vol_spike = round(vol_1h / max(avg_hourly, 1), 2) if avg_hourly > 0 else 0
        if vol_spike >= 5:
            vol_tier = "extreme_spike"
        elif vol_spike >= 3:
            vol_tier = "high_spike"
        elif vol_spike >= 1.5:
            vol_tier = "moderate"
        else:
            vol_tier = "normal"

        # Categorize buy pressure
        if buy_ratio_1h >= 2.0:
            pressure_tier = "heavy_buying"
        elif buy_ratio_1h >= 1.5:
            pressure_tier = "strong_buying"
        elif buy_ratio_1h >= 1.0:
            pressure_tier = "balanced"
        else:
            pressure_tier = "selling_pressure"

        return {
            "price": price,
            "market_cap": mc,
            "mc_tier": mc_tier,
            "liquidity": lq,
            "liq_to_mc_ratio": round(lq / max(mc, 1), 4),
            "volume_1h": vol_1h,
            "volume_24h": vol_24h,
            "vol_spike": vol_spike,
            "vol_tier": vol_tier,
            "buys_1h": buys_1h,
            "sells_1h": sells_1h,
            "buy_ratio_1h": buy_ratio_1h,
            "pressure_tier": pressure_tier,
            "price_change_1h": float(pc.get("h1", 0) or 0),
            "price_change_6h": float(pc.get("h6", 0) or 0),
            "price_change_24h": float(pc.get("h24", 0) or 0),
            "dex": dex_id,
            "hours_old": hours_old
        }
    except:
        return None


def fetch_pair_data(token_list):
    results = []
    seen = set()

    for item in token_list:
        addr = item["addr"]
        source = item["source"]
        if addr in seen or not addr:
            continue
        seen.add(addr)
        if len(results) >= 12:
            break

        try:
            r = requests.get(
                f"https://api.dexscreener.com/tokens/v1/solana/{addr}",
                timeout=8
            )
            data = r.json()
            pairs = data if isinstance(data, list) else data.get("pairs", [])
            if not pairs:
                continue

            p = sorted(
                [x for x in pairs if x],
                key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0),
                reverse=True
            )[0]

            mc = float(p.get("marketCap", 0) or 0)
            if mc < 5000:
                continue

            pc = p.get("priceChange", {})
            vol = p.get("volume", {})
            lq = p.get("liquidity", {})
            tx_h1 = p.get("txns", {}).get("h1", {})
            tx_h6 = p.get("txns", {}).get("h6", {})
            tx_h24 = p.get("txns", {}).get("h24", {})

            buys_1h = int(tx_h1.get("buys", 0) or 0)
            sells_1h = int(tx_h1.get("sells", 0) or 0)
            buys_6h = int(tx_h6.get("buys", 0) or 0)
            sells_6h = int(tx_h6.get("sells", 0) or 0)
            buys_24h = int(tx_h24.get("buys", 0) or 0)
            sells_24h = int(tx_h24.get("sells", 0) or 0)

            ratio_1h = round(buys_1h / max(sells_1h, 1), 2)
            ratio_24h = round(buys_24h / max(sells_24h, 1), 2)

            vol_1h = float(vol.get("h1", 0) or 0)
            vol_24h = float(vol.get("h24", 0) or 0)
            avg_hourly_vol = vol_24h / 24 if vol_24h > 0 else 0
            vol_spike = round(vol_1h / max(avg_hourly_vol, 1), 2) if avg_hourly_vol > 0 else 0

            whale_signals = []
            if ratio_1h >= 2.0:
                whale_signals.append("HEAVY buy pressure")
            elif ratio_1h >= 1.5:
                whale_signals.append("Strong buy pressure")
            if vol_spike >= 3.0:
                whale_signals.append(f"Vol spike {vol_spike}x")
            if mc > 100000 and ratio_1h > 1.3:
                whale_signals.append("Whale accumulation")

            whale_tag = " | WHALE: " + ", ".join(whale_signals) if whale_signals else ""

            dex_id = p.get("dexId", "unknown")
            pair_created = p.get("pairCreatedAt", "")
            is_new = ""
            if pair_created:
                try:
                    created_ts = int(pair_created) / 1000
                    hours_old = (datetime.now().timestamp() - created_ts) / 3600
                    if hours_old < 1:
                        is_new = f" | NEW ({int(hours_old*60)}min old)"
                    elif hours_old < 24:
                        is_new = f" | NEW ({int(hours_old)}h old)"
                    elif hours_old < 72:
                        is_new = f" | RECENT ({int(hours_old/24)}d old)"
                except:
                    pass

            pumpfun_tag = ""
            if dex_id in ("raydium", "orca") and is_new:
                pumpfun_tag = " | LIKELY PUMP.FUN GRADUATE"

            results.append(
                f"Token: {p.get('baseToken',{}).get('name','?')} "
                f"({p.get('baseToken',{}).get('symbol','?')})\n"
                f"Contract: {addr}\n"
                f"DEX: {dex_id}{is_new}{pumpfun_tag} | Found via: {source}\n"
                f"Price: ${p.get('priceUsd','?')}\n"
                f"Price Change >> 5m: {pc.get('m5','?')}% | 1h: {pc.get('h1','?')}% | "
                f"6h: {pc.get('h6','?')}% | 24h: {pc.get('h24','?')}%\n"
                f"Volume >> 1h: ${vol.get('h1','?')} | 6h: ${vol.get('h6','?')} | "
                f"24h: ${vol.get('h24','?')}\n"
                f"Volume Spike: {vol_spike}x vs 24h avg\n"
                f"Txns 1h >> Buys: {buys_1h} | Sells: {sells_1h} | Ratio: {ratio_1h}\n"
                f"Txns 6h >> Buys: {buys_6h} | Sells: {sells_6h}\n"
                f"Txns 24h >> Buys: {buys_24h} | Sells: {sells_24h} | Ratio: {ratio_24h}\n"
                f"Liquidity: ${lq.get('usd','?')} | Market Cap: ${mc:,.0f}\n"
                f"URL: {p.get('url','?')}"
                f"{whale_tag}"
            )
        except:
            continue

    return results


def run_full_scan():
    all_tokens = []
    boosted = fetch_boosted_tokens()
    all_tokens.extend(boosted)
    profiles = fetch_latest_profiles()
    all_tokens.extend(profiles)
    new_pairs = fetch_new_pairs()
    all_tokens.extend(new_pairs)

    seen = set()
    unique = []
    for item in all_tokens:
        if item["addr"] not in seen and item["addr"]:
            seen.add(item["addr"])
            unique.append(item)

    if not unique:
        return [], {"boosted": 0, "profiles": 0, "new_pairs": 0}

    tokens = fetch_pair_data(unique)
    stats = {
        "boosted": len(boosted),
        "profiles": len(profiles),
        "new_pairs": len(new_pairs),
        "unique": len(seen),
        "with_data": len(tokens)
    }
    return tokens, stats


# ============================================================
# TOKEN SAFETY CHECK (RugCheck API)
# ============================================================

def check_token_safety(contract_addr):
    """Check token safety via RugCheck API.
    Returns (is_safe: bool, score: int, risks: list[str], verdict: str)"""
    try:
        r = requests.get(
            f"https://api.rugcheck.xyz/v1/tokens/{contract_addr}/report/summary",
            timeout=10
        )
        if r.status_code != 200:
            # API unavailable â default to safe so we don't block everything
            return True, 0, [], "API unavailable"

        data = r.json()
        score = data.get("score", 0)
        risks = data.get("risks", [])
        rugged = data.get("rugged", False)

        # Parse risk details
        risk_names = []
        danger_count = 0
        critical_flags = []

        for risk in risks:
            level = risk.get("level", "")
            name = risk.get("name", "unknown risk")
            description = risk.get("description", "")
            risk_names.append(f"{name} ({level})")

            if level == "danger" or level == "error":
                danger_count += 1
                # Track the most critical risks
                name_lower = name.lower()
                if "mint" in name_lower and ("not" in name_lower or "enabled" in name_lower):
                    critical_flags.append("MINT_AUTHORITY_ACTIVE")
                if "freeze" in name_lower:
                    critical_flags.append("FREEZE_AUTHORITY_ACTIVE")
                if "rug" in name_lower or "scam" in name_lower:
                    critical_flags.append("FLAGGED_SCAM")
                if "copycat" in name_lower:
                    critical_flags.append("COPYCAT_TOKEN")
                if "top" in name_lower and ("holder" in name_lower or "10" in name_lower):
                    critical_flags.append("CONCENTRATED_HOLDERS")

        # Decision logic
        if rugged:
            return False, score, risk_names, "RUGGED"
        if "FLAGGED_SCAM" in critical_flags:
            return False, score, risk_names, "FLAGGED_SCAM"
        if "MINT_AUTHORITY_ACTIVE" in critical_flags:
            return False, score, risk_names, "MINT_NOT_REVOKED"
        if danger_count >= 3:
            return False, score, risk_names, f"{danger_count}_DANGER_FLAGS"
        if "FREEZE_AUTHORITY_ACTIVE" in critical_flags and danger_count >= 2:
            return False, score, risk_names, "FREEZE+DANGER"

        # Passed safety checks
        verdict = "SAFE" if danger_count == 0 else f"CAUTION ({danger_count} warnings)"
        return True, score, risk_names, verdict

    except Exception:
        # API error â default to safe so we don't block everything
        return True, 0, [], "API error"


# ============================================================
# PLAYBOOK + PERFORMANCE TRACKING + SELF-LEARNING ENGINE
# ============================================================

def load_playbook():
    try:
        with open("playbook.json") as f:
            pb = json.load(f)
            # Ensure all V5 fields exist
            pb.setdefault("strategy_rules", [])
            pb.setdefault("avoid_conditions", [])
            pb.setdefault("mistake_log", [])
            pb.setdefault("roi_tiers", {})
            pb.setdefault("trade_memory", [])
            pb.setdefault("pattern_stats", {
                "by_mc_tier": {},
                "by_vol_tier": {},
                "by_pressure_tier": {},
                "by_age_group": {},
                "by_source": {}
            })
            # === PAPER TRADE FIELDS ===
            pb.setdefault("paper_trades", [])
            pb.setdefault("paper_trade_history", [])
            pb.setdefault("paper_trade_stats", {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0,
                "avg_return_pct": 0,
                "avg_return_x": 0,
                "best_trade": {},
                "worst_trade": {},
                "current_streak": 0,
                "strategy_accuracy_pct": 0
            })
            pb.setdefault("paper_trade_id_counter", 0)
            # === PENDING TRADES (confirmation delay) ===
            pb.setdefault("pending_paper_trades", [])
            # === CONVERSATION STATE ===
            pb.setdefault("last_update_id", 0)
            return pb
    except:
        return {
            "lessons": [],
            "scans": 0,
            "tokens_seen": [],
            "last_scan": None,
            "active_picks": [],
            "pick_history": [],
            "performance": {
                "total_picks": 0,
                "wins": 0,
                "losses": 0,
                "neutral": 0,
                "avg_return_pct": 0,
                "best_pick": {},
                "worst_pick": {}
            },
            "win_patterns": [],
            "lose_patterns": [],
            # V5 NEW FIELDS
            "strategy_rules": [],
            "avoid_conditions": [],
            "mistake_log": [],
            "roi_tiers": {},
            "trade_memory": [],
            "pattern_stats": {
                "by_mc_tier": {},
                "by_vol_tier": {},
                "by_pressure_tier": {},
                "by_age_group": {},
                "by_source": {}
            },
            # === PAPER TRADE FIELDS ===
            "paper_trades": [],
            "paper_trade_history": [],
            "paper_trade_stats": {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0,
                "avg_return_pct": 0,
                "avg_return_x": 0,
                "best_trade": {},
                "worst_trade": {},
                "current_streak": 0,
                "strategy_accuracy_pct": 0
            },
            "paper_trade_id_counter": 0,
            # === PENDING TRADES (confirmation delay) ===
            "pending_paper_trades": [],
            # === CONVERSATION STATE ===
            "last_update_id": 0
        }


def save_playbook(pb):
    pb["last_scan"] = datetime.now().isoformat()[:16]
    pb["scans"] = pb.get("scans", 0) + 1
    pb["tokens_seen"] = pb.get("tokens_seen", [])[-200:]
    pb["pick_history"] = pb.get("pick_history", [])[-100:]
    pb["active_picks"] = pb.get("active_picks", [])[-20:]
    pb["win_patterns"] = pb.get("win_patterns", [])[-30:]
    pb["lose_patterns"] = pb.get("lose_patterns", [])[-30:]
    pb["strategy_rules"] = pb.get("strategy_rules", [])[-20:]
    pb["avoid_conditions"] = pb.get("avoid_conditions", [])[-20:]
    pb["mistake_log"] = pb.get("mistake_log", [])[-20:]
    pb["trade_memory"] = pb.get("trade_memory", [])[-100:]
    pb["lessons"] = pb.get("lessons", [])[-50:]
    # === PAPER TRADE TRIMMING ===
    pb["paper_trades"] = pb.get("paper_trades", [])[:3]  # Max 3 open
    pb["paper_trade_history"] = pb.get("paper_trade_history", [])[-100:]
    pb["pending_paper_trades"] = pb.get("pending_paper_trades", [])[:5]  # Max 5 pending
    with open("playbook.json", "w") as f:
        json.dump(pb, f, indent=2)


def track_tokens(pb, tokens_text):
    seen_list = pb.get("tokens_seen", [])
    for line in tokens_text.split("\n"):
        if line.startswith("Token: "):
            token_name = line.replace("Token: ", "").strip()
            seen_list.append({
                "name": token_name,
                "date": datetime.now().isoformat()[:16]
            })
    pb["tokens_seen"] = seen_list[-200:]


def save_new_picks(pb, stage1_result):
    """Parse AI picks and save them with FULL market snapshot for future analysis."""
    picks = extract_picks_json(stage1_result)
    if not picks:
        return

    active = pb.get("active_picks", [])
    for pick in picks:
        try:
            entry = pick.get("entry_price", "0")
            entry_float = float(str(entry).replace("$", "").replace(",", "").strip())
            if entry_float <= 0:
                continue

            contract = pick.get("contract", "")

            # Get full market snapshot at entry time
            market_data = get_token_full_data(contract) if contract else {}
            if not market_data:
                market_data = {}

            active.append({
                "name": pick.get("name", "Unknown"),
                "symbol": pick.get("symbol", "?"),
                "contract": contract,
                "entry_price": entry_float,
                "confidence": pick.get("confidence", 5),
                "reason": pick.get("reason", ""),
                "picked_at": datetime.now().isoformat()[:16],
                "scans_tracked": 0,
                "peak_price": entry_float,
                "lowest_price": entry_float,
                # V5: Full market snapshot at entry
                "entry_snapshot": {
                    "market_cap": market_data.get("market_cap", 0),
                    "mc_tier": market_data.get("mc_tier", "unknown"),
                    "liquidity": market_data.get("liquidity", 0),
                    "liq_to_mc_ratio": market_data.get("liq_to_mc_ratio", 0),
                    "volume_1h": market_data.get("volume_1h", 0),
                    "volume_24h": market_data.get("volume_24h", 0),
                    "vol_spike": market_data.get("vol_spike", 0),
                    "vol_tier": market_data.get("vol_tier", "unknown"),
                    "buy_ratio_1h": market_data.get("buy_ratio_1h", 0),
                    "pressure_tier": market_data.get("pressure_tier", "unknown"),
                    "price_change_1h": market_data.get("price_change_1h", 0),
                    "price_change_6h": market_data.get("price_change_6h", 0),
                    "price_change_24h": market_data.get("price_change_24h", 0),
                    "dex": market_data.get("dex", "unknown"),
                    "hours_old": market_data.get("hours_old"),
                    "source": pick.get("source", "scan")
                }
            })
        except:
            continue

    pb["active_picks"] = active[-20:]


def update_pattern_stats(pb, pick, return_pct, result_tag):
    """Update statistical pattern tracking based on trade outcomes."""
    stats = pb.get("pattern_stats", {
        "by_mc_tier": {}, "by_vol_tier": {},
        "by_pressure_tier": {}, "by_age_group": {}, "by_source": {}
    })
    snapshot = pick.get("entry_snapshot", {})

    # Helper to update a category
    def update_cat(category_key, tier_value):
        if not tier_value or tier_value == "unknown":
            return
        cat = stats.setdefault(category_key, {})
        tier = cat.setdefault(tier_value, {"total": 0, "wins": 0, "losses": 0, "returns": []})
        tier["total"] += 1
        if return_pct >= 20:
            tier["wins"] += 1
        elif return_pct < -10:
            tier["losses"] += 1
        tier["returns"].append(round(return_pct, 1))
        # Keep last 50 returns per tier
        tier["returns"] = tier["returns"][-50:]

    update_cat("by_mc_tier", snapshot.get("mc_tier"))
    update_cat("by_vol_tier", snapshot.get("vol_tier"))
    update_cat("by_pressure_tier", snapshot.get("pressure_tier"))

    # Age grouping
    hours_old = snapshot.get("hours_old")
    if hours_old is not None:
        if hours_old < 1:
            age_group = "<1h"
        elif hours_old < 6:
            age_group = "1-6h"
        elif hours_old < 24:
            age_group = "6-24h"
        elif hours_old < 72:
            age_group = "1-3d"
        else:
            age_group = "3d+"
        update_cat("by_age_group", age_group)

    update_cat("by_source", snapshot.get("source", "scan"))

    pb["pattern_stats"] = stats


def save_trade_memory(pb, pick, current_price, return_pct, result_tag):
    """Save detailed trade record for pattern analysis."""
    snapshot = pick.get("entry_snapshot", {})
    memory = pb.get("trade_memory", [])

    record = {
        "name": pick.get("name", "?"),
        "symbol": pick.get("symbol", "?"),
        "contract": pick.get("contract", ""),
        "entry_price": pick.get("entry_price", 0),
        "exit_price": current_price,
        "return_pct": round(return_pct, 1),
        "peak_return_pct": round(((pick.get("peak_price", 0) - pick.get("entry_price", 1)) / max(pick.get("entry_price", 1), 0.0000001)) * 100, 1),
        "result": result_tag,
        "confidence_at_pick": pick.get("confidence", 5),
        "reason": pick.get("reason", ""),
        "picked_at": pick.get("picked_at", ""),
        "closed_at": datetime.now().isoformat()[:16],
        "scans_held": pick.get("scans_tracked", 0),
        # Full entry conditions for pattern matching
        "entry_market_cap": snapshot.get("market_cap", 0),
        "entry_mc_tier": snapshot.get("mc_tier", "unknown"),
        "entry_liquidity": snapshot.get("liquidity", 0),
        "entry_liq_ratio": snapshot.get("liq_to_mc_ratio", 0),
        "entry_vol_spike": snapshot.get("vol_spike", 0),
        "entry_vol_tier": snapshot.get("vol_tier", "unknown"),
        "entry_buy_ratio": snapshot.get("buy_ratio_1h", 0),
        "entry_pressure": snapshot.get("pressure_tier", "unknown"),
        "entry_price_change_1h": snapshot.get("price_change_1h", 0),
        "entry_age_hours": snapshot.get("hours_old"),
        "entry_dex": snapshot.get("dex", "unknown")
    }
    memory.append(record)
    pb["trade_memory"] = memory[-100:]


def detect_mistakes(pb, pick, return_pct):
    """After a loss, analyze what went wrong and log the mistake."""
    if return_pct >= -10:
        return  # Not a significant loss

    snapshot = pick.get("entry_snapshot", {})
    mistakes = pb.get("mistake_log", [])

    # Build mistake analysis
    conditions = []
    if snapshot.get("vol_spike", 0) < 1.5:
        conditions.append("low volume spike at entry")
    if snapshot.get("buy_ratio_1h", 0) < 1.0:
        conditions.append("sellers outnumbered buyers at entry")
    if snapshot.get("price_change_1h", 0) > 100:
        conditions.append("token had already pumped 100%+ in 1h before entry")
    if snapshot.get("price_change_24h", 0) > 500:
        conditions.append("token had already pumped 500%+ in 24h")
    if snapshot.get("liq_to_mc_ratio", 0) < 0.05:
        conditions.append("very low liquidity-to-MC ratio (thin exit)")
    if snapshot.get("market_cap", 0) > 5000000:
        conditions.append("MC was over $5M (limited upside)")

    hours_old = snapshot.get("hours_old")
    if hours_old and hours_old > 72:
        conditions.append("token was already 3+ days old (not fresh)")

    lesson = f"Lost {return_pct:.1f}% on {pick.get('name', '?')}"
    if conditions:
        lesson += f". Warning signs: {', '.join(conditions)}"
    else:
        lesson += ". No obvious warning signs at entry - may be random market conditions"

    mistakes.append({
        "date": datetime.now().isoformat()[:10],
        "token": pick.get("name", "?"),
        "return_pct": round(return_pct, 1),
        "confidence_was": pick.get("confidence", "?"),
        "lesson": lesson,
        "conditions": conditions
    })
    pb["mistake_log"] = mistakes[-20:]

    # Add to avoid conditions if clear patterns
    avoid = pb.get("avoid_conditions", [])
    if snapshot.get("price_change_1h", 0) > 100:
        rule = f"AVOID tokens already pumped >100% in 1h (lost {return_pct:.1f}% on {pick['name']})"
        if rule not in avoid:
            avoid.append(rule)
    if snapshot.get("buy_ratio_1h", 0) < 1.0:
        rule = f"AVOID tokens with sell pressure (ratio <1.0) (lost {return_pct:.1f}% on {pick['name']})"
        if rule not in avoid:
            avoid.append(rule)
    if snapshot.get("liq_to_mc_ratio", 0) < 0.05:
        rule = f"AVOID tokens with liq/MC ratio <5% (thin liquidity trap, lost on {pick['name']})"
        if rule not in avoid:
            avoid.append(rule)
    pb["avoid_conditions"] = avoid[-20:]


def update_roi_tiers(pb):
    """Analyze trade memory to find which setup types produce the best ROI."""
    memory = pb.get("trade_memory", [])
    if len(memory) < 3:
        return

    tiers = {}

    # Analyze by MC tier
    by_mc = {}
    for trade in memory:
        tier = trade.get("entry_mc_tier", "unknown")
        if tier == "unknown":
            continue
        by_mc.setdefault(tier, []).append(trade["return_pct"])

    for tier, returns in by_mc.items():
        if len(returns) >= 2:
            tiers[f"MC_{tier}"] = {
                "avg_roi": round(sum(returns) / len(returns), 1),
                "count": len(returns),
                "win_rate": round(len([r for r in returns if r >= 20]) / len(returns) * 100, 1)
            }

    # Analyze by volume tier
    by_vol = {}
    for trade in memory:
        tier = trade.get("entry_vol_tier", "unknown")
        if tier == "unknown":
            continue
        by_vol.setdefault(tier, []).append(trade["return_pct"])

    for tier, returns in by_vol.items():
        if len(returns) >= 2:
            tiers[f"Vol_{tier}"] = {
                "avg_roi": round(sum(returns) / len(returns), 1),
                "count": len(returns),
                "win_rate": round(len([r for r in returns if r >= 20]) / len(returns) * 100, 1)
            }

    # Analyze by buy pressure tier
    by_pressure = {}
    for trade in memory:
        tier = trade.get("entry_pressure", "unknown")
        if tier == "unknown":
            continue
        by_pressure.setdefault(tier, []).append(trade["return_pct"])

    for tier, returns in by_pressure.items():
        if len(returns) >= 2:
            tiers[f"Pressure_{tier}"] = {
                "avg_roi": round(sum(returns) / len(returns), 1),
                "count": len(returns),
                "win_rate": round(len([r for r in returns if r >= 20]) / len(returns) * 100, 1)
            }

    # Analyze by age group
    by_age = {}
    for trade in memory:
        hours = trade.get("entry_age_hours")
        if hours is None:
            continue
        if hours < 1:
            group = "<1h"
        elif hours < 6:
            group = "1-6h"
        elif hours < 24:
            group = "6-24h"
        else:
            group = "24h+"
        by_age.setdefault(group, []).append(trade["return_pct"])

    for group, returns in by_age.items():
        if len(returns) >= 2:
            tiers[f"Age_{group}"] = {
                "avg_roi": round(sum(returns) / len(returns), 1),
                "count": len(returns),
                "win_rate": round(len([r for r in returns if r >= 20]) / len(returns) * 100, 1)
            }

    pb["roi_tiers"] = tiers


def generate_strategy_rules(pb):
    """Auto-generate strategy rules from trade data using AI."""
    memory = pb.get("trade_memory", [])
    if len(memory) < 5:
        return  # Need enough data

    stats = pb.get("pattern_stats", {})
    roi_tiers = pb.get("roi_tiers", {})

    # Build a concise data summary for the AI
    summary = "TRADE HISTORY SUMMARY:\n"
    summary += f"Total trades: {len(memory)}\n"

    wins = [t for t in memory if t["return_pct"] >= 20]
    losses = [t for t in memory if t["return_pct"] < -10]

    if wins:
        summary += f"\nWINNING TRADES ({len(wins)}):\n"
        for w in wins[-10:]:
            summary += (
                f"- {w['name']}: +{w['return_pct']}% | MC: ${w.get('entry_market_cap',0):,.0f} "
                f"({w.get('entry_mc_tier','?')}) | Vol spike: {w.get('entry_vol_spike',0)}x | "
                f"Buy ratio: {w.get('entry_buy_ratio',0)} | Age: {w.get('entry_age_hours','?')}h | "
                f"1h change at entry: {w.get('entry_price_change_1h',0)}%\n"
            )

    if losses:
        summary += f"\nLOSING TRADES ({len(losses)}):\n"
        for l in losses[-10:]:
            summary += (
                f"- {l['name']}: {l['return_pct']}% | MC: ${l.get('entry_market_cap',0):,.0f} "
                f"({l.get('entry_mc_tier','?')}) | Vol spike: {l.get('entry_vol_spike',0)}x | "
                f"Buy ratio: {l.get('entry_buy_ratio',0)} | Age: {l.get('entry_age_hours','?')}h | "
                f"1h change at entry: {l.get('entry_price_change_1h',0)}%\n"
            )

    if roi_tiers:
        summary += "\nROI BY CATEGORY:\n"
        sorted_tiers = sorted(roi_tiers.items(), key=lambda x: x[1]["avg_roi"], reverse=True)
        for name, data in sorted_tiers:
            summary += f"- {name}: avg ROI {data['avg_roi']:+.1f}%, win rate {data['win_rate']}% ({data['count']} trades)\n"

    system = """You are a quantitative trading analyst. Based on the trade history data below,
generate exactly 5-8 SPECIFIC, ACTIONABLE strategy rules that this scanner should follow.

Each rule should be data-backed. Format:
RULE: [specific rule with numbers]

Examples of good rules:
RULE: Prioritize tokens with MC 500k-2M - these averaged +45% ROI vs +12% for larger caps
RULE: Require buy ratio >= 1.5 in 1h - trades with ratio <1.3 averaged -15%
RULE: Avoid tokens already up >200% in 24h - 4/5 of these lost money
RULE: Best entry is tokens 1-6h old with vol spike >3x

Only output RULE: lines, nothing else."""

    result = call_groq(system, summary, temperature=0.3)
    if not result:
        return

    # Parse rules
    new_rules = []
    for line in result.split("\n"):
        line = line.strip()
        if line.startswith("RULE:"):
            rule_text = line[5:].strip()
            if rule_text:
                new_rules.append(rule_text)

    if new_rules:
        pb["strategy_rules"] = new_rules[:10]


def check_past_picks(pb):
    """Check current prices of all active picks and update performance with full trade memory."""
    active = pb.get("active_picks", [])
    if not active:
        return None

    still_active = []
    report_lines = []
    performance = pb.get("performance", {
        "total_picks": 0, "wins": 0, "losses": 0, "neutral": 0,
        "avg_return_pct": 0, "best_pick": {}, "worst_pick": {}
    })
    history = pb.get("pick_history", [])

    for pick in active:
        contract = pick.get("contract", "")
        if not contract:
            continue

        current_price = get_token_price(contract)
        if current_price is None:
            pick["scans_tracked"] = pick.get("scans_tracked", 0) + 1
            if pick["scans_tracked"] >= 12:
                return_pct = -100.0
                result_tag = "DEAD/RUGGED"

                # V5: Save to trade memory
                save_trade_memory(pb, pick, 0, return_pct, result_tag)
                update_pattern_stats(pb, pick, return_pct, result_tag)
                detect_mistakes(pb, pick, return_pct)

                result = {
                    **pick,
                    "final_price": 0,
                    "return_pct": return_pct,
                    "result": result_tag,
                    "closed_at": datetime.now().isoformat()[:16]
                }
                history.append(result)
                performance["total_picks"] += 1
                performance["losses"] += 1

                pb["lose_patterns"].append(
                    f"{pick['name']}: Token went dead/unreachable after {pick['scans_tracked']} scans. "
                    f"Reason picked: {pick.get('reason', '?')}"
                )
                report_lines.append(f"\U0001f480 {pick['name']} - DEAD/RUGGED (-100%)")
            else:
                still_active.append(pick)
            continue

        entry_price = pick.get("entry_price", 0)
        if entry_price <= 0:
            still_active.append(pick)
            continue

        return_pct = round(((current_price - entry_price) / entry_price) * 100, 1)
        pick["scans_tracked"] = pick.get("scans_tracked", 0) + 1

        # Track peak and lowest
        if current_price > pick.get("peak_price", 0):
            pick["peak_price"] = current_price
        if current_price < pick.get("lowest_price", float('inf')):
            pick["lowest_price"] = current_price

        peak_return = round(((pick["peak_price"] - entry_price) / entry_price) * 100, 1)

        # Check if pick should be closed (after ~3 hours / 12 scans at 15min)
        if pick["scans_tracked"] >= 12:
            result_tag = ""
            if return_pct >= 100:
                result_tag = "BIG WIN (2x+)"
                performance["wins"] = performance.get("wins", 0) + 1
                pb.setdefault("win_patterns", []).append(
                    f"{pick['name']}: +{return_pct}% in {pick['scans_tracked']} scans. "
                    f"Peak was +{peak_return}%. Reason: {pick.get('reason', '?')}"
                )
            elif return_pct >= 20:
                result_tag = "SMALL WIN"
                performance["wins"] = performance.get("wins", 0) + 1
                pb.setdefault("win_patterns", []).append(
                    f"{pick['name']}: +{return_pct}% (small win). Reason: {pick.get('reason', '?')}"
                )
            elif return_pct >= -10:
                result_tag = "NEUTRAL"
                performance["neutral"] = performance.get("neutral", 0) + 1
            else:
                result_tag = "LOSS"
                performance["losses"] = performance.get("losses", 0) + 1
                pb.setdefault("lose_patterns", []).append(
                    f"{pick['name']}: {return_pct}% loss. Peak was +{peak_return}%. "
                    f"Reason picked: {pick.get('reason', '?')}"
                )

            performance["total_picks"] = performance.get("total_picks", 0) + 1

            # V5: Save to trade memory with full details
            save_trade_memory(pb, pick, current_price, return_pct, result_tag)
            update_pattern_stats(pb, pick, return_pct, result_tag)

            # V5: Detect mistakes on losses
            if return_pct < -10:
                detect_mistakes(pb, pick, return_pct)

            # Update best/worst
            best = performance.get("best_pick", {})
            if not best or return_pct > best.get("return_pct", -999):
                performance["best_pick"] = {"name": pick["name"], "return_pct": return_pct}

            worst = performance.get("worst_pick", {})
            if not worst or return_pct < worst.get("return_pct", 999):
                performance["worst_pick"] = {"name": pick["name"], "return_pct": return_pct}

            # Update average return
            total = performance["total_picks"]
            old_avg = performance.get("avg_return_pct", 0)
            performance["avg_return_pct"] = round(
                ((old_avg * (total - 1)) + return_pct) / total, 1
            )

            result = {
                **pick,
                "final_price": current_price,
                "return_pct": return_pct,
                "peak_return_pct": peak_return,
                "result": result_tag,
                "closed_at": datetime.now().isoformat()[:16]
            }
            history.append(result)

            emoji = "\U0001f7e2" if return_pct > 20 else "\U0001f534" if return_pct < -10 else "\u26aa"
            conf = pick.get("confidence", "?")
            report_lines.append(
                f"{emoji} {pick['name']} ({pick['symbol']}): {return_pct:+.1f}% "
                f"(peak: +{peak_return}%) | Conf was: {conf}/10 - {result_tag}"
            )
        else:
            # Still tracking
            emoji = "\U0001f4c8" if return_pct > 0 else "\U0001f4c9"
            report_lines.append(
                f"{emoji} {pick['name']} ({pick['symbol']}): {return_pct:+.1f}% "
                f"(peak: +{peak_return}%) - tracking ({pick['scans_tracked']}/12)"
            )
            still_active.append(pick)

    pb["active_picks"] = still_active
    pb["pick_history"] = history[-100:]
    pb["performance"] = performance

    if report_lines:
        total = performance.get("total_picks", 0)
        wins = performance.get("wins", 0)
        win_rate = round((wins / max(total, 1)) * 100, 1) if total > 0 else 0
        avg_ret = performance.get("avg_return_pct", 0)

        # V5: Add ROI tier summary
        roi_summary = ""
        roi_tiers = pb.get("roi_tiers", {})
        if roi_tiers:
            best_tier = max(roi_tiers.items(), key=lambda x: x[1]["avg_roi"])
            worst_tier = min(roi_tiers.items(), key=lambda x: x[1]["avg_roi"])
            roi_summary = (
                f"\nBest setup type: {best_tier[0]} (avg {best_tier[1]['avg_roi']:+.1f}%)"
                f"\nWorst setup type: {worst_tier[0]} (avg {worst_tier[1]['avg_roi']:+.1f}%)"
            )

        # V5: Add mistake count
        mistakes = pb.get("mistake_log", [])
        rules = pb.get("strategy_rules", [])

        header = (
            f"\U0001f4cb PICK TRACKER UPDATE\n"
            f"{'='*35}\n"
            f"Active picks: {len(still_active)} | Completed: {total}\n"
            f"Win rate: {win_rate}% | Avg return: {avg_ret:+.1f}%\n"
            f"Strategy rules: {len(rules)} | Mistakes logged: {len(mistakes)}"
            f"{roi_summary}\n"
            f"{'='*35}\n\n"
        )
        return header + "\n".join(report_lines)
    return None


# ============================================================
# AUTO PAPER TRADE ENGINE
# ============================================================

def parse_price_value(text):
    """Extract a numeric price value from a line of text."""
    # Remove commas from numbers like $334,808
    cleaned = text.replace(",", "")
    # Match price patterns: $0.0003348, $1.073e-05, 0.0003348, 1.073e-05
    patterns = [
        r'\$\s*([0-9]*\.?[0-9]+(?:[eE][+-]?\d+)?)',   # With $ sign
        r'([0-9]*\.?[0-9]+(?:[eE][+-]?\d+)?)',          # Plain number
    ]
    for pat in patterns:
        m = re.search(pat, cleaned)
        if m:
            try:
                val = float(m.group(1))
                if val > 0:
                    return val
            except:
                continue
    return None


def parse_trade_setups(research_text):
    """Parse trade setups (SL, TP1, TP2, TP3) from Stage 2 research output.
    Returns dict keyed by symbol: {symbol: {stop_loss, tp1, tp2, tp3}}"""
    setups = {}
    current_symbol = None

    for line in research_text.split('\n'):
        line_stripped = line.strip()

        # Detect pick headers: "PICK #1: TOKEN_NAME (SYMBOL)" or similar
        pick_match = re.search(
            r'PICK\s*#?\s*\d+\s*[:\-]\s*(.+?)\s*\((\w+)\)',
            line_stripped, re.IGNORECASE
        )
        if pick_match:
            current_symbol = pick_match.group(2).upper()
            setups.setdefault(current_symbol, {})
            continue

        if not current_symbol:
            continue

        line_lower = line_stripped.lower()
        price = parse_price_value(line_stripped)

        if price and price > 0:
            if 'stop loss' in line_lower or 'stop-loss' in line_lower or 'sl:' in line_lower:
                setups[current_symbol]['stop_loss'] = price
            elif 'tp3' in line_lower or 'moon' in line_lower:
                setups[current_symbol]['tp3'] = price
            elif 'tp2' in line_lower or 'mid' in line_lower:
                setups[current_symbol]['tp2'] = price
            elif 'tp1' in line_lower or 'safe' in line_lower:
                setups[current_symbol]['tp1'] = price

    return setups


def create_paper_trade(pb, pick, trade_setup, scan_num):
    """Create a single paper trade entry with simulated slippage."""
    entry_price = pick.get("entry_price", 0)
    if entry_price <= 0:
        return None

    # Simulated slippage 0.5-1% (buying slightly higher)
    slippage_pct = round(random.uniform(0.5, 1.0), 2)
    entry_with_slippage = entry_price * (1 + slippage_pct / 100)

    # Get SL/TP from parsed research, or use defaults
    stop_loss = trade_setup.get('stop_loss', entry_with_slippage * 0.75)
    tp1 = trade_setup.get('tp1', entry_with_slippage * 2.0)
    tp2 = trade_setup.get('tp2', entry_with_slippage * 3.5)
    tp3 = trade_setup.get('tp3', entry_with_slippage * 7.0)

    # Generate trade ID
    pb["paper_trade_id_counter"] = pb.get("paper_trade_id_counter", 0) + 1
    trade_id = f"PT-{scan_num}-{pb['paper_trade_id_counter']}"

    # Get full market snapshot at entry
    contract = pick.get("contract", "")
    market_data = get_token_full_data(contract) if contract else {}
    if not market_data:
        market_data = {}

    now = datetime.now().isoformat()[:16]

    trade = {
        "trade_id": trade_id,
        "token_name": pick.get("name", "Unknown"),
        "symbol": pick.get("symbol", "?"),
        "contract": contract,
        "entry_price": round(entry_with_slippage, 10),
        "original_rec_price": entry_price,
        "slippage_pct": slippage_pct,
        "stop_loss": stop_loss,
        "original_stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "tp1_hit": False,
        "tp2_hit": False,
        "status": "OPEN",
        "confidence": pick.get("confidence", 5),
        "reason": pick.get("reason", ""),
        "opened_at": now,
        "last_update": now,
        "peak_price": entry_with_slippage,
        "lowest_price": entry_with_slippage,
        "scans_monitored": 0,
        "updates": [],
        "entry_snapshot": {
            "market_cap": market_data.get("market_cap", 0),
            "mc_tier": market_data.get("mc_tier", "unknown"),
            "liquidity": market_data.get("liquidity", 0),
            "liq_to_mc_ratio": market_data.get("liq_to_mc_ratio", 0),
            "volume_1h": market_data.get("volume_1h", 0),
            "volume_24h": market_data.get("volume_24h", 0),
            "vol_spike": market_data.get("vol_spike", 0),
            "vol_tier": market_data.get("vol_tier", "unknown"),
            "buy_ratio_1h": market_data.get("buy_ratio_1h", 0),
            "pressure_tier": market_data.get("pressure_tier", "unknown"),
            "price_change_1h": market_data.get("price_change_1h", 0),
            "price_change_6h": market_data.get("price_change_6h", 0),
            "price_change_24h": market_data.get("price_change_24h", 0),
            "dex": market_data.get("dex", "unknown"),
            "hours_old": market_data.get("hours_old")
        }
    }

    return trade


def apply_trailing_stop(trade, current_price):
    """Apply adaptive trailing stop loss. Tighter trail at higher profits.
    Modifies trade['stop_loss'] in place. Returns (adjusted: bool, new_sl: float)."""
    entry = trade["entry_price"]
    peak = trade.get("peak_price", entry)
    current_sl = trade["stop_loss"]

    # Only trail if we have a peak above entry
    if peak <= entry:
        return False, current_sl

    peak_return_pct = ((peak - entry) / entry) * 100

    # Adaptive trail distance â tighter as profit grows
    if peak_return_pct >= 400:      # 5x+ from entry
        trail_pct = 0.15            # Trail 15% below peak
    elif peak_return_pct >= 200:    # 3x+ from entry
        trail_pct = 0.18            # Trail 18% below peak
    elif peak_return_pct >= 100:    # 2x+ from entry
        trail_pct = 0.20            # Trail 20% below peak
    elif peak_return_pct >= 50:     # 1.5x+ from entry
        trail_pct = 0.22            # Trail 22% below peak
    else:
        trail_pct = 0.25            # Trail 25% below peak

    trailing_sl = peak * (1 - trail_pct)

    # Never move SL down â only up
    if trailing_sl > current_sl:
        trade["stop_loss"] = trailing_sl
        return True, trailing_sl

    return False, current_sl


def evaluate_trade_action(trade, current_price, current_data):
    """Evaluate what action to take on an open paper trade.
    Returns (action, reason) tuple.
    NOTE: apply_trailing_stop() should be called BEFORE this function."""
    entry = trade["entry_price"]
    sl = trade["stop_loss"]  # May already be updated by trailing stop
    tp1 = trade["tp1"]
    tp2 = trade["tp2"]
    tp3 = trade["tp3"]

    return_pct = ((current_price - entry) / entry) * 100

    # ---- STOP LOSS CHECK (uses trailing SL if updated) ----
    if current_price <= sl:
        return "EXIT", f"Stop loss hit at ${current_price:.10g} (SL: ${sl:.10g})"

    # ---- TAKE PROFIT CHECKS ----
    if current_price >= tp3:
        return "EXIT_TP3", f"TP3 (Moon) reached! ${current_price:.10g} (+{return_pct:.1f}%)"

    if current_price >= tp2 and not trade.get("tp2_hit"):
        return "PARTIAL_TP2", f"TP2 (Mid) reached! ${current_price:.10g} (+{return_pct:.1f}%)"

    if current_price >= tp1 and not trade.get("tp1_hit"):
        return "PARTIAL_TP1", f"TP1 (Safe) reached! ${current_price:.10g} (+{return_pct:.1f}%)"

    # ---- MOMENTUM / WHALE CHECKS (need current market data) ----
    if current_data:
        buy_ratio = current_data.get("buy_ratio_1h", 1.0)
        vol_spike = current_data.get("vol_spike", 0)
        price_change_1h = current_data.get("price_change_1h", 0)
        liquidity = current_data.get("liquidity", 0)
        entry_liq = trade.get("entry_snapshot", {}).get("liquidity", 0)

        # Momentum collapse: heavy selling + sharp price drop
        if buy_ratio < 0.5 and price_change_1h < -30:
            return "EXIT", f"Momentum collapse: buy ratio {buy_ratio}, 1h change {price_change_1h}%"

        # Whale exit: high volume dump
        if buy_ratio < 0.6 and vol_spike > 3.0 and price_change_1h < -20:
            return "EXIT", f"Whale exit signal: ratio {buy_ratio}, vol spike {vol_spike}x, 1h {price_change_1h}%"

        # Liquidity rug: liquidity dropped significantly
        if entry_liq > 0 and liquidity < entry_liq * 0.3:
            return "EXIT", f"Liquidity drain: ${liquidity:,.0f} (was ${entry_liq:,.0f} at entry)"

        # Strong buy signal
        if buy_ratio >= 2.5 and vol_spike >= 2.0 and return_pct > 0:
            return "ADD", f"Strong momentum: buy ratio {buy_ratio}, vol spike {vol_spike}x"

    return "HOLD", f"Monitoring ({return_pct:+.1f}%)"


def close_paper_trade(pb, trade, exit_price, reason, exit_data=None):
    """Close a paper trade and record results."""
    entry = trade["entry_price"]
    return_pct = round(((exit_price - entry) / entry) * 100, 1)
    return_x = round(exit_price / entry, 2)
    peak_return = round(((trade.get("peak_price", entry) - entry) / entry) * 100, 1)

    is_win = return_pct > 0
    result_tag = "WIN" if is_win else "LOSS"

    now = datetime.now().isoformat()[:16]
    opened_at = trade.get("opened_at", now)

    # Calculate duration
    try:
        open_dt = datetime.fromisoformat(opened_at)
        close_dt = datetime.now()
        duration_mins = int((close_dt - open_dt).total_seconds() / 60)
        if duration_mins < 60:
            duration_str = f"{duration_mins}m"
        else:
            hours = duration_mins // 60
            mins = duration_mins % 60
            duration_str = f"{hours}h {mins}m"
    except:
        duration_str = "unknown"

    snapshot = trade.get("entry_snapshot", {})

    # Build closed trade record
    closed_record = {
        "trade_id": trade["trade_id"],
        "token_name": trade["token_name"],
        "symbol": trade["symbol"],
        "contract": trade["contract"],
        "entry_price": entry,
        "exit_price": exit_price,
        "return_pct": return_pct,
        "return_x": return_x,
        "peak_return_pct": peak_return,
        "result": result_tag,
        "reason_closed": reason,
        "confidence_at_entry": trade.get("confidence", 0),
        "reason_entered": trade.get("reason", ""),
        "opened_at": opened_at,
        "closed_at": now,
        "duration": duration_str,
        "scans_monitored": trade.get("scans_monitored", 0),
        "tp1_hit": trade.get("tp1_hit", False),
        "tp2_hit": trade.get("tp2_hit", False),
        "slippage_pct": trade.get("slippage_pct", 0),
        # Entry conditions for analysis
        "entry_vol_spike": snapshot.get("vol_spike", 0),
        "entry_buy_pressure": snapshot.get("buy_ratio_1h", 0),
        "entry_pressure_tier": snapshot.get("pressure_tier", "unknown"),
        "entry_whale_activity": snapshot.get("pressure_tier", "unknown"),
        "entry_market_cap": snapshot.get("market_cap", 0),
        "entry_mc_tier": snapshot.get("mc_tier", "unknown"),
        "entry_liquidity": snapshot.get("liquidity", 0),
        "entry_age_hours": snapshot.get("hours_old"),
        "failure_reason": reason if not is_win else ""
    }

    # Add to history
    history = pb.get("paper_trade_history", [])
    history.append(closed_record)
    pb["paper_trade_history"] = history[-100:]

    # Update stats
    update_paper_trade_stats(pb)

    return closed_record


def update_paper_trade_stats(pb):
    """Recalculate paper trade performance statistics."""
    history = pb.get("paper_trade_history", [])
    if not history:
        return

    total = len(history)
    wins = [t for t in history if t["return_pct"] > 0]
    losses = [t for t in history if t["return_pct"] <= 0]
    all_returns = [t["return_pct"] for t in history]
    all_x = [t.get("return_x", 1.0) for t in history]

    win_count = len(wins)
    loss_count = len(losses)
    win_rate = round((win_count / max(total, 1)) * 100, 1)
    avg_return = round(sum(all_returns) / max(total, 1), 1)
    avg_x = round(sum(all_x) / max(total, 1), 2)

    # Best and worst trades
    best = max(history, key=lambda t: t["return_pct"])
    worst = min(history, key=lambda t: t["return_pct"])

    # Current streak
    streak = 0
    if history:
        last_result = history[-1]["return_pct"] > 0
        for t in reversed(history):
            if (t["return_pct"] > 0) == last_result:
                streak += 1 if last_result else -1
            else:
                break

    # Strategy accuracy: how often high confidence (>=8) picks win
    high_conf = [t for t in history if t.get("confidence_at_entry", 0) >= 8]
    if high_conf:
        hc_wins = len([t for t in high_conf if t["return_pct"] > 0])
        strategy_accuracy = round((hc_wins / len(high_conf)) * 100, 1)
    else:
        strategy_accuracy = 0

    pb["paper_trade_stats"] = {
        "total_trades": total,
        "wins": win_count,
        "losses": loss_count,
        "win_rate": win_rate,
        "avg_return_pct": avg_return,
        "avg_return_x": avg_x,
        "best_trade": {
            "token": best["token_name"],
            "return_pct": best["return_pct"],
            "return_x": best.get("return_x", 0)
        },
        "worst_trade": {
            "token": worst["token_name"],
            "return_pct": worst["return_pct"],
            "return_x": worst.get("return_x", 0)
        },
        "current_streak": streak,
        "strategy_accuracy_pct": strategy_accuracy
    }


def monitor_paper_trades(pb):
    """Monitor all open paper trades. Returns Telegram report text or None."""
    trades = pb.get("paper_trades", [])
    if not trades:
        return None

    still_open = []
    report_lines = []
    closed_lines = []

    for trade in trades:
        contract = trade.get("contract", "")
        if not contract:
            still_open.append(trade)
            continue

        # Get current price and full data
        current_price = get_token_price(contract)
        current_data = get_token_full_data(contract)

        trade["scans_monitored"] = trade.get("scans_monitored", 0) + 1
        trade["last_update"] = datetime.now().isoformat()[:16]

        # Handle unreachable token
        if current_price is None:
            if trade["scans_monitored"] >= 8:
                # Token is dead â close the trade
                closed = close_paper_trade(pb, trade, 0, "Token unreachable/rugged")
                closed_lines.append(
                    f"\U0001f480 {trade['trade_id']} {trade['token_name']} - "
                    f"RUGGED/DEAD (-100%) | Duration: {closed['duration']}"
                )
            else:
                still_open.append(trade)
                report_lines.append(
                    f"\u26a0\ufe0f {trade['trade_id']} {trade['symbol']}: "
                    f"Price unavailable (scan {trade['scans_monitored']}/8)"
                )
            continue

        # Update peak/lowest
        if current_price > trade.get("peak_price", 0):
            trade["peak_price"] = current_price
        if current_price < trade.get("lowest_price", float('inf')):
            trade["lowest_price"] = current_price

        # Apply adaptive trailing stop BEFORE evaluating action
        old_sl = trade["stop_loss"]
        trail_adjusted, new_sl = apply_trailing_stop(trade, current_price)

        # Evaluate what to do (uses updated SL from trailing stop)
        action, reason = evaluate_trade_action(trade, current_price, current_data)
        return_pct = round(((current_price - trade["entry_price"]) / trade["entry_price"]) * 100, 1)

        # Record update
        trade.setdefault("updates", []).append({
            "scan": trade["scans_monitored"],
            "price": current_price,
            "action": action,
            "reason": reason,
            "time": datetime.now().isoformat()[:16]
        })
        # Keep last 20 updates per trade
        trade["updates"] = trade["updates"][-20:]

        if action == "EXIT" or action == "EXIT_TP3":
            # Close the trade
            closed = close_paper_trade(pb, trade, current_price, reason)
            emoji = "\u2705" if closed["return_pct"] > 0 else "\u274c"
            closed_lines.append(
                f"{emoji} {trade['trade_id']} {trade['token_name']} ({trade['symbol']})\n"
                f"   Entry: ${trade['entry_price']:.10g} \u2192 Exit: ${current_price:.10g}\n"
                f"   Return: {closed['return_pct']:+.1f}% ({closed['return_x']}x) | "
                f"Duration: {closed['duration']}\n"
                f"   Reason: {reason}"
            )

        elif action == "PARTIAL_TP1":
            trade["tp1_hit"] = True
            # Move SL to breakeven
            trade["stop_loss"] = trade["entry_price"]
            still_open.append(trade)
            report_lines.append(
                f"\U0001f4b0 {trade['trade_id']} {trade['symbol']}: TP1 HIT! "
                f"{return_pct:+.1f}% | SL moved to breakeven\n"
                f"   {reason}"
            )

        elif action == "PARTIAL_TP2":
            trade["tp2_hit"] = True
            # Move SL to TP1
            trade["stop_loss"] = trade["tp1"]
            still_open.append(trade)
            report_lines.append(
                f"\U0001f4b0\U0001f4b0 {trade['trade_id']} {trade['symbol']}: TP2 HIT! "
                f"{return_pct:+.1f}% | SL moved to TP1\n"
                f"   {reason}"
            )

        elif action == "MOVE_SL":
            # Trailing stop already handled above â just report
            still_open.append(trade)
            report_lines.append(
                f"\U0001f6e1\ufe0f {trade['trade_id']} {trade['symbol']}: "
                f"{return_pct:+.1f}% | SL trailed to ${trade['stop_loss']:.10g}\n"
                f"   {reason}"
            )

        elif action == "ADD":
            still_open.append(trade)
            report_lines.append(
                f"\U0001f7e2 {trade['trade_id']} {trade['symbol']}: "
                f"{return_pct:+.1f}% | STRONG SIGNAL\n"
                f"   {reason}"
            )

        else:  # HOLD
            still_open.append(trade)
            emoji = "\U0001f4c8" if return_pct > 0 else "\U0001f4c9" if return_pct < 0 else "\u2796"
            peak_ret = round(((trade["peak_price"] - trade["entry_price"]) / trade["entry_price"]) * 100, 1)

            # Build status indicators
            status_parts = []
            if trade.get("tp1_hit"):
                status_parts.append("TP1\u2705")
            if trade.get("tp2_hit"):
                status_parts.append("TP2\u2705")
            status_tag = " | ".join(status_parts) if status_parts else ""

            # Show current market conditions if available
            market_info = ""
            if current_data:
                br = current_data.get("buy_ratio_1h", 0)
                vs = current_data.get("vol_spike", 0)
                market_info = f" | Buy: {br}x | Vol: {vs}x"

            # Show trailing stop adjustment
            trail_info = ""
            if trail_adjusted:
                trail_info = f" | SL\u2191${new_sl:.10g}"

            report_lines.append(
                f"{emoji} {trade['trade_id']} {trade['symbol']}: "
                f"{return_pct:+.1f}% (peak: +{peak_ret}%) | "
                f"HOLD{' | ' + status_tag if status_tag else ''}"
                f"{market_info}{trail_info}"
            )

    pb["paper_trades"] = still_open

    # Build full report
    if not report_lines and not closed_lines:
        return None

    pt_stats = pb.get("paper_trade_stats", {})
    total_closed = pt_stats.get("total_trades", 0)
    win_rate = pt_stats.get("win_rate", 0)
    avg_ret = pt_stats.get("avg_return_pct", 0)
    streak = pt_stats.get("current_streak", 0)
    streak_str = f"+{streak}W" if streak > 0 else f"{streak}L" if streak < 0 else "0"

    header = (
        f"\U0001f4b5 PAPER TRADE MONITOR\n"
        f"{'='*35}\n"
        f"Open: {len(still_open)}/3 | Closed: {total_closed} | "
        f"Win rate: {win_rate}%\n"
        f"Avg return: {avg_ret:+.1f}% | Streak: {streak_str}\n"
        f"{'='*35}\n"
    )

    sections = []
    if closed_lines:
        sections.append("\n\U0001f4cb CLOSED TRADES:\n" + "\n".join(closed_lines))
    if report_lines:
        sections.append("\n\U0001f50d OPEN POSITIONS:\n" + "\n".join(report_lines))

    return header + "\n".join(sections)


def queue_pending_paper_trades(pb, stage1_result, research_text, scan_num):
    """Queue high-confidence picks as PENDING paper trades.
    They'll be confirmed (or rejected) on the next scan cycle.
    Safety check via RugCheck happens here â unsafe tokens are blocked immediately."""
    open_trades = pb.get("paper_trades", [])
    pending = pb.get("pending_paper_trades", [])

    # Check max open trades limit (open + pending combined)
    total_slots_used = len(open_trades) + len(pending)
    if total_slots_used >= 3:
        return [], []

    # Get picks from Stage 1
    picks = extract_picks_json(stage1_result)
    if not picks:
        return [], []

    # Parse trade setups from Stage 2 research
    setups = parse_trade_setups(research_text) if research_text else {}

    # Filter for high confidence only, sort by confidence desc
    eligible = [p for p in picks if p.get("confidence", 0) >= 7]
    eligible.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    if not eligible:
        return [], []

    # Don't duplicate: check contracts already open or pending
    open_contracts = {t["contract"] for t in open_trades}
    pending_contracts = {t["contract"] for t in pending}
    all_used = open_contracts | pending_contracts

    queued = []
    blocked = []
    slots = 3 - total_slots_used

    for pick in eligible:
        if slots <= 0:
            break

        contract = pick.get("contract", "")
        if not contract or contract in all_used:
            continue

        # === RUGCHECK SAFETY GATE ===
        is_safe, safety_score, risk_names, verdict = check_token_safety(contract)

        if not is_safe:
            blocked.append({
                "name": pick.get("name", "?"),
                "symbol": pick.get("symbol", "?"),
                "contract": contract,
                "verdict": verdict,
                "risks": risk_names[:5],  # Top 5 risks
                "confidence": pick.get("confidence", 0)
            })
            continue

        symbol = pick.get("symbol", "?").upper()
        trade_setup = setups.get(symbol, {})

        # Get current price for confirmation comparison
        current_price = get_token_price(contract)

        pending_entry = {
            "pick": pick,
            "trade_setup": trade_setup,
            "scan_num": scan_num,
            "contract": contract,
            "symbol": symbol,
            "name": pick.get("name", "?"),
            "rec_price": float(str(pick.get("entry_price", "0")).replace("$", "").replace(",", "").strip()),
            "price_at_queue": current_price,
            "confidence": pick.get("confidence", 0),
            "queued_at": datetime.now().isoformat()[:16],
            "safety_verdict": verdict,
            "safety_score": safety_score,
            "safety_risks": risk_names[:5]
        }

        pending.append(pending_entry)
        all_used.add(contract)
        queued.append(pending_entry)
        slots -= 1

    pb["pending_paper_trades"] = pending
    return queued, blocked


def confirm_pending_trades(pb):
    """Confirm or reject pending paper trades from the previous scan.
    A pending trade is CONFIRMED if:
      1. Price hasn't dumped >15% since recommendation
      2. Buy pressure is still present (ratio >= 0.8)
      3. Token is still reachable
    Returns (confirmed_trades, rejected_info)."""
    pending = pb.get("pending_paper_trades", [])
    if not pending:
        return [], []

    open_trades = pb.get("paper_trades", [])
    open_contracts = {t["contract"] for t in open_trades}

    confirmed = []
    rejected = []
    scan_num = pb.get("scans", 0) + 1

    for pt in pending:
        contract = pt.get("contract", "")

        # Skip if somehow a trade was already opened for this contract
        if contract in open_contracts:
            rejected.append({"name": pt["name"], "reason": "Already has open trade"})
            continue

        # Max 3 open trades
        if len(open_trades) >= 3:
            rejected.append({"name": pt["name"], "reason": "Trade slots full (3/3)"})
            continue

        # Get current price
        current_price = get_token_price(contract)
        if current_price is None:
            rejected.append({"name": pt["name"], "reason": "Token unreachable after 1 scan"})
            continue

        rec_price = pt.get("rec_price", 0)
        if rec_price <= 0:
            rejected.append({"name": pt["name"], "reason": "Invalid recommendation price"})
            continue

        # Check: price hasn't dumped >15% since recommendation
        price_change = ((current_price - rec_price) / rec_price) * 100
        if price_change < -15:
            rejected.append({
                "name": pt["name"],
                "reason": f"Price dumped {price_change:.1f}% since recommendation"
            })
            continue

        # Check: buy pressure still present
        current_data = get_token_full_data(contract)
        if current_data:
            buy_ratio = current_data.get("buy_ratio_1h", 1.0)
            if buy_ratio < 0.8:
                rejected.append({
                    "name": pt["name"],
                    "reason": f"Buy pressure collapsed (ratio: {buy_ratio})"
                })
                continue

        # === CONFIRMED â create the actual paper trade ===
        # Use CURRENT price as entry (more realistic after delay)
        pick = pt.get("pick", {})
        pick["entry_price"] = current_price  # Override with current price
        trade_setup = pt.get("trade_setup", {})

        trade = create_paper_trade(pb, pick, trade_setup, scan_num)
        if trade:
            # Tag with safety and confirmation info
            trade["safety_verdict"] = pt.get("safety_verdict", "unknown")
            trade["safety_score"] = pt.get("safety_score", 0)
            trade["confirmed_from_pending"] = True
            trade["price_at_recommendation"] = rec_price
            trade["price_change_during_confirmation"] = round(price_change, 1)

            open_trades.append(trade)
            open_contracts.add(contract)
            confirmed.append(trade)

    # Clear pending list â all processed
    pb["pending_paper_trades"] = []
    pb["paper_trades"] = open_trades

    return confirmed, rejected


def format_pending_alert(queued_entries, blocked_entries):
    """Format Telegram message for queued and blocked trades."""
    lines = []

    if queued_entries:
        lines.append(f"\u23f3 QUEUED ({len(queued_entries)}):")
        for pt in queued_entries:
            lines.append(
                f"  \U0001f7e1 {pt['name']} ({pt['symbol']}) "
                f"conf {pt['confidence']}/10 | ${pt['rec_price']:.10g} | "
                f"{pt.get('safety_verdict', '?')}"
            )

    if blocked_entries:
        lines.append(f"\U0001f6ab BLOCKED ({len(blocked_entries)}):")
        for bt in blocked_entries:
            lines.append(f"  \u274c {bt['name']} â {bt['verdict']}")

    return "\n".join(lines) if lines else ""


def format_confirmation_alert(confirmed, rejected):
    """Format Telegram message for confirmed/rejected pending trades."""
    lines = []

    if confirmed:
        lines.append(f"\u2705 CONFIRMED ({len(confirmed)}):")
        for trade in confirmed:
            pc = trade.get("price_change_during_confirmation", 0)
            pc_str = f"{pc:+.1f}%" if pc else "~"
            lines.append(
                f"  \U0001f7e2 {trade['token_name']} ({trade['symbol']}) "
                f"${trade['entry_price']:.10g} ({pc_str})"
            )

    if rejected:
        lines.append(f"\u274c REJECTED ({len(rejected)}):")
        for rej in rejected:
            lines.append(f"  \U0001f534 {rej['name']}: {rej['reason']}")

    return "\n".join(lines) if lines else ""


def format_new_trade_alert(trade):
    """Format a Telegram alert for a newly opened paper trade."""
    entry = trade["entry_price"]
    sl = trade["stop_loss"]
    tp1 = trade["tp1"]
    tp2 = trade["tp2"]
    tp3 = trade["tp3"]
    conf = trade.get("confidence", 0)

    return (
        f"\U0001f514 PAPER TRADE #{trade['trade_id']}\n"
        f"{trade['token_name']} ({trade['symbol']})\n"
        f"Entry ${entry:.10g} | SL ${sl:.10g}\n"
        f"TP1 ${tp1:.10g} ({round((tp1/entry - 1)*100)}%) | "
        f"TP2 ${tp2:.10g} ({round((tp2/entry - 1)*100)}%) | "
        f"TP3 ${tp3:.10g} ({round((tp3/entry - 1)*100)}%)\n"
        f"Conf {conf}/10 â {trade.get('reason', '?')}"
    )


# ============================================================
# TELEGRAM CONVERSATION HANDLER
# ============================================================

def get_telegram_updates(offset=None):
    """Fetch new messages sent TO the bot since last check."""
    try:
        params = {"timeout": 5, "allowed_updates": ["message"]}
        if offset:
            params["offset"] = offset
        r = requests.get(f"{TAPI}/getUpdates", params=params, timeout=10)
        if r.status_code == 200:
            return r.json().get("result", [])
    except:
        pass
    return []


def build_chat_context(pb):
    """Build concise scanner state for AI conversation."""
    trades = pb.get("paper_trades", [])
    pending = pb.get("pending_paper_trades", [])
    stats = pb.get("paper_trade_stats", {})
    history = pb.get("paper_trade_history", [])[-5:]
    perf = pb.get("performance", {})
    rules = pb.get("strategy_rules", [])
    picks = pb.get("active_picks", [])[-5:]

    lines = [
        f"Scans: {pb.get('scans', 0)} | Picks: {perf.get('total_picks', 0)} | "
        f"Win rate: {round((perf.get('wins',0)/max(perf.get('total_picks',1),1))*100,1)}%",
        f"\nOPEN TRADES ({len(trades)}/3):"
    ]
    for t in trades:
        cur = t.get("current_price", t["entry_price"])
        pnl = ((cur - t["entry_price"]) / t["entry_price"]) * 100
        lines.append(
            f"  #{t['trade_id']} {t['token_name']} ({t['symbol']}) | "
            f"Entry ${t['entry_price']:.10g} â ${cur:.10g} | "
            f"PnL {pnl:+.1f}% | SL ${t['stop_loss']:.10g}"
        )
    if not trades:
        lines.append("  (none)")

    if pending:
        lines.append(f"\nPENDING ({len(pending)}):")
        for p in pending:
            lines.append(f"  {p['name']} ({p['symbol']}) conf {p['confidence']}/10 @ ${p['rec_price']:.10g}")

    if history:
        lines.append(f"\nLAST {len(history)} CLOSED:")
        for h in history:
            lines.append(
                f"  #{h.get('trade_id','?')} {h.get('token_name','?')} | "
                f"{h.get('result','?')} {h.get('pnl_pct', 0):+.1f}% | {h.get('exit_reason','?')}"
            )

    lines.append(
        f"\nSTATS: {stats.get('total_trades',0)} trades | "
        f"{stats.get('wins',0)}W-{stats.get('losses',0)}L | "
        f"WR {stats.get('win_rate',0)}% | Avg {stats.get('avg_pnl_pct',0):+.1f}%"
    )

    if picks:
        lines.append(f"\nRECENT PICKS:")
        for pk in picks:
            lines.append(
                f"  {pk.get('name','?')} conf {pk.get('confidence','?')}/10 | "
                f"entry ${pk.get('entry_price',0):.10g} | {pk.get('status', 'active')}"
            )

    if rules:
        lines.append(f"\nTOP RULES:")
        for r in rules[-5:]:
            lines.append(f"  - {r}")

    return "\n".join(lines)


def handle_user_messages(pb):
    """Check for user messages and reply using AI with scanner context."""
    last_offset = pb.get("last_update_id", 0)
    updates = get_telegram_updates(offset=last_offset + 1 if last_offset else None)

    if not updates:
        return

    replies_sent = 0
    for update in updates:
        pb["last_update_id"] = update["update_id"]

        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if not text or chat_id != USER_ID:
            continue

        # Build context and reply with AI
        context = build_chat_context(pb)

        system = (
            "You are a Solana memecoin scanner assistant. Short, direct replies. "
            "Talk casual like a trading buddy. Use data below to answer.\n"
            "If user asks to close a trade or change something, confirm what you'd do "
            "and note it takes effect next scan cycle.\n"
            "Keep replies under 300 words.\n\n"
            f"{context}"
        )

        reply = call_groq(system, text, temperature=0.6)
        if reply:
            if len(reply) > 4000:
                reply = reply[:3997] + "..."
            send_msg(f"\U0001f4ac {reply}")
            replies_sent += 1

    if replies_sent:
        send_msg(f"\u2705 Replied to {replies_sent} message(s). Continuing scan...")


# ============================================================
# AI CALLS
# ============================================================

def call_groq(system, prompt, temperature=0.8):
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ],
                "temperature": temperature,
                "max_tokens": 4096
            },
            timeout=60
        )
        result = resp.json()
        return result["choices"][0]["message"]["content"]
    except:
        return None


def extract_picks_json(response):
    try:
        match = re.search(r'```json\s*(\[.*?\])\s*```', response, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        match = re.search(r'\[.*\]', response, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except:
        pass
    return None


# ============================================================
# MAIN
# ============================================================

def main():
    playbook = load_playbook()

    # ---- STEP 0: Reply to any user messages ----
    handle_user_messages(playbook)

    scan_num = playbook.get("scans", 0) + 1
    perf = playbook.get("performance", {})
    total_picks = perf.get("total_picks", 0)
    win_rate = round((perf.get("wins", 0) / max(total_picks, 1)) * 100, 1) if total_picks > 0 else 0
    pt_stats = playbook.get("paper_trade_stats", {})
    pt_open = len(playbook.get("paper_trades", []))
    pt_pending = len(playbook.get("pending_paper_trades", []))

    send_msg(
        f"\U0001f50d Scan #{scan_num}\n"
        f"\U0001f9e0 {len(playbook.get('lessons', []))}pat | {total_picks}picks {win_rate}%wr | "
        f"{len(playbook.get('trade_memory', []))}mem | {len(playbook.get('strategy_rules', []))}rules\n"
        f"\U0001f4b5 {pt_open}/3 open | {pt_pending} pending | "
        f"{pt_stats.get('total_trades', 0)} closed {pt_stats.get('win_rate', 0)}%wr"
    )

    # ---- STEP 1: Check past picks (with full trade memory) ----
    tracker_report = check_past_picks(playbook)
    if tracker_report:
        send_msg(tracker_report)

    # ---- STEP 1.5: Monitor open paper trades ----
    pt_report = monitor_paper_trades(playbook)
    if pt_report:
        send_msg(pt_report)

    # ---- STEP 1.6: Confirm pending paper trades from previous scan ----
    if playbook.get("pending_paper_trades"):
        confirmed, rejected = confirm_pending_trades(playbook)
        conf_alert = format_confirmation_alert(confirmed, rejected)
        if conf_alert:
            send_msg(conf_alert)
        if confirmed:
            for trade in confirmed:
                send_msg(format_new_trade_alert(trade))

    # ---- STEP 1.7: Update ROI tiers and strategy rules ----
    update_roi_tiers(playbook)

    if scan_num % 5 == 0 and len(playbook.get("trade_memory", [])) >= 5:
        generate_strategy_rules(playbook)
        rules = playbook.get("strategy_rules", [])
        if rules:
            rules_text = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(rules[-5:]))
            send_msg(f"\U0001f504 Rules updated:\n{rules_text}")

    # ---- STEP 2: Gather new data ----
    tokens, stats = run_full_scan()

    if not tokens:
        send_msg(f"\U0001f634 Nothing trending. Next scan in 15m.")
        save_playbook(playbook)
        return

    token_data = "\n\n---\n\n".join(tokens)
    track_tokens(playbook, token_data)

    send_msg(f"\U0001f4ca {len(tokens)} tokens found. Running Stage 1...")

    # ---- STEP 3: Stage 1 - Quick scan (now uses dynamic strategy rules) ----
    scan_prompt = build_scan_prompt(playbook)
    stage1_result = call_groq(scan_prompt, f"Analyze:\n\n{token_data}", temperature=0.5)

    if not stage1_result:
        send_msg("\u26a0\ufe0f Stage 1 failed. Next cycle.")
        save_playbook(playbook)
        return

    # Save picks for tracking (with full market snapshots)
    save_new_picks(playbook, stage1_result)

    picks = extract_picks_json(stage1_result)
    if picks:
        picks_summary = "\n".join(
            f"#{p.get('rank','?')} {p.get('name','?')} ({p.get('symbol','?')}) "
            f"conf {p.get('confidence','?')}/10 â {p.get('reason','?')}"
            for p in picks
        )
        send_msg(f"\U0001f3af Picks:\n{picks_summary}\n\U0001f52c Stage 2 running...")
    else:
        picks_summary = stage1_result[:500]
        send_msg("\U0001f3af Picks found. Stage 2...")

    # ---- STEP 4: Stage 2 - Deep research (with full self-learning context) ----
    research_prompt = build_research_prompt(playbook)
    deep_prompt = (
        f"Top picks:\n{picks_summary}\n\n"
        f"Full token data:\n\n{token_data}\n\n"
        f"Do DEEP RESEARCH on each pick. Reference your real track record, "
        f"strategy rules, and trade memory. Flag any picks that match your "
        f"losing patterns or avoid conditions."
    )

    research = call_groq(research_prompt, deep_prompt, temperature=0.85)

    if research:
        # Extract and save lessons
        if "PLAYBOOK" in research and "UPDATE" in research:
            try:
                note = research.split("UPDATE")[-1]
                if "STRATEGY" in note:
                    note = note.split("STRATEGY")[0]
                elif "MISTAKE" in note:
                    note = note.split("MISTAKE")[0]
                elif "SELF-REFLECTION" in note:
                    note = note.split("SELF-REFLECTION")[0]
                note = note.strip(": \n")[:500]
                playbook.setdefault("lessons", []).append({
                    "date": datetime.now().isoformat()[:10],
                    "note": note
                })
                playbook["lessons"] = playbook["lessons"][-50:]
            except:
                pass

        send_msg(f"\U0001f4ca SCAN #{scan_num} RESEARCH\n{'='*30}\n\n{research}")
    else:
        send_msg("\u26a0\ufe0f Stage 2 failed. Stage 1 picks still valid.")

    # ---- STEP 5: Queue paper trades (confidence >= 7 picks) ----
    queued, blocked = queue_pending_paper_trades(
        playbook, stage1_result, research or "", scan_num
    )
    pending_alert = format_pending_alert(queued, blocked)
    if pending_alert:
        send_msg(pending_alert)

    open_count = len(playbook.get("paper_trades", []))
    pending_count = len(playbook.get("pending_paper_trades", []))
    if queued:
        send_msg(f"\U0001f4b5 {open_count}/3 open | {pending_count} pending | +{len(queued)} queued")
    elif not blocked:
        if open_count >= 3:
            send_msg("\U0001f4b5 Slots full (3/3)")
        else:
            send_msg("\U0001f4b5 No picks \u22657 confidence")

    save_playbook(playbook)


if __name__ == "__main__":
    main()