import os, re, json, requests
from datetime import datetime
from collections import Counter

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
USER_ID = os.environ.get("TELEGRAM_USER_ID", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")

TAPI = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ============================================================
# STAGE 1: Quick scan prompt
# ============================================================
SCAN_PROMPT = """You are a sharp Solana memecoin scanner. Analyze the token data and quickly identify
the TOP 3 tokens with the best 2x-10x potential.

For each pick, respond in this EXACT JSON format and nothing else:
```json
[
  {"rank": 1, "name": "TOKEN_NAME", "symbol": "SYMBOL", "contract": "ADDRESS", "entry_price": "current_price_number", "reason": "Brief reason"},
  {"rank": 2, "name": "TOKEN_NAME", "symbol": "SYMBOL", "contract": "ADDRESS", "entry_price": "current_price_number", "reason": "Brief reason"},
  {"rank": 3, "name": "TOKEN_NAME", "symbol": "SYMBOL", "contract": "ADDRESS", "entry_price": "current_price_number", "reason": "Brief reason"}
]
```

Use the ACTUAL current price from the data as entry_price (just the number, no $ sign).

Selection criteria (in order of importance):
- Buy/sell ratio > 1.3 in last hour (whale accumulation)
- Volume spike above average (momentum building)
- Market cap under $5M (room to run 2-10x)
- Strong recent price action but NOT already pumped 500%+ in 24h
- Good liquidity relative to market cap
- NEW PAIRS and PUMP.FUN graduates get bonus points

Skip: already-pumped tokens, dead volume, MC > $10M
Only output the JSON, nothing else."""


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

TRADE SETUP:
Entry Price: $[specific price]
Stop Loss: $[20-30% below entry]
TP1 (Safe): $[~2x from entry]
TP2 (Mid): $[~3-5x from entry]
TP3 (Moon): $[~5-10x from entry]

Strategy: [quick flip / swing / hold]
Risk Level: LOW / MEDIUM / HIGH / DEGEN
Time Outlook: [specific timeframe]
Confidence: [1-10]

After all picks:
WHALE WATCH: Unusual whale activity
AVOID LIST: Tokens that look like traps
MARKET VIBE: Overall Solana memecoin sentiment

PLAYBOOK UPDATE: (2-3 specific lessons - reference your win/loss data if available)
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
# PLAYBOOK + PERFORMANCE TRACKING
# ============================================================

def load_playbook():
    try:
        with open("playbook.json") as f:
            return json.load(f)
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
            "lose_patterns": []
        }


def save_playbook(pb):
    pb["last_scan"] = datetime.now().isoformat()[:16]
    pb["scans"] = pb.get("scans", 0) + 1
    pb["tokens_seen"] = pb.get("tokens_seen", [])[-200:]
    pb["pick_history"] = pb.get("pick_history", [])[-100:]
    pb["active_picks"] = pb.get("active_picks", [])[-20:]
    pb["win_patterns"] = pb.get("win_patterns", [])[-30:]
    pb["lose_patterns"] = pb.get("lose_patterns", [])[-30:]
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

            active.append({
                "name": pick.get("name", "Unknown"),
                "symbol": pick.get("symbol", "?"),
                "contract": pick.get("contract", ""),
                "entry_price": entry_float,
                "reason": pick.get("reason", ""),
                "picked_at": datetime.now().isoformat()[:16],
                "scans_tracked": 0,
                "peak_price": entry_float,
                "lowest_price": entry_float
            })
        except:
            continue

    pb["active_picks"] = active[-20:]


