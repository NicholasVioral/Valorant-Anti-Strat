"""
ollama_synth.py
---------------
Feed the extracted G2 Lotus stats digest to a local Ollama model and get
counter-strat synthesis per pattern category. Saves g2_lotus_synthesis.md.
"""

import json
import urllib.request
from pathlib import Path

MODEL = "qwen2.5:14b"
OLLAMA_URL = "http://localhost:11434/api/generate"

d = json.load(open("g2_lotus_stats.json", encoding="utf-8"))


def digest() -> str:
    lines = []
    lines.append("MATCHES ANALYZED (G2 Esports on Lotus):")
    for m in d["matches_analyzed"]:
        pos = "full replay data" if m["has_replay_positions"] else "kill/economy data only"
        lines.append(f"- {m['opponent']}: {m['rounds']} rounds ({pos})")
    lines.append("G2 comp every match: BABYBAY=Vyse, valyn=Omen, leaf=Viper, jawgemo=Raze, trent=Fade")

    lines.append("\n1) ULT CAST LOCATIONS (3 matches with replay data; "
                 "Viper positions corrected to pit CAST location, not exit):")
    for pl, s in d["ult_locations"].items():
        cs = "; ".join(f"{c['callout']}({c['region']}) R{c['round']} {c['side']} {c['match']}"
                       for c in s["casts"])
        lines.append(f"- {pl}, {s['total_casts']} casts: {cs}")

    lines.append("\n2) UNCONVENTIONAL WEAPON PURCHASES (actual buy-phase inventory for 3 matches; "
                 "kill-feed inference for the 2 matches without replay data):")
    for w, pls in d["weird_buys"].items():
        for pl, s in pls.items():
            lines.append(f"- {w} bought by {pl} in {s['rounds']} rounds ({', '.join(s['round_list'])}) "
                         f"| sides {s['sides']} | econ tiers {s['tiers']} "
                         f"| early-round position {s['early_callouts']} "
                         f"| main play area {s['play_areas']} | kill locations {s['kill_callouts']}")

    eco = d["eco_stacks"]
    lines.append(f"\n3) ECO/PISTOL DEFENSE SETUPS ({eco['def_eco_pistol_rounds']} low-buy def rounds "
                 f"in replay matches, {eco['rounds_with_3plus_stack']} had a 3+ stack):")
    for r in eco["detail"]:
        lines.append(f"- {r['match']} R{r['round']} ({r['tier']}): players per zone {r['regions']}, "
                     f"forward/aggressive players: {r['aggressive_players']}, won={r['won']}")

    gen = d["general_stacks"]
    lines.append(f"\n4) GENERAL DEFENSIVE STACKS ({gen['def_rounds_analyzed']} def rounds analyzed, "
                 f"{gen['rounds_with_3plus_stack']} rounds with 3+ players in one zone):")
    lines.append(f"- Stack zone distribution: {json.dumps(gen['stack_regions'])}")
    lines.append(f"- Stacks by econ tier: {json.dumps(gen['stack_by_tier'])}")
    lines.append(f"- Overall player-presence by zone at 15s: {json.dumps(gen['avg_region_spread'])}")

    ex = d["site_execs"]
    lines.append(f"\n5) ATTACK SITE EXECUTES ({ex['atk_rounds']} atk rounds, site known for {ex['site_known']}):")
    lines.append(f"- Site distribution: {json.dumps(ex['by_site'])}")
    lines.append(f"- By econ tier: {json.dumps(ex['by_site_tier'])}")
    lines.append(f"- Pistol rounds: {json.dumps(ex['pistol_sites'])}")
    lines.append(f"- Win rate by site: {json.dumps(ex['win_rate_by_site'])}")
    lines.append(f"- Entry style where measurable: {json.dumps(ex['style'])}; "
                 f"approach lanes used: {json.dumps(ex['common_approaches'])}")
    lines.append(f"- Average execute commit time: {ex['avg_exec_time_s']}s into the round")
    return "\n".join(lines)


PROMPT = f"""You are a professional Valorant analyst preparing an anti-strat brief on G2 Esports' Lotus.
Below are hard statistics mined from 5 of their recent Lotus matches.

{digest()}

Write a counter-strat brief with EXACTLY these five sections, using these exact markdown headers:

## 1. Ultimate Locations
## 2. Unconventional Buys
## 3. Eco Stacks
## 4. General Stacks
## 5. Site Executes

In each section give 2-4 bullet points. Each bullet must follow this format:
- **FINDING:** <the tendency, with the supporting numbers> **STRENGTH:** <STRONG / MODERATE / WEAK based on sample size and consistency> **COUNTER:** <one specific, actionable defensive adjustment or exploit>

Rules:
- Use only the data above; never invent numbers.
- Lotus-specific tactical language (callouts, agent abilities) is encouraged.
- STRONG = consistent across 3+ instances/matches; MODERATE = 2-3 instances; WEAK = small sample.
- Be specific in counters: name the callout to play, the timing, the utility to use.
"""


def main():
    print(f"Prompt length: {len(PROMPT)} chars. Querying {MODEL} ...")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps({
            "model": MODEL,
            "prompt": PROMPT,
            "stream": False,
            "options": {"temperature": 0.3, "num_ctx": 8192, "num_predict": 2500},
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=900) as resp:
        out = json.loads(resp.read())
    text = out.get("response", "")
    Path("g2_lotus_synthesis.md").write_text(text, encoding="utf-8")
    print(f"Saved g2_lotus_synthesis.md ({len(text)} chars)")
    print("\n" + text)


if __name__ == "__main__":
    main()
