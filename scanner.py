import os, re, json, requests, math
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
            }
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
    scan_num = playbook.get("scans", 0) + 1
    perf = playbook.get("performance", {})
    total_picks = perf.get("total_picks", 0)
    win_rate = round((perf.get("wins", 0) / max(total_picks, 1)) * 100, 1) if total_picks > 0 else 0
    rules_count = len(playbook.get("strategy_rules", []))
    memory_count = len(playbook.get("trade_memory", []))

    send_msg(
        f"\U0001f50d Scan #{scan_num} starting...\n"
        f"\U0001f4e1 Sources: DEXScreener Boosted + Profiles + New/PumpFun\n"
        f"\U0001f9e0 Brain: {len(playbook.get('lessons', []))} patterns | "
        f"Track record: {total_picks} picks, {win_rate}% win rate\n"
        f"\U0001f4ca Trade memory: {memory_count} detailed records | "
        f"Strategy rules: {rules_count}\n"
        f"\u23f3 Self-learning scan with dynamic strategy..."
    )

    # ---- STEP 1: Check past picks (with full trade memory) ----
    tracker_report = check_past_picks(playbook)
    if tracker_report:
        send_msg(tracker_report)

    # ---- STEP 1.5: Update ROI tiers and strategy rules ----
    update_roi_tiers(playbook)

    # Generate dynamic strategy rules every 5 scans (once enough data)
    if scan_num % 5 == 0 and len(playbook.get("trade_memory", [])) >= 5:
        send_msg("\U0001f504 Updating strategy rules from trade data...")
        generate_strategy_rules(playbook)
        rules = playbook.get("strategy_rules", [])
        if rules:
            rules_text = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(rules))
            send_msg(f"\U0001f4dd New strategy rules:\n{rules_text}")

    # ---- STEP 2: Gather new data ----
    tokens, stats = run_full_scan()

    if not tokens:
        send_msg(
            f"\U0001f634 No trending Solana tokens right now.\n"
            f"Sources: Boosted ({stats.get('boosted', 0)}) | "
            f"Profiles ({stats.get('profiles', 0)}) | "
            f"New/PumpFun ({stats.get('new_pairs', 0)})\n"
            "Checking again in 15 min."
        )
        save_playbook(playbook)
        return

    token_data = "\n\n---\n\n".join(tokens)
    track_tokens(playbook, token_data)

    send_msg(
        f"\U0001f4ca Found {len(tokens)} tokens\n"
        f"Boosted: {stats.get('boosted', 0)} | Profiles: {stats.get('profiles', 0)} | "
        f"New/PumpFun: {stats.get('new_pairs', 0)}\n"
        f"\U0001f52c Stage 1: Pattern-aware quick scan..."
    )

    # ---- STEP 3: Stage 1 - Quick scan (now uses dynamic strategy rules) ----
    scan_prompt = build_scan_prompt(playbook)
    stage1_result = call_groq(scan_prompt, f"Analyze:\n\n{token_data}", temperature=0.5)

    if not stage1_result:
        send_msg("\u26a0\ufe0f Stage 1 failed. Retrying next cycle.")
        save_playbook(playbook)
        return

    # Save picks for tracking (with full market snapshots)
    save_new_picks(playbook, stage1_result)

    picks = extract_picks_json(stage1_result)
    if picks:
        picks_summary = "\n".join(
            f"#{p.get('rank','?')}: {p.get('name','?')} ({p.get('symbol','?')}) "
            f"[Conf: {p.get('confidence','?')}/10] - {p.get('reason','?')}"
            for p in picks
        )
        send_msg(
            f"\U0001f3af Stage 1 picks:\n{picks_summary}\n\n"
            f"\U0001f52c Stage 2: Deep research with full trade intelligence..."
        )
    else:
        picks_summary = stage1_result[:500]
        send_msg("\U0001f3af Picks identified. Running deep research...")

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

        active_count = len(playbook.get("active_picks", []))
        rules_count = len(playbook.get("strategy_rules", []))
        mistakes_count = len(playbook.get("mistake_log", []))
        memory_count = len(playbook.get("trade_memory", []))

        header = (
            f"\U0001f4ca SCAN #{scan_num} - DEEP RESEARCH COMPLETE\n"
            f"{'='*40}\n"
            f"Tokens scanned: {len(tokens)} | Active picks: {active_count}\n"
            f"Track record: {total_picks} picks | {win_rate}% win rate\n"
            f"Brain: {len(playbook.get('lessons', []))} patterns | "
            f"Trade memory: {memory_count}\n"
            f"Strategy rules: {rules_count} | Mistakes logged: {mistakes_count}\n"
            f"{'='*40}\n\n"
        )
        send_msg(header + research)
    else:
        send_msg("\u26a0\ufe0f Deep research failed. Stage 1 picks above still valid.")

    save_playbook(playbook)


if __name__ == "__main__":
    main()
