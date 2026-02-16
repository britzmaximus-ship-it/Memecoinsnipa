+    1 import os, re, json, requests, math
+    2 from datetime import datetime
+    3 from collections import Counter
+    4 
+    5 BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
+    6 USER_ID = os.environ.get("TELEGRAM_USER_ID", "")
+    7 GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
+    8 
+    9 TAPI = f"https://api.telegram.org/bot{BOT_TOKEN}"
+   10 
+   11 
+   12 # ============================================================
+   13 # STAGE 1: Quick scan prompt (now with pattern-aware filtering)
+   14 # ============================================================
+   15 def build_scan_prompt(playbook):
+   16     """Build Stage 1 prompt that's informed by learned strategy rules."""
+   17     base = """You are a sharp Solana memecoin scanner. Analyze the token data and quickly identify
+   18 the TOP 3 tokens with the best 2x-10x potential.
+   19 
+   20 For each pick, respond in this EXACT JSON format and nothing else:
+   21 ```json
+   22 [
+   23   {"rank": 1, "name": "TOKEN_NAME", "symbol": "SYMBOL", "contract": "ADDRESS", "entry_price": "current_price_number", "confidence": 7, "reason": "Brief reason"},
+   24   {"rank": 2, "name": "TOKEN_NAME", "symbol": "SYMBOL", "contract": "ADDRESS", "entry_price": "current_price_number", "confidence": 6, "reason": "Brief reason"},
+   25   {"rank": 3, "name": "TOKEN_NAME", "symbol": "SYMBOL", "contract": "ADDRESS", "entry_price": "current_price_number", "confidence": 5, "reason": "Brief reason"}
+   26 ]
+   27 ```
+   28 
+   29 Use the ACTUAL current price from the data as entry_price (just the number, no $ sign).
+   30 confidence = 1-10 based on how closely the token matches your winning criteria.
+   31 """
+   32 
+   33     # Inject dynamic strategy rules if they exist
+   34     rules = playbook.get("strategy_rules", [])
+   35     if rules:
+   36         base += "\n--- YOUR LEARNED STRATEGY RULES (follow these, they come from your real results) ---\n"
+   37         for rule in rules[-15:]:
+   38             base += f"- {rule}\n"
+   39         base += "\nPrioritize tokens that match your WINNING rules. Avoid tokens that match LOSING rules.\n"
+   40     else:
+   41         base += """
+   42 Default criteria (will be replaced by learned rules once you have data):
+   43 - Buy/sell ratio > 1.3 in last hour (whale accumulation)
+   44 - Volume spike above average (momentum building)
+   45 - Market cap under $5M (room to run 2-10x)
+   46 - Strong recent price action but NOT already pumped 500%+ in 24h
+   47 - Good liquidity relative to market cap
+   48 - NEW PAIRS and PUMP.FUN graduates get bonus points
+   49 """
+   50 
+   51     # Inject failure patterns to avoid
+   52     avoid_patterns = playbook.get("avoid_conditions", [])
+   53     if avoid_patterns:
+   54         base += "\n--- RED FLAGS (these conditions caused losses - AVOID) ---\n"
+   55         for ap in avoid_patterns[-10:]:
+   56             base += f"- {ap}\n"
+   57 
+   58     base += "\nSkip: already-pumped tokens, dead volume, MC > $10M\nOnly output the JSON, nothing else."
+   59     return base
+   60 
+   61 
+   62 # ============================================================
+   63 # STAGE 2: Deep research prompt (built dynamically with performance data)
+   64 # ============================================================
+   65 def build_research_prompt(playbook):
+   66     """Build the deep research prompt with real performance data."""
+   67     prompt = """You are a sharp, street-smart Solana memecoin trading AI that LEARNS FROM REAL RESULTS.
+   68 You talk like a real trading partner - casual, direct, hyped when something looks good, honest when it doesn't.
+   69 
+   70 Your personality:
+   71 - Talk like you're texting a friend who trades
+   72 - Use emojis naturally but don't overdo it
+   73 - Be decisive - strong opinions backed by data
+   74 - Reference your ACTUAL track record when making calls
+   75 - Admit when your past picks were wrong and explain what you learned
+   76 
+   77 IMPORTANT: Only pick coins that realistically have 2x-10x potential.
+   78 Skip anything that looks pumped out or has no room to run.
+   79 """
+   80 
+   81     # Inject REAL performance data
+   82     stats = playbook.get("performance", {})
+   83     total_picks = stats.get("total_picks", 0)
+   84     if total_picks > 0:
+   85         wins = stats.get("wins", 0)
+   86         losses = stats.get("losses", 0)
+   87         avg_return = stats.get("avg_return_pct", 0)
+   88         best = stats.get("best_pick", {})
+   89         worst = stats.get("worst_pick", {})
+   90         win_rate = round((wins / max(total_picks, 1)) * 100, 1)
+   91 
+   92         prompt += f"""
+   93 YOUR REAL TRACK RECORD (from watching your past picks):
+   94 - Total picks tracked: {total_picks}
+   95 - Wins (went up): {wins} | Losses (went down): {losses}
+   96 - Win rate: {win_rate}%
+   97 - Average return: {avg_return:+.1f}%
+   98 """
+   99         if best.get("name"):
+  100             prompt += f"- Best pick: {best['name']} ({best.get('return_pct', 0):+.1f}%)\n"
+  101         if worst.get("name"):
+  102             prompt += f"- Worst pick: {worst['name']} ({worst.get('return_pct', 0):+.1f}%)\n"
+  103 
+  104         prompt += "\nUSE THIS DATA. If certain types of tokens tend to win/lose, adjust your picks accordingly.\n"
+  105 
+  106     # Inject ROI tier analysis
+  107     roi_tiers = playbook.get("roi_tiers", {})
+  108     if roi_tiers:
+  109         prompt += "\n--- ROI ANALYSIS BY SETUP TYPE ---\n"
+  110         for tier_name, tier_data in roi_tiers.items():
+  111             avg_roi = tier_data.get("avg_roi", 0)
+  112             count = tier_data.get("count", 0)
+  113             prompt += f"- {tier_name}: avg ROI {avg_roi:+.1f}% across {count} picks\n"
+  114         prompt += "PRIORITIZE setup types with the highest historical ROI.\n"
+  115 
+  116     # Inject winning/losing patterns
+  117     win_patterns = playbook.get("win_patterns", [])
+  118     lose_patterns = playbook.get("lose_patterns", [])
+  119 
+  120     if win_patterns:
+  121         prompt += "\nPATTERNS THAT LED TO WINS:\n"
+  122         for p in win_patterns[-10:]:
+  123             prompt += f"- {p}\n"
+  124 
+  125     if lose_patterns:
+  126         prompt += "\nPATTERNS THAT LED TO LOSSES:\n"
+  127         for p in lose_patterns[-10:]:
+  128             prompt += f"- {p}\n"
+  129 
+  130     # Inject dynamic strategy rules
+  131     rules = playbook.get("strategy_rules", [])
+  132     if rules:
+  133         prompt += "\n--- YOUR STRATEGY RULES (learned from real results, FOLLOW THESE) ---\n"
+  134         for r in rules[-15:]:
+  135             prompt += f"- {r}\n"
+  136 
+  137     # Inject avoid conditions
+  138     avoid_conditions = playbook.get("avoid_conditions", [])
+  139     if avoid_conditions:
+  140         prompt += "\n--- CONDITIONS TO AVOID (caused losses) ---\n"
+  141         for ac in avoid_conditions[-10:]:
+  142             prompt += f"- {ac}\n"
+  143 
+  144     # Inject mistake post-mortems
+  145     mistakes = playbook.get("mistake_log", [])
+  146     if mistakes:
+  147         prompt += "\n--- RECENT MISTAKES & LESSONS ---\n"
+  148         for m in mistakes[-5:]:
+  149             prompt += f"- [{m.get('date', '')}] {m.get('token', '')}: {m.get('lesson', '')}\n"
+  150 
+  151     # Inject learned playbook
+  152     if playbook.get("lessons"):
+  153         prompt += "\n--- YOUR LEARNED PLAYBOOK ---\n"
+  154         for l in playbook["lessons"][-20:]:
+  155             prompt += f"- [{l.get('date','')}] {l.get('note','')}\n"
+  156 
+  157     # Inject repeat sightings
+  158     recent_tokens = playbook.get("tokens_seen", [])[-50:]
+  159     if recent_tokens:
+  160         token_names = [t["name"] for t in recent_tokens]
+  161         repeats = {n: c for n, c in Counter(token_names).items() if c >= 2}
+  162         if repeats:
+  163             prompt += "\n--- REPEAT SIGHTINGS ---\n"
+  164             for name, count in sorted(repeats.items(), key=lambda x: x[1], reverse=True)[:10]:
+  165                 prompt += f"- {name}: seen {count} times\n"
+  166 
+  167     prompt += f"""
+  168 Scan #{playbook.get('scans', 0) + 1}. You've been learning for {playbook.get('scans', 0)} scans.
+  169 
+  170 For EACH pick provide:
+  171 
+  172 PICK #[number]: [TOKEN NAME] ([SYMBOL])
+  173 Contract: [address]
+  174 
+  175 DEEP RESEARCH:
+  176 - What the data tells us (reference specific numbers)
+  177 - Volume pattern analysis
+  178 - Buy pressure analysis (retail or whales?)
+  179 - Market cap trajectory - where could this realistically go?
+  180 - How new is this token?
+  181 - Does this match any of your WINNING or LOSING patterns?
+  182 - Similarity score to past winners (how closely does this resemble your best picks?)
+  183 
+  184 TRADE SETUP:
+  185 Entry Price: $[specific price]
+  186 Stop Loss: $[20-30% below entry]
+  187 TP1 (Safe): $[~2x from entry]
+  188 TP2 (Mid): $[~3-5x from entry]
+  189 TP3 (Moon): $[~5-10x from entry]
+  190 
+  191 Strategy: [quick flip / swing / hold]
+  192 Risk Level: LOW / MEDIUM / HIGH / DEGEN
+  193 Time Outlook: [specific timeframe]
+  194 Confidence: [1-10] (based on similarity to past winners and how well this matches your learned strategy rules)
+  195 
+  196 After all picks:
+  197 WHALE WATCH: Unusual whale activity
+  198 AVOID LIST: Tokens that look like traps (explain WHY using your learned avoid conditions)
+  199 MARKET VIBE: Overall Solana memecoin sentiment
+  200 
+  201 PLAYBOOK UPDATE: (2-3 specific lessons - reference your win/loss data)
+  202 STRATEGY RULE UPDATE: Based on your results, what rules should be ADDED, MODIFIED, or REMOVED?
+  203 MISTAKE REFLECTION: What recent mistakes did you make? What specific conditions will you watch for to avoid them?
+  204 SELF-REFLECTION: What did you learn from your past picks' performance? What will you do differently?
+  205 
+  206 Not financial advice."""
+  207 
+  208     return prompt
+  209 
+  210 
+  211 # ============================================================
+  212 # TELEGRAM
+  213 # ============================================================
+  214 
+  215 def send_msg(text):
+  216     for i in range(0, len(text), 4000):
+  217         chunk = text[i:i+4000]
+  218         try:
+  219             requests.post(f"{TAPI}/sendMessage", json={
+  220                 "chat_id": USER_ID, "text": chunk
+  221             }, timeout=10)
+  222         except:
+  223             pass
+  224 
+  225 
+  226 # ============================================================
+  227 # DATA SOURCES
+  228 # ============================================================
+  229 
+  230 def fetch_boosted_tokens():
+  231     try:
+  232         r = requests.get("https://api.dexscreener.com/token-boosts/top/v1", timeout=10)
+  233         tokens = r.json()[:30]
+  234         return [
+  235             {"addr": t.get("tokenAddress", ""), "source": "boosted"}
+  236             for t in tokens
+  237             if t.get("chainId") == "solana" and t.get("tokenAddress")
+  238         ]
+  239     except:
+  240         return []
+  241 
+  242 
+  243 def fetch_latest_profiles():
+  244     try:
+  245         r = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10)
+  246         tokens = r.json()[:30]
+  247         return [
+  248             {"addr": t.get("tokenAddress", ""), "source": "profile"}
+  249             for t in tokens
+  250             if t.get("chainId") == "solana" and t.get("tokenAddress")
+  251         ]
+  252     except:
+  253         return []
+  254 
+  255 
+  256 def fetch_new_pairs():
+  257     try:
+  258         r = requests.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=10)
+  259         tokens = r.json()[:30]
+  260         return [
+  261             {"addr": t.get("tokenAddress", ""), "source": "new/pumpfun"}
+  262             for t in tokens
+  263             if t.get("chainId") == "solana" and t.get("tokenAddress")
+  264         ]
+  265     except:
+  266         return []
+  267 
+  268 
+  269 def get_token_price(contract_addr):
+  270     """Get current price for a single token."""
+  271     try:
+  272         r = requests.get(
+  273             f"https://api.dexscreener.com/tokens/v1/solana/{contract_addr}",
+  274             timeout=8
+  275         )
+  276         data = r.json()
+  277         pairs = data if isinstance(data, list) else data.get("pairs", [])
+  278         if not pairs:
+  279             return None
+  280         p = sorted(
+  281             [x for x in pairs if x],
+  282             key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0),
+  283             reverse=True
+  284         )[0]
+  285         price = p.get("priceUsd")
+  286         return float(price) if price else None
+  287     except:
+  288         return None
+  289 
+  290 
+  291 def get_token_full_data(contract_addr):
+  292     """Get full market data for a token (used for detailed trade memory)."""
+  293     try:
+  294         r = requests.get(
+  295             f"https://api.dexscreener.com/tokens/v1/solana/{contract_addr}",
+  296             timeout=8
+  297         )
+  298         data = r.json()
+  299         pairs = data if isinstance(data, list) else data.get("pairs", [])
+  300         if not pairs:
+  301             return None
+  302         p = sorted(
+  303             [x for x in pairs if x],
+  304             key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0),
+  305             reverse=True
+  306         )[0]
+  307 
+  308         mc = float(p.get("marketCap", 0) or 0)
+  309         lq = float(p.get("liquidity", {}).get("usd", 0) or 0)
+  310         vol_1h = float(p.get("volume", {}).get("h1", 0) or 0)
+  311         vol_24h = float(p.get("volume", {}).get("h24", 0) or 0)
+  312         tx_h1 = p.get("txns", {}).get("h1", {})
+  313         buys_1h = int(tx_h1.get("buys", 0) or 0)
+  314         sells_1h = int(tx_h1.get("sells", 0) or 0)
+  315         buy_ratio_1h = round(buys_1h / max(sells_1h, 1), 2)
+  316         price = float(p.get("priceUsd", 0) or 0)
+  317         pc = p.get("priceChange", {})
+  318         dex_id = p.get("dexId", "unknown")
+  319 
+  320         # Determine age
+  321         pair_created = p.get("pairCreatedAt", "")
+  322         hours_old = None
+  323         if pair_created:
+  324             try:
+  325                 created_ts = int(pair_created) / 1000
+  326                 hours_old = round((datetime.now().timestamp() - created_ts) / 3600, 1)
+  327             except:
+  328                 pass
+  329 
+  330         # Categorize market cap tier
+  331         if mc < 100000:
+  332             mc_tier = "micro (<100k)"
+  333         elif mc < 500000:
+  334             mc_tier = "small (100k-500k)"
+  335         elif mc < 2000000:
+  336             mc_tier = "mid (500k-2M)"
+  337         elif mc < 5000000:
+  338             mc_tier = "large (2M-5M)"
+  339         else:
+  340             mc_tier = "mega (5M+)"
+  341 
+  342         # Categorize volume tier
+  343         avg_hourly = vol_24h / 24 if vol_24h > 0 else 0
+  344         vol_spike = round(vol_1h / max(avg_hourly, 1), 2) if avg_hourly > 0 else 0
+  345         if vol_spike >= 5:
+  346             vol_tier = "extreme_spike"
+  347         elif vol_spike >= 3:
+  348             vol_tier = "high_spike"
+  349         elif vol_spike >= 1.5:
+  350             vol_tier = "moderate"
+  351         else:
+  352             vol_tier = "normal"
+  353 
+  354         # Categorize buy pressure
+  355         if buy_ratio_1h >= 2.0:
+  356             pressure_tier = "heavy_buying"
+  357         elif buy_ratio_1h >= 1.5:
+  358             pressure_tier = "strong_buying"
+  359         elif buy_ratio_1h >= 1.0:
+  360             pressure_tier = "balanced"
+  361         else:
+  362             pressure_tier = "selling_pressure"
+  363 
+  364         return {
+  365             "price": price,
+  366             "market_cap": mc,
+  367             "mc_tier": mc_tier,
+  368             "liquidity": lq,
+  369             "liq_to_mc_ratio": round(lq / max(mc, 1), 4),
+  370             "volume_1h": vol_1h,
+  371             "volume_24h": vol_24h,
+  372             "vol_spike": vol_spike,
+  373             "vol_tier": vol_tier,
+  374             "buys_1h": buys_1h,
+  375             "sells_1h": sells_1h,
+  376             "buy_ratio_1h": buy_ratio_1h,
+  377             "pressure_tier": pressure_tier,
+  378             "price_change_1h": float(pc.get("h1", 0) or 0),
+  379             "price_change_6h": float(pc.get("h6", 0) or 0),
+  380             "price_change_24h": float(pc.get("h24", 0) or 0),
+  381             "dex": dex_id,
+  382             "hours_old": hours_old
+  383         }
+  384     except:
+  385         return None
+  386 
+  387 
+  388 def fetch_pair_data(token_list):
+  389     results = []
+  390     seen = set()
+  391 
+  392     for item in token_list:
+  393         addr = item["addr"]
+  394         source = item["source"]
+  395         if addr in seen or not addr:
+  396             continue
+  397         seen.add(addr)
+  398         if len(results) >= 12:
+  399             break
+  400 
+  401         try:
+  402             r = requests.get(
+  403                 f"https://api.dexscreener.com/tokens/v1/solana/{addr}",
+  404                 timeout=8
+  405             )
+  406             data = r.json()
+  407             pairs = data if isinstance(data, list) else data.get("pairs", [])
+  408             if not pairs:
+  409                 continue
+  410 
+  411             p = sorted(
+  412                 [x for x in pairs if x],
+  413                 key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0),
+  414                 reverse=True
+  415             )[0]
+  416 
+  417             mc = float(p.get("marketCap", 0) or 0)
+  418             if mc < 5000:
+  419                 continue
+  420 
+  421             pc = p.get("priceChange", {})
+  422             vol = p.get("volume", {})
+  423             lq = p.get("liquidity", {})
+  424             tx_h1 = p.get("txns", {}).get("h1", {})
+  425             tx_h6 = p.get("txns", {}).get("h6", {})
+  426             tx_h24 = p.get("txns", {}).get("h24", {})
+  427 
+  428             buys_1h = int(tx_h1.get("buys", 0) or 0)
+  429             sells_1h = int(tx_h1.get("sells", 0) or 0)
+  430             buys_6h = int(tx_h6.get("buys", 0) or 0)
+  431             sells_6h = int(tx_h6.get("sells", 0) or 0)
+  432             buys_24h = int(tx_h24.get("buys", 0) or 0)
+  433             sells_24h = int(tx_h24.get("sells", 0) or 0)
+  434 
+  435             ratio_1h = round(buys_1h / max(sells_1h, 1), 2)
+  436             ratio_24h = round(buys_24h / max(sells_24h, 1), 2)
+  437 
+  438             vol_1h = float(vol.get("h1", 0) or 0)
+  439             vol_24h = float(vol.get("h24", 0) or 0)
+  440             avg_hourly_vol = vol_24h / 24 if vol_24h > 0 else 0
+  441             vol_spike = round(vol_1h / max(avg_hourly_vol, 1), 2) if avg_hourly_vol > 0 else 0
+  442 
+  443             whale_signals = []
+  444             if ratio_1h >= 2.0:
+  445                 whale_signals.append("HEAVY buy pressure")
+  446             elif ratio_1h >= 1.5:
+  447                 whale_signals.append("Strong buy pressure")
+  448             if vol_spike >= 3.0:
+  449                 whale_signals.append(f"Vol spike {vol_spike}x")
+  450             if mc > 100000 and ratio_1h > 1.3:
+  451                 whale_signals.append("Whale accumulation")
+  452 
+  453             whale_tag = " | WHALE: " + ", ".join(whale_signals) if whale_signals else ""
+  454 
+  455             dex_id = p.get("dexId", "unknown")
+  456             pair_created = p.get("pairCreatedAt", "")
+  457             is_new = ""
+  458             if pair_created:
+  459                 try:
+  460                     created_ts = int(pair_created) / 1000
+  461                     hours_old = (datetime.now().timestamp() - created_ts) / 3600
+  462                     if hours_old < 1:
+  463                         is_new = f" | NEW ({int(hours_old*60)}min old)"
+  464                     elif hours_old < 24:
+  465                         is_new = f" | NEW ({int(hours_old)}h old)"
+  466                     elif hours_old < 72:
+  467                         is_new = f" | RECENT ({int(hours_old/24)}d old)"
+  468                 except:
+  469                     pass
+  470 
+  471             pumpfun_tag = ""
+  472             if dex_id in ("raydium", "orca") and is_new:
+  473                 pumpfun_tag = " | LIKELY PUMP.FUN GRADUATE"
+  474 
+  475             results.append(
+  476                 f"Token: {p.get('baseToken',{}).get('name','?')} "
+  477                 f"({p.get('baseToken',{}).get('symbol','?')})\n"
+  478                 f"Contract: {addr}\n"
+  479                 f"DEX: {dex_id}{is_new}{pumpfun_tag} | Found via: {source}\n"
+  480                 f"Price: ${p.get('priceUsd','?')}\n"
+  481                 f"Price Change >> 5m: {pc.get('m5','?')}% | 1h: {pc.get('h1','?')}% | "
+  482                 f"6h: {pc.get('h6','?')}% | 24h: {pc.get('h24','?')}%\n"
+  483                 f"Volume >> 1h: ${vol.get('h1','?')} | 6h: ${vol.get('h6','?')} | "
+  484                 f"24h: ${vol.get('h24','?')}\n"
+  485                 f"Volume Spike: {vol_spike}x vs 24h avg\n"
+  486                 f"Txns 1h >> Buys: {buys_1h} | Sells: {sells_1h} | Ratio: {ratio_1h}\n"
+  487                 f"Txns 6h >> Buys: {buys_6h} | Sells: {sells_6h}\n"
+  488                 f"Txns 24h >> Buys: {buys_24h} | Sells: {sells_24h} | Ratio: {ratio_24h}\n"
+  489                 f"Liquidity: ${lq.get('usd','?')} | Market Cap: ${mc:,.0f}\n"
+  490                 f"URL: {p.get('url','?')}"
+  491                 f"{whale_tag}"
+  492             )
+  493         except:
+  494             continue
+  495 
+  496     return results
+  497 
+  498 
+  499 def run_full_scan():
+  500     all_tokens = []
+  501     boosted = fetch_boosted_tokens()
+  502     all_tokens.extend(boosted)
+  503     profiles = fetch_latest_profiles()
+  504     all_tokens.extend(profiles)
+  505     new_pairs = fetch_new_pairs()
+  506     all_tokens.extend(new_pairs)
+  507 
+  508     seen = set()
+  509     unique = []
+  510     for item in all_tokens:
+  511         if item["addr"] not in seen and item["addr"]:
+  512             seen.add(item["addr"])
+  513             unique.append(item)
+  514 
+  515     if not unique:
+  516         return [], {"boosted": 0, "profiles": 0, "new_pairs": 0}
+  517 
+  518     tokens = fetch_pair_data(unique)
+  519     stats = {
+  520         "boosted": len(boosted),
+  521         "profiles": len(profiles),
+  522         "new_pairs": len(new_pairs),
+  523         "unique": len(seen),
+  524         "with_data": len(tokens)
+  525     }
+  526     return tokens, stats
+  527 
+  528 
+  529 # ============================================================
+  530 # PLAYBOOK + PERFORMANCE TRACKING + SELF-LEARNING ENGINE
+  531 # ============================================================
+  532 
+  533 def load_playbook():
+  534     try:
+  535         with open("playbook.json") as f:
+  536             pb = json.load(f)
+  537             # Ensure all V5 fields exist
+  538             pb.setdefault("strategy_rules", [])
+  539             pb.setdefault("avoid_conditions", [])
+  540             pb.setdefault("mistake_log", [])
+  541             pb.setdefault("roi_tiers", {})
+  542             pb.setdefault("trade_memory", [])
+  543             pb.setdefault("pattern_stats", {
+  544                 "by_mc_tier": {},
+  545                 "by_vol_tier": {},
+  546                 "by_pressure_tier": {},
+  547                 "by_age_group": {},
+  548                 "by_source": {}
+  549             })
+  550             return pb
+  551     except:
+  552         return {
+  553             "lessons": [],
+  554             "scans": 0,
+  555             "tokens_seen": [],
+  556             "last_scan": None,
+  557             "active_picks": [],
+  558             "pick_history": [],
+  559             "performance": {
+  560                 "total_picks": 0,
+  561                 "wins": 0,
+  562                 "losses": 0,
+  563                 "neutral": 0,
+  564                 "avg_return_pct": 0,
+  565                 "best_pick": {},
+  566                 "worst_pick": {}
+  567             },
+  568             "win_patterns": [],
+  569             "lose_patterns": [],
+  570             # V5 NEW FIELDS
+  571             "strategy_rules": [],
+  572             "avoid_conditions": [],
+  573             "mistake_log": [],
+  574             "roi_tiers": {},
+  575             "trade_memory": [],
+  576             "pattern_stats": {
+  577                 "by_mc_tier": {},
+  578                 "by_vol_tier": {},
+  579                 "by_pressure_tier": {},
+  580                 "by_age_group": {},
+  581                 "by_source": {}
+  582             }
+  583         }
+  584 
+  585 
+  586 def save_playbook(pb):
+  587     pb["last_scan"] = datetime.now().isoformat()[:16]
+  588     pb["scans"] = pb.get("scans", 0) + 1
+  589     pb["tokens_seen"] = pb.get("tokens_seen", [])[-200:]
+  590     pb["pick_history"] = pb.get("pick_history", [])[-100:]
+  591     pb["active_picks"] = pb.get("active_picks", [])[-20:]
+  592     pb["win_patterns"] = pb.get("win_patterns", [])[-30:]
+  593     pb["lose_patterns"] = pb.get("lose_patterns", [])[-30:]
+  594     pb["strategy_rules"] = pb.get("strategy_rules", [])[-20:]
+  595     pb["avoid_conditions"] = pb.get("avoid_conditions", [])[-20:]
+  596     pb["mistake_log"] = pb.get("mistake_log", [])[-20:]
+  597     pb["trade_memory"] = pb.get("trade_memory", [])[-100:]
+  598     pb["lessons"] = pb.get("lessons", [])[-50:]
+  599     with open("playbook.json", "w") as f:
+  600         json.dump(pb, f, indent=2)
+  601 
+  602 
+  603 def track_tokens(pb, tokens_text):
+  604     seen_list = pb.get("tokens_seen", [])
+  605     for line in tokens_text.split("\n"):
+  606         if line.startswith("Token: "):
+  607             token_name = line.replace("Token: ", "").strip()
+  608             seen_list.append({
+  609                 "name": token_name,
+  610                 "date": datetime.now().isoformat()[:16]
+  611             })
+  612     pb["tokens_seen"] = seen_list[-200:]
+  613 
+  614 
+  615 def save_new_picks(pb, stage1_result):
+  616     """Parse AI picks and save them with FULL market snapshot for future analysis."""
+  617     picks = extract_picks_json(stage1_result)
+  618     if not picks:
+  619         return
+  620 
+  621     active = pb.get("active_picks", [])
+  622     for pick in picks:
+  623         try:
+  624             entry = pick.get("entry_price", "0")
+  625             entry_float = float(str(entry).replace("$", "").replace(",", "").strip())
+  626             if entry_float <= 0:
+  627                 continue
+  628 
+  629             contract = pick.get("contract", "")
+  630 
+  631             # Get full market snapshot at entry time
+  632             market_data = get_token_full_data(contract) if contract else {}
+  633             if not market_data:
+  634                 market_data = {}
+  635 
+  636             active.append({
+  637                 "name": pick.get("name", "Unknown"),
+  638                 "symbol": pick.get("symbol", "?"),
+  639                 "contract": contract,
+  640                 "entry_price": entry_float,
+  641                 "confidence": pick.get("confidence", 5),
+  642                 "reason": pick.get("reason", ""),
+  643                 "picked_at": datetime.now().isoformat()[:16],
+  644                 "scans_tracked": 0,
+  645                 "peak_price": entry_float,
+  646                 "lowest_price": entry_float,
+  647                 # V5: Full market snapshot at entry
+  648                 "entry_snapshot": {
+  649                     "market_cap": market_data.get("market_cap", 0),
+  650                     "mc_tier": market_data.get("mc_tier", "unknown"),
+  651                     "liquidity": market_data.get("liquidity", 0),
+  652                     "liq_to_mc_ratio": market_data.get("liq_to_mc_ratio", 0),
+  653                     "volume_1h": market_data.get("volume_1h", 0),
+  654                     "volume_24h": market_data.get("volume_24h", 0),
+  655                     "vol_spike": market_data.get("vol_spike", 0),
+  656                     "vol_tier": market_data.get("vol_tier", "unknown"),
+  657                     "buy_ratio_1h": market_data.get("buy_ratio_1h", 0),
+  658                     "pressure_tier": market_data.get("pressure_tier", "unknown"),
+  659                     "price_change_1h": market_data.get("price_change_1h", 0),
+  660                     "price_change_6h": market_data.get("price_change_6h", 0),
+  661                     "price_change_24h": market_data.get("price_change_24h", 0),
+  662                     "dex": market_data.get("dex", "unknown"),
+  663                     "hours_old": market_data.get("hours_old"),
+  664                     "source": pick.get("source", "scan")
+  665                 }
+  666             })
+  667         except:
+  668             continue
+  669 
+  670     pb["active_picks"] = active[-20:]
+  671 
+  672 
+  673 def update_pattern_stats(pb, pick, return_pct, result_tag):
+  674     """Update statistical pattern tracking based on trade outcomes."""
+  675     stats = pb.get("pattern_stats", {
+  676         "by_mc_tier": {}, "by_vol_tier": {},
+  677         "by_pressure_tier": {}, "by_age_group": {}, "by_source": {}
+  678     })
+  679     snapshot = pick.get("entry_snapshot", {})
+  680 
+  681     # Helper to update a category
+  682     def update_cat(category_key, tier_value):
+  683         if not tier_value or tier_value == "unknown":
+  684             return
+  685         cat = stats.setdefault(category_key, {})
+  686         tier = cat.setdefault(tier_value, {"total": 0, "wins": 0, "losses": 0, "returns": []})
+  687         tier["total"] += 1
+  688         if return_pct >= 20:
+  689             tier["wins"] += 1
+  690         elif return_pct < -10:
+  691             tier["losses"] += 1
+  692         tier["returns"].append(round(return_pct, 1))
+  693         # Keep last 50 returns per tier
+  694         tier["returns"] = tier["returns"][-50:]
+  695 
+  696     update_cat("by_mc_tier", snapshot.get("mc_tier"))
+  697     update_cat("by_vol_tier", snapshot.get("vol_tier"))
+  698     update_cat("by_pressure_tier", snapshot.get("pressure_tier"))
+  699 
+  700     # Age grouping
+  701     hours_old = snapshot.get("hours_old")
+  702     if hours_old is not None:
+  703         if hours_old < 1:
+  704             age_group = "<1h"
+  705         elif hours_old < 6:
+  706             age_group = "1-6h"
+  707         elif hours_old < 24:
+  708             age_group = "6-24h"
+  709         elif hours_old < 72:
+  710             age_group = "1-3d"
+  711         else:
+  712             age_group = "3d+"
+  713         update_cat("by_age_group", age_group)
+  714 
+  715     update_cat("by_source", snapshot.get("source", "scan"))
+  716 
+  717     pb["pattern_stats"] = stats
+  718 
+  719 
+  720 def save_trade_memory(pb, pick, current_price, return_pct, result_tag):
+  721     """Save detailed trade record for pattern analysis."""
+  722     snapshot = pick.get("entry_snapshot", {})
+  723     memory = pb.get("trade_memory", [])
+  724 
+  725     record = {
+  726         "name": pick.get("name", "?"),
+  727         "symbol": pick.get("symbol", "?"),
+  728         "contract": pick.get("contract", ""),
+  729         "entry_price": pick.get("entry_price", 0),
+  730         "exit_price": current_price,
+  731         "return_pct": round(return_pct, 1),
+  732         "peak_return_pct": round(((pick.get("peak_price", 0) - pick.get("entry_price", 1)) / max(pick.get("entry_price", 1), 0.0000001)) * 100, 1),
+  733         "result": result_tag,
+  734         "confidence_at_pick": pick.get("confidence", 5),
+  735         "reason": pick.get("reason", ""),
+  736         "picked_at": pick.get("picked_at", ""),
+  737         "closed_at": datetime.now().isoformat()[:16],
+  738         "scans_held": pick.get("scans_tracked", 0),
+  739         # Full entry conditions for pattern matching
+  740         "entry_market_cap": snapshot.get("market_cap", 0),
+  741         "entry_mc_tier": snapshot.get("mc_tier", "unknown"),
+  742         "entry_liquidity": snapshot.get("liquidity", 0),
+  743         "entry_liq_ratio": snapshot.get("liq_to_mc_ratio", 0),
+  744         "entry_vol_spike": snapshot.get("vol_spike", 0),
+  745         "entry_vol_tier": snapshot.get("vol_tier", "unknown"),
+  746         "entry_buy_ratio": snapshot.get("buy_ratio_1h", 0),
+  747         "entry_pressure": snapshot.get("pressure_tier", "unknown"),
+  748         "entry_price_change_1h": snapshot.get("price_change_1h", 0),
+  749         "entry_age_hours": snapshot.get("hours_old"),
+  750         "entry_dex": snapshot.get("dex", "unknown")
+  751     }
+  752     memory.append(record)
+  753     pb["trade_memory"] = memory[-100:]
+  754 
+  755 
+  756 def detect_mistakes(pb, pick, return_pct):
+  757     """After a loss, analyze what went wrong and log the mistake."""
+  758     if return_pct >= -10:
+  759         return  # Not a significant loss
+  760 
+  761     snapshot = pick.get("entry_snapshot", {})
+  762     mistakes = pb.get("mistake_log", [])
+  763 
+  764     # Build mistake analysis
+  765     conditions = []
+  766     if snapshot.get("vol_spike", 0) < 1.5:
+  767         conditions.append("low volume spike at entry")
+  768     if snapshot.get("buy_ratio_1h", 0) < 1.0:
+  769         conditions.append("sellers outnumbered buyers at entry")
+  770     if snapshot.get("price_change_1h", 0) > 100:
+  771         conditions.append("token had already pumped 100%+ in 1h before entry")
+  772     if snapshot.get("price_change_24h", 0) > 500:
+  773         conditions.append("token had already pumped 500%+ in 24h")
+  774     if snapshot.get("liq_to_mc_ratio", 0) < 0.05:
+  775         conditions.append("very low liquidity-to-MC ratio (thin exit)")
+  776     if snapshot.get("market_cap", 0) > 5000000:
+  777         conditions.append("MC was over $5M (limited upside)")
+  778 
+  779     hours_old = snapshot.get("hours_old")
+  780     if hours_old and hours_old > 72:
+  781         conditions.append("token was already 3+ days old (not fresh)")
+  782 
+  783     lesson = f"Lost {return_pct:.1f}% on {pick.get('name', '?')}"
+  784     if conditions:
+  785         lesson += f". Warning signs: {', '.join(conditions)}"
+  786     else:
+  787         lesson += ". No obvious warning signs at entry - may be random market conditions"
+  788 
+  789     mistakes.append({
+  790         "date": datetime.now().isoformat()[:10],
+  791         "token": pick.get("name", "?"),
+  792         "return_pct": round(return_pct, 1),
+  793         "confidence_was": pick.get("confidence", "?"),
+  794         "lesson": lesson,
+  795         "conditions": conditions
+  796     })
+  797     pb["mistake_log"] = mistakes[-20:]
+  798 
+  799     # Add to avoid conditions if clear patterns
+  800     avoid = pb.get("avoid_conditions", [])
+  801     if snapshot.get("price_change_1h", 0) > 100:
+  802         rule = f"AVOID tokens already pumped >100% in 1h (lost {return_pct:.1f}% on {pick['name']})"
+  803         if rule not in avoid:
+  804             avoid.append(rule)
+  805     if snapshot.get("buy_ratio_1h", 0) < 1.0:
+  806         rule = f"AVOID tokens with sell pressure (ratio <1.0) (lost {return_pct:.1f}% on {pick['name']})"
+  807         if rule not in avoid:
+  808             avoid.append(rule)
+  809     if snapshot.get("liq_to_mc_ratio", 0) < 0.05:
+  810         rule = f"AVOID tokens with liq/MC ratio <5% (thin liquidity trap, lost on {pick['name']})"
+  811         if rule not in avoid:
+  812             avoid.append(rule)
+  813     pb["avoid_conditions"] = avoid[-20:]
+  814 
+  815 
+  816 def update_roi_tiers(pb):
+  817     """Analyze trade memory to find which setup types produce the best ROI."""
+  818     memory = pb.get("trade_memory", [])
+  819     if len(memory) < 3:
+  820         return
+  821 
+  822     tiers = {}
+  823 
+  824     # Analyze by MC tier
+  825     by_mc = {}
+  826     for trade in memory:
+  827         tier = trade.get("entry_mc_tier", "unknown")
+  828         if tier == "unknown":
+  829             continue
+  830         by_mc.setdefault(tier, []).append(trade["return_pct"])
+  831 
+  832     for tier, returns in by_mc.items():
+  833         if len(returns) >= 2:
+  834             tiers[f"MC_{tier}"] = {
+  835                 "avg_roi": round(sum(returns) / len(returns), 1),
+  836                 "count": len(returns),
+  837                 "win_rate": round(len([r for r in returns if r >= 20]) / len(returns) * 100, 1)
+  838             }
+  839 
+  840     # Analyze by volume tier
+  841     by_vol = {}
+  842     for trade in memory:
+  843         tier = trade.get("entry_vol_tier", "unknown")
+  844         if tier == "unknown":
+  845             continue
+  846         by_vol.setdefault(tier, []).append(trade["return_pct"])
+  847 
+  848     for tier, returns in by_vol.items():
+  849         if len(returns) >= 2:
+  850             tiers[f"Vol_{tier}"] = {
+  851                 "avg_roi": round(sum(returns) / len(returns), 1),
+  852                 "count": len(returns),
+  853                 "win_rate": round(len([r for r in returns if r >= 20]) / len(returns) * 100, 1)
+  854             }
+  855 
+  856     # Analyze by buy pressure tier
+  857     by_pressure = {}
+  858     for trade in memory:
+  859         tier = trade.get("entry_pressure", "unknown")
+  860         if tier == "unknown":
+  861             continue
+  862         by_pressure.setdefault(tier, []).append(trade["return_pct"])
+  863 
+  864     for tier, returns in by_pressure.items():
+  865         if len(returns) >= 2:
+  866             tiers[f"Pressure_{tier}"] = {
+  867                 "avg_roi": round(sum(returns) / len(returns), 1),
+  868                 "count": len(returns),
+  869                 "win_rate": round(len([r for r in returns if r >= 20]) / len(returns) * 100, 1)
+  870             }
+  871 
+  872     # Analyze by age group
+  873     by_age = {}
+  874     for trade in memory:
+  875         hours = trade.get("entry_age_hours")
+  876         if hours is None:
+  877             continue
+  878         if hours < 1:
+  879             group = "<1h"
+  880         elif hours < 6:
+  881             group = "1-6h"
+  882         elif hours < 24:
+  883             group = "6-24h"
+  884         else:
+  885             group = "24h+"
+  886         by_age.setdefault(group, []).append(trade["return_pct"])
+  887 
+  888     for group, returns in by_age.items():
+  889         if len(returns) >= 2:
+  890             tiers[f"Age_{group}"] = {
+  891                 "avg_roi": round(sum(returns) / len(returns), 1),
+  892                 "count": len(returns),
+  893                 "win_rate": round(len([r for r in returns if r >= 20]) / len(returns) * 100, 1)
+  894             }
+  895 
+  896     pb["roi_tiers"] = tiers
+  897 
+  898 
+  899 def generate_strategy_rules(pb):
+  900     """Auto-generate strategy rules from trade data using AI."""
+  901     memory = pb.get("trade_memory", [])
+  902     if len(memory) < 5:
+  903         return  # Need enough data
+  904 
+  905     stats = pb.get("pattern_stats", {})
+  906     roi_tiers = pb.get("roi_tiers", {})
+  907 
+  908     # Build a concise data summary for the AI
+  909     summary = "TRADE HISTORY SUMMARY:\n"
+  910     summary += f"Total trades: {len(memory)}\n"
+  911 
+  912     wins = [t for t in memory if t["return_pct"] >= 20]
+  913     losses = [t for t in memory if t["return_pct"] < -10]
+  914 
+  915     if wins:
+  916         summary += f"\nWINNING TRADES ({len(wins)}):\n"
+  917         for w in wins[-10:]:
+  918             summary += (
+  919                 f"- {w['name']}: +{w['return_pct']}% | MC: ${w.get('entry_market_cap',0):,.0f} "
+  920                 f"({w.get('entry_mc_tier','?')}) | Vol spike: {w.get('entry_vol_spike',0)}x | "
+  921                 f"Buy ratio: {w.get('entry_buy_ratio',0)} | Age: {w.get('entry_age_hours','?')}h | "
+  922                 f"1h change at entry: {w.get('entry_price_change_1h',0)}%\n"
+  923             )
+  924 
+  925     if losses:
+  926         summary += f"\nLOSING TRADES ({len(losses)}):\n"
+  927         for l in losses[-10:]:
+  928             summary += (
+  929                 f"- {l['name']}: {l['return_pct']}% | MC: ${l.get('entry_market_cap',0):,.0f} "
+  930                 f"({l.get('entry_mc_tier','?')}) | Vol spike: {l.get('entry_vol_spike',0)}x | "
+  931                 f"Buy ratio: {l.get('entry_buy_ratio',0)} | Age: {l.get('entry_age_hours','?')}h | "
+  932                 f"1h change at entry: {l.get('entry_price_change_1h',0)}%\n"
+  933             )
+  934 
+  935     if roi_tiers:
+  936         summary += "\nROI BY CATEGORY:\n"
+  937         sorted_tiers = sorted(roi_tiers.items(), key=lambda x: x[1]["avg_roi"], reverse=True)
+  938         for name, data in sorted_tiers:
+  939             summary += f"- {name}: avg ROI {data['avg_roi']:+.1f}%, win rate {data['win_rate']}% ({data['count']} trades)\n"
+  940 
+  941     system = """You are a quantitative trading analyst. Based on the trade history data below,
+  942 generate exactly 5-8 SPECIFIC, ACTIONABLE strategy rules that this scanner should follow.
+  943 
+  944 Each rule should be data-backed. Format:
+  945 RULE: [specific rule with numbers]
+  946 
+  947 Examples of good rules:
+  948 RULE: Prioritize tokens with MC 500k-2M - these averaged +45% ROI vs +12% for larger caps
+  949 RULE: Require buy ratio >= 1.5 in 1h - trades with ratio <1.3 averaged -15%
+  950 RULE: Avoid tokens already up >200% in 24h - 4/5 of these lost money
+  951 RULE: Best entry is tokens 1-6h old with vol spike >3x
+  952 
+  953 Only output RULE: lines, nothing else."""
+  954 
+  955     result = call_groq(system, summary, temperature=0.3)
+  956     if not result:
+  957         return
+  958 
+  959     # Parse rules
+  960     new_rules = []
+  961     for line in result.split("\n"):
+  962         line = line.strip()
+  963         if line.startswith("RULE:"):
+  964             rule_text = line[5:].strip()
+  965             if rule_text:
+  966                 new_rules.append(rule_text)
+  967 
+  968     if new_rules:
+  969         pb["strategy_rules"] = new_rules[:10]
+  970 
+  971 
+  972 def check_past_picks(pb):
+  973     """Check current prices of all active picks and update performance with full trade memory."""
+  974     active = pb.get("active_picks", [])
+  975     if not active:
+  976         return None
+  977 
+  978     still_active = []
+  979     report_lines = []
+  980     performance = pb.get("performance", {
+  981         "total_picks": 0, "wins": 0, "losses": 0, "neutral": 0,
+  982         "avg_return_pct": 0, "best_pick": {}, "worst_pick": {}
+  983     })
+  984     history = pb.get("pick_history", [])
+  985 
+  986     for pick in active:
+  987         contract = pick.get("contract", "")
+  988         if not contract:
+  989             continue
+  990 
+  991         current_price = get_token_price(contract)
+  992         if current_price is None:
+  993             pick["scans_tracked"] = pick.get("scans_tracked", 0) + 1
+  994             if pick["scans_tracked"] >= 12:
+  995                 return_pct = -100.0
+  996                 result_tag = "DEAD/RUGGED"
+  997 
+  998                 # V5: Save to trade memory
+  999                 save_trade_memory(pb, pick, 0, return_pct, result_tag)
+ 1000                 update_pattern_stats(pb, pick, return_pct, result_tag)
+ 1001                 detect_mistakes(pb, pick, return_pct)
+ 1002 
+ 1003                 result = {
+ 1004                     **pick,
+ 1005                     "final_price": 0,
+ 1006                     "return_pct": return_pct,
+ 1007                     "result": result_tag,
+ 1008                     "closed_at": datetime.now().isoformat()[:16]
+ 1009                 }
+ 1010                 history.append(result)
+ 1011                 performance["total_picks"] += 1
+ 1012                 performance["losses"] += 1
+ 1013 
+ 1014                 pb["lose_patterns"].append(
+ 1015                     f"{pick['name']}: Token went dead/unreachable after {pick['scans_tracked']} scans. "
+ 1016                     f"Reason picked: {pick.get('reason', '?')}"
+ 1017                 )
+ 1018                 report_lines.append(f"\U0001f480 {pick['name']} - DEAD/RUGGED (-100%)")
+ 1019             else:
+ 1020                 still_active.append(pick)
+ 1021             continue
+ 1022 
+ 1023         entry_price = pick.get("entry_price", 0)
+ 1024         if entry_price <= 0:
+ 1025             still_active.append(pick)
+ 1026             continue
+ 1027 
+ 1028         return_pct = round(((current_price - entry_price) / entry_price) * 100, 1)
+ 1029         pick["scans_tracked"] = pick.get("scans_tracked", 0) + 1
+ 1030 
+ 1031         # Track peak and lowest
+ 1032         if current_price > pick.get("peak_price", 0):
+ 1033             pick["peak_price"] = current_price
+ 1034         if current_price < pick.get("lowest_price", float('inf')):
+ 1035             pick["lowest_price"] = current_price
+ 1036 
+ 1037         peak_return = round(((pick["peak_price"] - entry_price) / entry_price) * 100, 1)
+ 1038 
+ 1039         # Check if pick should be closed (after ~3 hours / 12 scans at 15min)
+ 1040         if pick["scans_tracked"] >= 12:
+ 1041             result_tag = ""
+ 1042             if return_pct >= 100:
+ 1043                 result_tag = "BIG WIN (2x+)"
+ 1044                 performance["wins"] = performance.get("wins", 0) + 1
+ 1045                 pb.setdefault("win_patterns", []).append(
+ 1046                     f"{pick['name']}: +{return_pct}% in {pick['scans_tracked']} scans. "
+ 1047                     f"Peak was +{peak_return}%. Reason: {pick.get('reason', '?')}"
+ 1048                 )
+ 1049             elif return_pct >= 20:
+ 1050                 result_tag = "SMALL WIN"
+ 1051                 performance["wins"] = performance.get("wins", 0) + 1
+ 1052                 pb.setdefault("win_patterns", []).append(
+ 1053                     f"{pick['name']}: +{return_pct}% (small win). Reason: {pick.get('reason', '?')}"
+ 1054                 )
+ 1055             elif return_pct >= -10:
+ 1056                 result_tag = "NEUTRAL"
+ 1057                 performance["neutral"] = performance.get("neutral", 0) + 1
+ 1058             else:
+ 1059                 result_tag = "LOSS"
+ 1060                 performance["losses"] = performance.get("losses", 0) + 1
+ 1061                 pb.setdefault("lose_patterns", []).append(
+ 1062                     f"{pick['name']}: {return_pct}% loss. Peak was +{peak_return}%. "
+ 1063                     f"Reason picked: {pick.get('reason', '?')}"
+ 1064                 )
+ 1065 
+ 1066             performance["total_picks"] = performance.get("total_picks", 0) + 1
+ 1067 
+ 1068             # V5: Save to trade memory with full details
+ 1069             save_trade_memory(pb, pick, current_price, return_pct, result_tag)
+ 1070             update_pattern_stats(pb, pick, return_pct, result_tag)
+ 1071 
+ 1072             # V5: Detect mistakes on losses
+ 1073             if return_pct < -10:
+ 1074                 detect_mistakes(pb, pick, return_pct)
+ 1075 
+ 1076             # Update best/worst
+ 1077             best = performance.get("best_pick", {})
+ 1078             if not best or return_pct > best.get("return_pct", -999):
+ 1079                 performance["best_pick"] = {"name": pick["name"], "return_pct": return_pct}
+ 1080 
+ 1081             worst = performance.get("worst_pick", {})
+ 1082             if not worst or return_pct < worst.get("return_pct", 999):
+ 1083                 performance["worst_pick"] = {"name": pick["name"], "return_pct": return_pct}
+ 1084 
+ 1085             # Update average return
+ 1086             total = performance["total_picks"]
+ 1087             old_avg = performance.get("avg_return_pct", 0)
+ 1088             performance["avg_return_pct"] = round(
+ 1089                 ((old_avg * (total - 1)) + return_pct) / total, 1
+ 1090             )
+ 1091 
+ 1092             result = {
+ 1093                 **pick,
+ 1094                 "final_price": current_price,
+ 1095                 "return_pct": return_pct,
+ 1096                 "peak_return_pct": peak_return,
+ 1097                 "result": result_tag,
+ 1098                 "closed_at": datetime.now().isoformat()[:16]
+ 1099             }
+ 1100             history.append(result)
+ 1101 
+ 1102             emoji = "\U0001f7e2" if return_pct > 20 else "\U0001f534" if return_pct < -10 else "\u26aa"
+ 1103             conf = pick.get("confidence", "?")
+ 1104             report_lines.append(
+ 1105                 f"{emoji} {pick['name']} ({pick['symbol']}): {return_pct:+.1f}% "
+ 1106                 f"(peak: +{peak_return}%) | Conf was: {conf}/10 - {result_tag}"
+ 1107             )
+ 1108         else:
+ 1109             # Still tracking
+ 1110             emoji = "\U0001f4c8" if return_pct > 0 else "\U0001f4c9"
+ 1111             report_lines.append(
+ 1112                 f"{emoji} {pick['name']} ({pick['symbol']}): {return_pct:+.1f}% "
+ 1113                 f"(peak: +{peak_return}%) - tracking ({pick['scans_tracked']}/12)"
+ 1114             )
+ 1115             still_active.append(pick)
+ 1116 
+ 1117     pb["active_picks"] = still_active
+ 1118     pb["pick_history"] = history[-100:]
+ 1119     pb["performance"] = performance
+ 1120 
+ 1121     if report_lines:
+ 1122         total = performance.get("total_picks", 0)
+ 1123         wins = performance.get("wins", 0)
+ 1124         win_rate = round((wins / max(total, 1)) * 100, 1) if total > 0 else 0
+ 1125         avg_ret = performance.get("avg_return_pct", 0)
+ 1126 
+ 1127         # V5: Add ROI tier summary
+ 1128         roi_summary = ""
+ 1129         roi_tiers = pb.get("roi_tiers", {})
+ 1130         if roi_tiers:
+ 1131             best_tier = max(roi_tiers.items(), key=lambda x: x[1]["avg_roi"])
+ 1132             worst_tier = min(roi_tiers.items(), key=lambda x: x[1]["avg_roi"])
+ 1133             roi_summary = (
+ 1134                 f"\nBest setup type: {best_tier[0]} (avg {best_tier[1]['avg_roi']:+.1f}%)"
+ 1135                 f"\nWorst setup type: {worst_tier[0]} (avg {worst_tier[1]['avg_roi']:+.1f}%)"
+ 1136             )
+ 1137 
+ 1138         # V5: Add mistake count
+ 1139         mistakes = pb.get("mistake_log", [])
+ 1140         rules = pb.get("strategy_rules", [])
+ 1141 
+ 1142         header = (
+ 1143             f"\U0001f4cb PICK TRACKER UPDATE\n"
+ 1144             f"{'='*35}\n"
+ 1145             f"Active picks: {len(still_active)} | Completed: {total}\n"
+ 1146             f"Win rate: {win_rate}% | Avg return: {avg_ret:+.1f}%\n"
+ 1147             f"Strategy rules: {len(rules)} | Mistakes logged: {len(mistakes)}"
+ 1148             f"{roi_summary}\n"
+ 1149             f"{'='*35}\n\n"
+ 1150         )
+ 1151         return header + "\n".join(report_lines)
+ 1152     return None
+ 1153 
+ 1154 
+ 1155 # ============================================================
+ 1156 # AI CALLS
+ 1157 # ============================================================
+ 1158 
+ 1159 def call_groq(system, prompt, temperature=0.8):
+ 1160     try:
+ 1161         resp = requests.post(
+ 1162             "https://api.groq.com/openai/v1/chat/completions",
+ 1163             headers={
+ 1164                 "Authorization": f"Bearer {GROQ_KEY}",
+ 1165                 "Content-Type": "application/json"
+ 1166             },
+ 1167             json={
+ 1168                 "model": "llama-3.3-70b-versatile",
+ 1169                 "messages": [
+ 1170                     {"role": "system", "content": system},
+ 1171                     {"role": "user", "content": prompt}
+ 1172                 ],
+ 1173                 "temperature": temperature,
+ 1174                 "max_tokens": 4096
+ 1175             },
+ 1176             timeout=60
+ 1177         )
+ 1178         result = resp.json()
+ 1179         return result["choices"][0]["message"]["content"]
+ 1180     except:
+ 1181         return None
+ 1182 
+ 1183 
+ 1184 def extract_picks_json(response):
+ 1185     try:
+ 1186         match = re.search(r'```json\s*(\[.*?\])\s*```', response, re.DOTALL)
+ 1187         if match:
+ 1188             return json.loads(match.group(1))
+ 1189         match = re.search(r'\[.*\]', response, re.DOTALL)
+ 1190         if match:
+ 1191             return json.loads(match.group(0))
+ 1192     except:
+ 1193         pass
+ 1194     return None
+ 1195 
+ 1196 
+ 1197 # ============================================================
+ 1198 # MAIN
+ 1199 # ============================================================
+ 1200 
+ 1201 def main():
+ 1202     playbook = load_playbook()
+ 1203     scan_num = playbook.get("scans", 0) + 1
+ 1204     perf = playbook.get("performance", {})
+ 1205     total_picks = perf.get("total_picks", 0)
+ 1206     win_rate = round((perf.get("wins", 0) / max(total_picks, 1)) * 100, 1) if total_picks > 0 else 0
+ 1207     rules_count = len(playbook.get("strategy_rules", []))
+ 1208     memory_count = len(playbook.get("trade_memory", []))
+ 1209 
+ 1210     send_msg(
+ 1211         f"\U0001f50d Scan #{scan_num} starting...\n"
+ 1212         f"\U0001f4e1 Sources: DEXScreener Boosted + Profiles + New/PumpFun\n"
+ 1213         f"\U0001f9e0 Brain: {len(playbook.get('lessons', []))} patterns | "
+ 1214         f"Track record: {total_picks} picks, {win_rate}% win rate\n"
+ 1215         f"\U0001f4ca Trade memory: {memory_count} detailed records | "
+ 1216         f"Strategy rules: {rules_count}\n"
+ 1217         f"\u23f3 Self-learning scan with dynamic strategy..."
+ 1218     )
+ 1219 
+ 1220     # ---- STEP 1: Check past picks (with full trade memory) ----
+ 1221     tracker_report = check_past_picks(playbook)
+ 1222     if tracker_report:
+ 1223         send_msg(tracker_report)
+ 1224 
+ 1225     # ---- STEP 1.5: Update ROI tiers and strategy rules ----
+ 1226     update_roi_tiers(playbook)
+ 1227 
+ 1228     # Generate dynamic strategy rules every 5 scans (once enough data)
+ 1229     if scan_num % 5 == 0 and len(playbook.get("trade_memory", [])) >= 5:
+ 1230         send_msg("\U0001f504 Updating strategy rules from trade data...")
+ 1231         generate_strategy_rules(playbook)
+ 1232         rules = playbook.get("strategy_rules", [])
+ 1233         if rules:
+ 1234             rules_text = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(rules))
+ 1235             send_msg(f"\U0001f4dd New strategy rules:\n{rules_text}")
+ 1236 
+ 1237     # ---- STEP 2: Gather new data ----
+ 1238     tokens, stats = run_full_scan()
+ 1239 
+ 1240     if not tokens:
+ 1241         send_msg(
+ 1242             f"\U0001f634 No trending Solana tokens right now.\n"
+ 1243             f"Sources: Boosted ({stats.get('boosted', 0)}) | "
+ 1244             f"Profiles ({stats.get('profiles', 0)}) | "
+ 1245             f"New/PumpFun ({stats.get('new_pairs', 0)})\n"
+ 1246             "Checking again in 15 min."
+ 1247         )
+ 1248         save_playbook(playbook)
+ 1249         return
+ 1250 
+ 1251     token_data = "\n\n---\n\n".join(tokens)
+ 1252     track_tokens(playbook, token_data)
+ 1253 
+ 1254     send_msg(
+ 1255         f"\U0001f4ca Found {len(tokens)} tokens\n"
+ 1256         f"Boosted: {stats.get('boosted', 0)} | Profiles: {stats.get('profiles', 0)} | "
+ 1257         f"New/PumpFun: {stats.get('new_pairs', 0)}\n"
+ 1258         f"\U0001f52c Stage 1: Pattern-aware quick scan..."
+ 1259     )
+ 1260 
+ 1261     # ---- STEP 3: Stage 1 - Quick scan (now uses dynamic strategy rules) ----
+ 1262     scan_prompt = build_scan_prompt(playbook)
+ 1263     stage1_result = call_groq(scan_prompt, f"Analyze:\n\n{token_data}", temperature=0.5)
+ 1264 
+ 1265     if not stage1_result:
+ 1266         send_msg("\u26a0\ufe0f Stage 1 failed. Retrying next cycle.")
+ 1267         save_playbook(playbook)
+ 1268         return
+ 1269 
+ 1270     # Save picks for tracking (with full market snapshots)
+ 1271     save_new_picks(playbook, stage1_result)
+ 1272 
+ 1273     picks = extract_picks_json(stage1_result)
+ 1274     if picks:
+ 1275         picks_summary = "\n".join(
+ 1276             f"#{p.get('rank','?')}: {p.get('name','?')} ({p.get('symbol','?')}) "
+ 1277             f"[Conf: {p.get('confidence','?')}/10] - {p.get('reason','?')}"
+ 1278             for p in picks
+ 1279         )
+ 1280         send_msg(
+ 1281             f"\U0001f3af Stage 1 picks:\n{picks_summary}\n\n"
+ 1282             f"\U0001f52c Stage 2: Deep research with full trade intelligence..."
+ 1283         )
+ 1284     else:
+ 1285         picks_summary = stage1_result[:500]
+ 1286         send_msg("\U0001f3af Picks identified. Running deep research...")
+ 1287 
+ 1288     # ---- STEP 4: Stage 2 - Deep research (with full self-learning context) ----
+ 1289     research_prompt = build_research_prompt(playbook)
+ 1290     deep_prompt = (
+ 1291         f"Top picks:\n{picks_summary}\n\n"
+ 1292         f"Full token data:\n\n{token_data}\n\n"
+ 1293         f"Do DEEP RESEARCH on each pick. Reference your real track record, "
+ 1294         f"strategy rules, and trade memory. Flag any picks that match your "
+ 1295         f"losing patterns or avoid conditions."
+ 1296     )
+ 1297 
+ 1298     research = call_groq(research_prompt, deep_prompt, temperature=0.85)
+ 1299 
+ 1300     if research:
+ 1301         # Extract and save lessons
+ 1302         if "PLAYBOOK" in research and "UPDATE" in research:
+ 1303             try:
+ 1304                 note = research.split("UPDATE")[-1]
+ 1305                 if "STRATEGY" in note:
+ 1306                     note = note.split("STRATEGY")[0]
+ 1307                 elif "MISTAKE" in note:
+ 1308                     note = note.split("MISTAKE")[0]
+ 1309                 elif "SELF-REFLECTION" in note:
+ 1310                     note = note.split("SELF-REFLECTION")[0]
+ 1311                 note = note.strip(": \n")[:500]
+ 1312                 playbook.setdefault("lessons", []).append({
+ 1313                     "date": datetime.now().isoformat()[:10],
+ 1314                     "note": note
+ 1315                 })
+ 1316                 playbook["lessons"] = playbook["lessons"][-50:]
+ 1317             except:
+ 1318                 pass
+ 1319 
+ 1320         active_count = len(playbook.get("active_picks", []))
+ 1321         rules_count = len(playbook.get("strategy_rules", []))
+ 1322         mistakes_count = len(playbook.get("mistake_log", []))
+ 1323         memory_count = len(playbook.get("trade_memory", []))
+ 1324 
+ 1325         header = (
+ 1326             f"\U0001f4ca SCAN #{scan_num} - DEEP RESEARCH COMPLETE\n"
+ 1327             f"{'='*40}\n"
+ 1328             f"Tokens scanned: {len(tokens)} | Active picks: {active_count}\n"
+ 1329             f"Track record: {total_picks} picks | {win_rate}% win rate\n"
+ 1330             f"Brain: {len(playbook.get('lessons', []))} patterns | "
+ 1331             f"Trade memory: {memory_count}\n"
+ 1332             f"Strategy rules: {rules_count} | Mistakes logged: {mistakes_count}\n"
+ 1333             f"{'='*40}\n\n"
+ 1334         )
+ 1335         send_msg(header + research)
+ 1336     else:
+ 1337         send_msg("\u26a0\ufe0f Deep research failed. Stage 1 picks above still valid.")
+ 1338 
+ 1339     save_playbook(playbook)
+ 1340 
+ 1341 
+ 1342 if __name__ == "__main__":
+ 1343     main()