def check_past_picks(pb):
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
                result = {
                    **pick,
                    "final_price": 0,
                    "return_pct": return_pct,
                    "result": "DEAD/RUGGED",
                    "closed_at": datetime.now().isoformat()[:16]
                }
                history.append(result)
                performance["total_picks"] += 1
                performance["losses"] += 1

                pb.setdefault("lose_patterns", []).append(
                    f"{pick['name']}: Token went dead/unreachable after {pick['scans_tracked']} scans. "
                    f"Reason picked: {pick.get('reason', '?')}"
                )
                report_lines.append(f"💀 {pick['name']} - DEAD/RUGGED (-100%)")
            else:
                still_active.append(pick)
            continue

        entry_price = pick.get("entry_price", 0)
        if entry_price <= 0:
            still_active.append(pick)
            continue

        return_pct = round(((current_price - entry_price) / entry_price) * 100, 1)
        pick["scans_tracked"] = pick.get("scans_tracked", 0) + 1

        if current_price > pick.get("peak_price", 0):
            pick["peak_price"] = current_price
        if current_price < pick.get("lowest_price", float('inf')):
            pick["lowest_price"] = current_price

        peak_return = round(((pick["peak_price"] - entry_price) / entry_price) * 100, 1)

        if pick["scans_tracked"] >= 12:
            result_tag = ""
            if return_pct >= 100:
                result_tag = "WIN (2x+)"
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

            best = performance.get("best_pick", {})
            if not best or return_pct > best.get("return_pct", -999):
                performance["best_pick"] = {"name": pick["name"], "return_pct": return_pct}

            worst = performance.get("worst_pick", {})
            if not worst or return_pct < worst.get("return_pct", 999):
                performance["worst_pick"] = {"name": pick["name"], "return_pct": return_pct}

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

            emoji = "🟢" if return_pct > 20 else "🔴" if return_pct < -10 else "⚪"
            report_lines.append(
                f"{emoji} {pick['name']} ({pick['symbol']}): {return_pct:+.1f}% "
                f"(peak: +{peak_return}%) - {result_tag}"
            )
        else:
            emoji = "📈" if return_pct > 0 else "📉"
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

        header = (
            f"📋 PICK TRACKER UPDATE\n"
            f"{'='*35}\n"
            f"Active picks: {len(still_active)} | Completed: {total}\n"
            f"Win rate: {win_rate}% | Avg return: {avg_ret:+.1f}%\n"
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

    send_msg(
        f"🔍 Scan #{scan_num} starting...\n"
        f"📡 Sources: DEXScreener Boosted + Profiles + New/PumpFun\n"
        f"🧠 Brain: {len(playbook.get('lessons', []))} patterns | "
        f"Track record: {total_picks} picks, {win_rate}% win rate\n"
        f"⏳ 2-stage deep analysis + performance check..."
    )

    # ---- STEP 1: Check past picks ----
    tracker_report = check_past_picks(playbook)
    if tracker_report:
        send_msg(tracker_report)

    # ---- STEP 2: Gather new data ----
    tokens, stats = run_full_scan()

    if not tokens:
        send_msg(
            "😴 No trending Solana tokens right now.\n"
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
        f"📊 Found {len(tokens)} tokens\n"
        f"Boosted: {stats.get('boosted', 0)} | Profiles: {stats.get('profiles', 0)} | "
        f"New/PumpFun: {stats.get('new_pairs', 0)}\n"
        f"🔬 Stage 1: Quick scan..."
    )

    # ---- STEP 3: Stage 1 - Quick scan ----
    stage1_result = call_groq(SCAN_PROMPT, f"Analyze:\n\n{token_data}", temperature=0.5)

    if not stage1_result:
        send_msg("⚠️ Stage 1 failed. Retrying next cycle.")
        save_playbook(playbook)
        return

    # Save picks for tracking
    save_new_picks(playbook, stage1_result)

    picks = extract_picks_json(stage1_result)
    if picks:
        picks_summary = "\n".join(
            f"#{p.get('rank','?')}: {p.get('name','?')} ({p.get('symbol','?')}) - {p.get('reason','?')}"
            for p in picks
        )
        send_msg(
            f"🎯 Stage 1 picks:\n{picks_summary}\n\n"
            f"🔬 Stage 2: Deep research..."
        )
    else:
        picks_summary = stage1_result[:500]
        send_msg("🎯 Picks identified. Running deep research...")

    # ---- STEP 4: Stage 2 - Deep research ----
    research_prompt = build_research_prompt(playbook)
    deep_prompt = (
        f"Top picks:\n{picks_summary}\n\n"
        f"Full token data:\n\n{token_data}\n\n"
        f"Do DEEP RESEARCH on each pick. Reference your real track record and what you've learned."
    )

    research = call_groq(research_prompt, deep_prompt, temperature=0.85)

    if research:
        if "PLAYBOOK" in research and "UPDATE" in research:
            try:
                note = research.split("UPDATE")[-1]
                if "SELF-REFLECTION" in note:
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
        header = (
            f"📊 SCAN #{scan_num} - DEEP RESEARCH COMPLETE\n"
            f"{'='*40}\n"
            f"Tokens scanned: {len(tokens)} | Active picks tracking: {active_count}\n"
            f"Track record: {total_picks} picks | {win_rate}% win rate\n"
            f"Brain patterns: {len(playbook.get('lessons', []))}\n"
            f"{'='*40}\n\n"
        )
        send_msg(header + research)
    else:
        send_msg("⚠️ Deep research failed. Stage 1 picks above still valid.")

    save_playbook(playbook)


if __name__ == "__main__":
    main()
