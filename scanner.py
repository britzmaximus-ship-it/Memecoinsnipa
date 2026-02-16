import os, re, json, requests
from datetime import datetime

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
USER_ID = os.environ.get("TELEGRAM_USER_ID", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

TAPI = f"https://api.telegram.org/bot{BOT_TOKEN}"

SYSTEM_PROMPT = """Meme coin trading AI for pump.fun plays. Actionable, decisive.
Hunt Solana memecoins for biggest profit opportunities. Be decisive with PICKS.
Use LIVE DEXScreener data injected below. Present top plays with:
- Entry zones, targets (2-3x, 5-10x, moonbag 10x+), exit signals
- Time outlook and risks
Skip only: confirmed rugs, dead volume. Present everything else.
After each response add: PLAYBOOK UPDATE: (1-3 lessons learned)
Not financial advice."""


def send_msg(text):
    for i in range(0, len(text), 4000):
        try:
            requests.post(f"{TAPI}/sendMessage", json={
                "chat_id": USER_ID, "text": text[i:i+4000]
            }, timeout=10)
        except:
            pass


def fetch_trending():
    try:
        r = requests.get(
            "https://api.dexscreener.com/token-boosts/top/v1",
            timeout=10
        )
        tokens = r.json()[:10]
        results = []
        for t in tokens:
            addr = t.get("tokenAddress", "")
            chain = t.get("chainId", "solana")
            if chain != "solana":
                continue
            try:
                r2 = requests.get(
                    f"https://api.dexscreener.com/tokens/v1/{addr}",
                    timeout=8
                )
                pairs = r2.json()
                if isinstance(pairs, dict):
                    pairs = pairs.get("pairs", [])
                if not pairs:
                    continue
                p = sorted(
                    [x for x in pairs if x],
                    key=lambda x: float(
                        x.get("liquidity", {}).get("usd", 0) or 0
                    ),
                    reverse=True
                )[0]
                pc = p.get("priceChange", {})
                tx = p.get("txns", {}).get("h24", {})
                lq = p.get("liquidity", {})
                mc = p.get("marketCap", 0)
                if float(mc or 0) < 5000:
                    continue
                results.append(
                    f"Token: {p.get('baseToken',{}).get('name','?')} "
                    f"({p.get('baseToken',{}).get('symbol','?')})\n"
                    f"Chain: {p.get('chainId','?')} | "
                    f"DEX: {p.get('dexId','?')}\n"
                    f"Contract: {addr}\n"
                    f"Price: ${p.get('priceUsd','?')}\n"
                    f"5m: {pc.get('m5','?')}% | "
                    f"1h: {pc.get('h1','?')}% | "
                    f"24h: {pc.get('h24','?')}%\n"
                    f"Vol: ${p.get('volume',{}).get('h24','?')}\n"
                    f"Buys: {tx.get('buys','?')} | "
                    f"Sells: {tx.get('sells','?')}\n"
                    f"Liq: ${lq.get('usd','?')} | "
                    f"MC: ${mc}\n"
                    f"URL: {p.get('url','?')}"
                )
            except:
                continue
        return results
    except:
        return []


def load_playbook():
    try:
        with open("playbook.json") as f:
            return json.load(f)
    except:
        return {"lessons": [], "scans": 0}


def save_playbook(pb):
    pb["last_scan"] = datetime.now().isoformat()[:16]
    pb["scans"] = pb.get("scans", 0) + 1
    with open("playbook.json", "w") as f:
        json.dump(pb, f, indent=2)


def call_gemini(prompt):
    playbook = load_playbook()
    system = SYSTEM_PROMPT
    if playbook.get("lessons"):
        system += "\n\nLEARNED PLAYBOOK:\n"
        for l in playbook["lessons"][-15:]:
            system += f"- {l.get('note','')}\n"
    try:
        resp = requests.post(
            "https://generativelanguage.googleapis.com"
            "/v1beta/models/gemini-2.0-flash"
            f":generateContent?key={GEMINI_KEY}",
            json={
                "contents": [
                    {"role": "user",
                     "parts": [{"text": prompt}]}
                ],
                "systemInstruction": {
                    "parts": [{"text": system}]
                },
                "generationConfig": {
                    "temperature": 0.9,
                    "maxOutputTokens": 4096
                }
            },
            timeout=60
        )
        result = resp.json()
        if "candidates" not in result:
            return None
        answer = result[
            "candidates"
        ][0]["content"]["parts"][0]["text"]
        if "PLAYBOOK" in answer and "UPDATE" in answer:
            try:
                note = answer.split("UPDATE")[-1]
                note = note.strip(": \n")[:300]
                playbook["lessons"].append({
                    "date": datetime.now().isoformat()[:10],
                    "note": note
                })
                playbook["lessons"] = (
                    playbook["lessons"][-50:]
                )
            except:
                pass
        save_playbook(playbook)
        return answer
    except Exception as e:
        return f"Gemini error: {str(e)[:200]}"


def main():
    send_msg("Scanning for plays...")
    tokens = fetch_trending()
    if not tokens:
        send_msg("No trending Solana tokens found.")
        return
    token_data = "\n\n---\n\n".join(tokens[:5])
    prompt = (
        f"Analyze these trending Solana tokens. "
        f"Pick the TOP 1-3 best plays and explain why.\n"
        f"\n{token_data}"
    )
    analysis = call_gemini(prompt)
    if analysis:
        send_msg(f"SCAN RESULTS\n\n{analysis}")
    else:
        send_msg("Analysis failed. Will retry next scan.")


if __name__ == "__main__":
    main()
