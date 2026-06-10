"""
build_report.py
---------------
Render g2_lotus_counterstrats.html from g2_lotus_stats.json +
g2_lotus_synthesis.md (qwen2.5:14b). Self-contained dark-theme report.
"""

import base64
import json
import html as _html
from pathlib import Path

d = json.load(open("g2_lotus_stats.json", encoding="utf-8"))
synthesis_raw = Path("g2_lotus_synthesis.md").read_text(encoding="utf-8")
img_b64 = base64.b64encode(Path("map_cache/lotus.png").read_bytes()).decode()

COLORS = {
    "BABYBAY": "#f0c040", "valyn": "#4da6ff", "leaf": "#3ecf8e",
    "jawgemo": "#e8383d", "trent": "#9b59b6",
}


def esc(s):
    return _html.escape(str(s))


def badge(level):
    cls = {"STRONG": "b-strong", "MODERATE": "b-mod", "WEAK": "b-weak"}[level]
    return f'<span class="badge {cls}">{level}</span>'


def bar(pct, color="#e8383d"):
    return (f'<div class="bar-bg"><div class="bar-fill" '
            f'style="width:{pct}%;background:{color}"></div></div>'
            f'<span class="bar-num">{pct}%</span>')


# ── ult map markers ───────────────────────────────────────────────────────────

def ult_markers():
    parts = []
    for pl, s in d["ult_locations"].items():
        ign = pl.split(" (")[0]
        color = COLORS.get(ign, "#fff")
        for c in s["casts"]:
            shape = "border-radius:50%" if c["side"] == "atk" else "border-radius:2px"
            vc = " · pit cast (corrected)" if c.get("viper_corrected") else ""
            tip_x = "right:18px;" if c["map_px"] > 62 else "left:18px;"
            tip_y = "bottom:0;" if c["map_py"] > 80 else "top:-4px;"
            parts.append(
                f'<div class="dot" style="left:{c["map_px"]}%;top:{c["map_py"]}%;'
                f'background:{color};{shape}">'
                f'<div class="tip" style="{tip_x}{tip_y}">'
                f'<b style="color:{color}">{esc(pl)}</b>'
                f'{esc(c["callout"])} · R{c["round"]} {c["side"].upper()} · '
                f'{c["time_s"]}s · {esc(c["match"])}{vc}</div></div>')
    return "\n".join(parts)


def ult_player_rows():
    rows = []
    for pl, s in d["ult_locations"].items():
        ign = pl.split(" (")[0]
        color = COLORS.get(ign, "#fff")
        tops = list(s["by_callout"].items())
        top_str = ", ".join(f"{esc(k)} ×{v['count']}" for k, v in tops[:3])
        atk = sum(1 for c in s["casts"] if c["side"] == "atk")
        rows.append(
            f'<tr><td><span class="pdot" style="background:{color}"></span>'
            f'<b style="color:{color}">{esc(pl)}</b></td>'
            f'<td class="num">{s["total_casts"]}</td>'
            f'<td class="num">{atk} / {s["total_casts"] - atk}</td>'
            f'<td>{top_str}</td></tr>')
    return "\n".join(rows)


def ult_cast_rows():
    rows = []
    casts = [(pl, c) for pl, s in d["ult_locations"].items() for c in s["casts"]]
    casts.sort(key=lambda x: (x[0], x[1]["match"], x[1]["round"]))
    for pl, c in casts:
        ign = pl.split(" (")[0]
        color = COLORS.get(ign, "#fff")
        vc = ' <span class="note">pit-corrected</span>' if c.get("viper_corrected") else ""
        rows.append(
            f'<tr><td><span class="pdot" style="background:{color}"></span>'
            f'<span style="color:{color}">{esc(pl)}</span></td>'
            f'<td>{esc(c["match"])}</td><td class="num">R{c["round"]}</td>'
            f'<td>{c["side"].upper()}</td><td class="num">{c["time_s"]}s</td>'
            f'<td><b>{esc(c["callout"])}</b> ({esc(c["region"])}){vc}</td></tr>')
    return "\n".join(rows)


# ── weird buys ────────────────────────────────────────────────────────────────

def buys_rows():
    order = ["Judge", "Operator", "Odin", "Outlaw", "Marshal", "Bulldog"]
    rows = []
    for w in order:
        pls = d["weird_buys"].get(w, {})
        for pl, s in sorted(pls.items(), key=lambda x: -x[1]["rounds"]):
            ign = pl.split(" (")[0]
            color = COLORS.get(ign, "#fff")
            spots = ", ".join(f"{esc(k)} ×{v}" for k, v in s["kill_callouts"].items()) or "—"
            early = ", ".join(f"{esc(k)} ×{v}" for k, v in s["early_callouts"].items()) or "—"
            play = ", ".join(f"{esc(k)} ×{v}" for k, v in s["play_areas"].items()) or "—"
            tiers = ", ".join(f"{esc(k)} ×{v}" for k, v in s["tiers"].items())
            sides = ", ".join(f"{esc(k)} ×{v}" for k, v in s["sides"].items())
            hot = ' class="hot-row"' if s["rounds"] >= 4 else ""
            rows.append(
                f'<tr{hot}><td><b>{esc(w)}</b></td>'
                f'<td><span class="pdot" style="background:{color}"></span>'
                f'<span style="color:{color}">{esc(pl)}</span></td>'
                f'<td class="num">{s["rounds"]}</td><td>{sides}</td><td>{tiers}</td>'
                f'<td>{early}</td><td>{play}</td><td>{spots}</td></tr>')
    return "\n".join(rows)


def buy_detail_rows():
    rows = []
    for w, pls in d["weird_buys"].items():
        for pl, s in pls.items():
            ign = pl.split(" (")[0]
            color = COLORS.get(ign, "#fff")
            for x in s["detail"]:
                val = f'{x["loadout_value"]:,}' if x["loadout_value"] else "—"
                src = ("buy phase" if x["source"] == "buy-phase"
                       else '<span class="note">kill feed</span>')
                rows.append(
                    f'<tr><td><b>{esc(w)}</b></td>'
                    f'<td><span style="color:{color}">{esc(x["player"])}</span></td>'
                    f'<td>{esc(x["match"])}</td><td class="num">R{x["round"]}</td>'
                    f'<td>{esc(x["side"])}</td><td>{esc(x["tier"])}</td>'
                    f'<td class="num">{val}</td>'
                    f'<td>{esc(x["early_callout"] or "—")}</td>'
                    f'<td>{esc(x["play_area"] or "—")}</td>'
                    f'<td>{src}</td></tr>')
    return "\n".join(rows)


# ── stacks ────────────────────────────────────────────────────────────────────

def eco_rows():
    rows = []
    for r in d["eco_stacks"]["detail"]:
        regions = ", ".join(f"{esc(k)}: {v}" for k, v in r["regions"].items())
        fwd = ", ".join(esc(p) for p in r["aggressive_players"]) or "none"
        won = "✓" if r["won"] else "✗"
        if r.get("g2_buy"):
            buy = f'{r["g2_buy"]:,} vs {r["opp_buy"]:,}'
        else:
            buy = "—"
        rows.append(
            f'<tr><td>{esc(r["match"])}</td><td class="num">R{r["round"]}</td>'
            f'<td>{esc(r["tier"])}</td><td class="num">{buy}</td>'
            f'<td>{regions}</td><td>{fwd}</td>'
            f'<td class="num">{won}</td></tr>')
    return "\n".join(rows)


def stack_dist_rows():
    gen = d["general_stacks"]
    site_colors = {"A": "#e8383d", "B": "#4da6ff", "C": "#3ecf8e"}
    rows = []
    for reg, v in gen["stack_regions"].items():
        rows.append(f'<tr><td><b>{esc(reg)}</b></td><td class="num">{v["count"]}</td>'
                    f'<td class="barcell">{bar(v["pct"], site_colors.get(reg, "#888"))}</td></tr>')
    return "\n".join(rows)


def presence_rows():
    gen = d["general_stacks"]
    site_colors = {"A": "#e8383d", "B": "#4da6ff", "C": "#3ecf8e",
                   "Defender Side": "#888"}
    rows = []
    for reg, v in gen["avg_region_spread"].items():
        rows.append(f'<tr><td><b>{esc(reg)}</b></td><td class="num">{v["count"]}</td>'
                    f'<td class="barcell">{bar(v["pct"], site_colors.get(reg, "#888"))}</td></tr>')
    return "\n".join(rows)


# ── execs ─────────────────────────────────────────────────────────────────────

def exec_site_rows():
    ex = d["site_execs"]
    site_colors = {"A": "#e8383d", "B": "#4da6ff", "C": "#3ecf8e"}
    rows = []
    for site, v in ex["by_site"].items():
        wr = ex["win_rate_by_site"].get(site, {})
        wr_s = f'{wr.get("won", 0)}/{wr.get("total", 0)}'
        wr_pct = round(wr.get("won", 0) * 100 / wr["total"]) if wr.get("total") else 0
        rows.append(f'<tr><td><b>{esc(site)} site</b></td><td class="num">{v["count"]}</td>'
                    f'<td class="barcell">{bar(v["pct"], site_colors[site])}</td>'
                    f'<td class="num">{wr_s} ({wr_pct}%)</td></tr>')
    return "\n".join(rows)


def exec_tier_rows():
    ex = d["site_execs"]
    rows = []
    for tier in ("pistol", "eco", "semi-eco", "semi-buy", "full"):
        sites = ex["by_site_tier"].get(tier, {})
        if not sites:
            continue
        total = sum(sites.values())
        cells = " · ".join(f'<b>{esc(s)}</b> {n}/{total}'
                           for s, n in sorted(sites.items(), key=lambda x: -x[1]))
        rows.append(f'<tr><td>{esc(tier)}</td><td class="num">{total}</td><td>{cells}</td></tr>')
    return "\n".join(rows)


def exec_detail_rows():
    rows = []
    for e in d["site_execs"]["detail"]:
        if not e["site"]:
            continue
        appr = ", ".join(esc(a) for a in e["approaches"]) or "—"
        t = f'{e["exec_time_s"]}s' if e["exec_time_s"] else "—"
        won = "✓" if e["won"] else "✗"
        site_cls = {"A": "site-a", "B": "site-b", "C": "site-c"}.get(e["site"], "")
        rows.append(
            f'<tr><td>{esc(e["match"])}</td><td class="num">R{e["round"]}</td>'
            f'<td>{esc(e["tier"])}</td><td class="{site_cls}"><b>{e["site"]}</b></td>'
            f'<td class="num">{t}</td><td>{esc(e["style"])}</td><td>{appr}</td>'
            f'<td class="num">{won}</td></tr>')
    return "\n".join(rows)


def matches_rows():
    rows = []
    for m in d["matches_analyzed"]:
        cov = ('<span class="cov-full">full replay</span>' if m["has_replay_positions"]
               else '<span class="cov-part">kills + economy only</span>')
        rows.append(f'<tr><td><b>{esc(m["opponent"])}</b></td>'
                    f'<td class="num">{m["rounds"]}</td><td>{cov}</td>'
                    f'<td class="dim">series {m["series_id"]} · match {m["match_id"]}</td></tr>')
    return "\n".join(rows)


def synthesis_html():
    out = []
    for line in synthesis_raw.splitlines():
        line = line.strip()
        if line.startswith("## "):
            out.append(f"<h4>{esc(line[3:])}</h4>")
        elif line.startswith("- "):
            t = esc(line[2:])
            t = (t.replace("**FINDING:**", '<b class="lbl">FINDING</b>')
                  .replace("**STRENGTH:**", '<b class="lbl">STRENGTH</b>')
                  .replace("**COUNTER:**", '<b class="lbl">COUNTER</b>')
                  .replace("**", ""))
            out.append(f"<p class='syn-li'>{t}</p>")
    return "\n".join(out)


HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>G2 Esports — Lotus Counter-Strat Brief</title>
<style>
:root{{
  --bg:#0a0c10; --surf:#11141b; --surf2:#171b24; --bdr:#222838;
  --txt:#ccd6e8; --dim:#5d6b7e; --gold:#f0c040; --red:#e8383d;
  --blue:#4da6ff; --green:#3ecf8e; --amber:#f59e0b;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--txt);font-family:"Segoe UI",system-ui,sans-serif;
  line-height:1.55;padding:36px 28px;max-width:1180px;margin:0 auto}}
h1{{font-size:26px;color:#fff;letter-spacing:.04em}}
h1 .accent{{color:var(--red)}}
.sub{{color:var(--dim);font-size:13px;margin:4px 0 26px}}
h2{{font-size:15px;letter-spacing:.14em;text-transform:uppercase;color:var(--gold);
  margin:38px 0 4px;padding-top:18px;border-top:1px solid var(--bdr)}}
h3{{font-size:13px;color:#fff;margin:18px 0 8px;letter-spacing:.04em}}
h4{{font-size:12px;color:var(--gold);margin:14px 0 4px}}
.sec-sub{{color:var(--dim);font-size:12px;margin-bottom:14px}}
.card{{background:var(--surf);border:1px solid var(--bdr);border-radius:10px;
  padding:20px 22px;margin:14px 0}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0}}
th{{text-align:left;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--dim);padding:6px 10px;border-bottom:1px solid var(--bdr)}}
td{{padding:8px 10px;border-bottom:1px solid #151923;vertical-align:top}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#141925}}
.num{{font-variant-numeric:tabular-nums;white-space:nowrap}}
.dim{{color:var(--dim)}}
.note{{color:var(--dim);font-size:11px;font-style:italic}}
.badge{{display:inline-block;font-size:10px;font-weight:800;letter-spacing:.08em;
  padding:2px 9px;border-radius:20px;vertical-align:middle}}
.b-strong{{background:#3a1114;color:#ff6b6e;border:1px solid #71262b}}
.b-mod{{background:#382a0c;color:#fbbf24;border:1px solid #6e5418}}
.b-weak{{background:#1d2330;color:#8b9bb0;border:1px solid #313c52}}
.tldr{{border-left:3px solid var(--red)}}
.tldr-item{{display:flex;gap:14px;padding:11px 4px;border-bottom:1px solid #151923;align-items:flex-start}}
.tldr-item:last-child{{border-bottom:none}}
.tldr-item .badge{{margin-top:2px;flex-shrink:0}}
.counter{{background:#0e1a14;border:1px solid #1d3a2a;border-radius:8px;
  padding:12px 16px;margin:10px 0;font-size:13px}}
.counter b.cl{{color:var(--green);font-size:10px;letter-spacing:.12em;display:block;margin-bottom:3px}}
.pdot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px}}
.map-wrap{{display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start}}
.map-box{{position:relative;width:540px;height:540px;flex-shrink:0;border:1px solid var(--bdr);
  border-radius:6px;overflow:hidden}}
.map-box img{{width:100%;height:100%;object-fit:fill;filter:brightness(.85) contrast(1.05)}}
.dot{{position:absolute;width:13px;height:13px;transform:translate(-50%,-50%);
  box-shadow:0 0 0 2px #000;cursor:pointer;z-index:5;transition:transform .1s}}
.dot:hover{{transform:translate(-50%,-50%) scale(1.8);z-index:30}}
.tip{{display:none;position:absolute;white-space:nowrap;background:rgba(5,7,12,.97);
  border:1px solid #2c3650;border-radius:6px;padding:7px 11px;font-size:11px;z-index:50;line-height:1.5}}
.dot:hover .tip{{display:block}}
.tip b{{display:block;font-size:12px}}
.map-legend{{font-size:12px;min-width:230px}}
.map-legend .row{{display:flex;align-items:center;gap:8px;margin:6px 0}}
.shape-demo{{display:inline-block;width:11px;height:11px;background:#aaa;box-shadow:0 0 0 1px #000}}
.bar-bg{{display:inline-block;width:170px;height:10px;background:#1a2030;border-radius:5px;
  vertical-align:middle;overflow:hidden}}
.bar-fill{{height:100%;border-radius:5px}}
.bar-num{{font-size:12px;margin-left:8px;font-variant-numeric:tabular-nums}}
.barcell{{white-space:nowrap}}
.hot-row td{{background:#1a1115}}
.site-a{{color:var(--red)}} .site-b{{color:var(--blue)}} .site-c{{color:var(--green)}}
.cov-full{{color:var(--green);font-size:12px}}
.cov-part{{color:var(--amber);font-size:12px}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
@media(max-width:900px){{.grid2{{grid-template-columns:1fr}}}}
details{{margin:16px 0}}
summary{{cursor:pointer;color:var(--dim);font-size:12px;letter-spacing:.06em}}
.syn-li{{font-size:12.5px;margin:7px 0;color:#a8b6c8}}
.lbl{{color:var(--gold);font-size:10px;letter-spacing:.1em}}
.foot{{margin-top:40px;padding-top:16px;border-top:1px solid var(--bdr);
  color:var(--dim);font-size:11.5px;line-height:1.7}}
</style>
</head>
<body>

<h1>G2 ESPORTS <span class="accent">//</span> LOTUS COUNTER-STRAT BRIEF</h1>
<div class="sub">5 matches · VCT Americas 2026 · pattern analysis from rib.gg replay + round data ·
analysis assisted by local qwen2.5:14b · generated June 9, 2026</div>

<div class="card">
  <h3 style="margin-top:0">Matches Analyzed</h3>
  <table>
    <thead><tr><th>Opponent</th><th>Rounds</th><th>Data coverage</th><th>Source</th></tr></thead>
    <tbody>{matches_rows()}</tbody>
  </table>
  <div class="note">G2 ran the identical comp in all 5 matches: BABYBAY (Vyse) · valyn (Omen) ·
  leaf (Viper) · jawgemo (Raze) · trent (Fade). Ult + stack analysis uses the 3 matches with full
  replay positions; buys and execs use all 5.</div>
</div>

<h2>TL;DR — Most Exploitable Tendencies</h2>
<div class="card tldr">
  <div class="tldr-item">{badge("STRONG")}<div><b>They are an A-site team.</b>
    70% of identified executes (28/40) hit A — including 4 of 5 pistol rounds and 17 of 24 full buys.
    But they only convert A hits at 64% (18/28). <b style="color:var(--green)">Counter:</b> weight your
    defense A on pistols and buy rounds; numbers contesting A works — their A success is beatable.</div></div>
  <div class="tldr-item">{badge("STRONG")}<div><b>leaf's defensive Viper Pit lands on C Main — every time.</b>
    3 of 3 defensive pits across 3 different matches were placed at C Main, all mid-first-half (round 9
    in each match, 46–72s in). <b style="color:var(--green)">Counter:</b> when leaf is on defense with ult,
    take C Main control off the table — fake C to burn the pit, then hit A/B; never dry-push C Main lane.</div></div>
  <div class="tldr-item">{badge("STRONG")}<div><b>trent's Odin habit — anchored at A Tree.</b>
    Odin in 8 separate rounds across the 5 matches, 7 of them on defense buy rounds. In every round with
    buy-phase position data he set up at A Tree at round start, spamming the A Main/A Site walls; kill-feed
    data adds C Bend lines on rotations. <b style="color:var(--green)">Counter:</b> when trent is alive on
    a G2 defense buy round, treat A Tree as an Odin nest — no static positions in the A Main doorway or
    behind Tree boxes; entry through util, not bodies.</div></div>
  <div class="tldr-item">{badge("STRONG")}<div><b>C executes are their cleanest look — 9/9 won.</b>
    They only commit C when it's earned (avg. exec commit is a slow 62.5s), usually through C Main with
    jawgemo's Raze ult from C Mound. <b style="color:var(--green)">Counter:</b> their C hits come late —
    keep a re-anchor + retake util ready for C after 50s instead of over-rotating early.</div></div>
  <div class="tldr-item">{badge("STRONG")}<div><b>leaf's bonus-round Judge is a system.</b>
    Actual buy-phase data shows 11 Judge rounds across 4 of 5 matches — every one on defense, 10 of 11
    in rounds 2–6 (the rounds right after a won pistol). He sets up at B Main (×5, plus B Site once) or
    C Main/C Mound (×2) and holds close. <b style="color:var(--green)">Counter:</b> in your bonus-round attacks, assume a
    Judge waits inside B Main / C Main smoke range — util-clear close corners, isolate range, never
    chain-run the B Main lane.</div></div>
  <div class="tldr-item">{badge("MODERATE")}<div><b>The OP has owners: jawgemo on defense, leaf on attack.</b>
    jawgemo bought the Operator on 4 full-buy DEFENSE rounds (all R10–12, late first half), rotating it
    between A Stairs, B Main and C Site. leaf's 4 OP rounds are all full-buy ATTACK (R18–19), starting
    A Root/A Lobby. <b style="color:var(--green)">Counter:</b> attacking into a rich G2 defense around
    rounds 10–12, flash-entry every long angle; defending their late-half full buys, don't dry-hold the
    A Main/A Root lane — leaf is scoped in.</div></div>
  <div class="tldr-item">{badge("MODERATE")}<div><b>B is the soft spot on their defense.</b>
    Only 20% of defensive presence sits B at 15s (vs 39% A, 36% C), and only 3 of 18 defensive
    3-stacks were B. <b style="color:var(--green)">Counter:</b> fast B hits punish their default;
    expect slow rotates through B Upper from A-heavy setups.</div></div>
  <div class="tldr-item">{badge("STRONG")}<div><b>Low-buy defense is forward-aggressive and stacked —
    and it only works on pistols.</b> 5 of 6 low-buy defenses (pistols + forced buys) had a 3-stack with
    forward-zone pushes; they won all 3 pistols but lost all 3 non-pistol low buys, including a 4-forward
    C-stack save vs KRÜ (R5). <b style="color:var(--green)">Counter:</b> default slow on pistol attack and
    set trades at chokes; on their save rounds, expect the same stack+push with worse guns — trade the
    forward contact, then hit the thin site.</div></div>
</div>

<h2>1 · Ultimate Locations</h2>
<div class="sec-sub">From the 3 replay matches (33 G2 ult casts). Viper Pit positions are corrected to the
<b>cast</b> location (stationary window before exit movement), not the charge-drop/exit location.
Squares = defense casts, circles = attack casts. Hover dots for details.</div>

<div class="card">
  <div class="map-wrap">
    <div class="map-box">
      <img src="data:image/png;base64,{img_b64}" alt="Lotus minimap">
      {ult_markers()}
    </div>
    <div class="map-legend">
      <div class="row"><span class="pdot" style="background:#f0c040"></span>BABYBAY — Vyse</div>
      <div class="row"><span class="pdot" style="background:#4da6ff"></span>valyn — Omen</div>
      <div class="row"><span class="pdot" style="background:#3ecf8e"></span>leaf — Viper</div>
      <div class="row"><span class="pdot" style="background:#e8383d"></span>jawgemo — Raze</div>
      <div class="row"><span class="pdot" style="background:#9b59b6"></span>trent — Fade</div>
      <div class="row" style="margin-top:14px"><span class="shape-demo" style="border-radius:50%"></span>attack cast</div>
      <div class="row"><span class="shape-demo" style="border-radius:2px"></span>defense cast</div>
    </div>
  </div>

  <h3>Per-player tendencies</h3>
  <table>
    <thead><tr><th>Player</th><th>Casts</th><th>Atk / Def</th><th>Top locations</th></tr></thead>
    <tbody>{ult_player_rows()}</tbody>
  </table>

  <div class="counter"><b class="cl">COUNTER-STRATS</b>
  · <b>leaf (Viper)</b> — defensive pit = C Main {badge("STRONG")} — see TL;DR. Her single attack pit
  was A Hut (post-plant, 128s): on late A retakes vs leaf with ult, expect pit on the A Hut plant zone.<br>
  · <b>jawgemo (Raze)</b> — showstopper twice from C Mound on C executes and twice at A Main / A Site
  on defense (anti-rush, as early as 18s) {badge("MODERATE")} — space out the first A Main contact and
  bait the defensive ult with util before swinging; on their C hit, expect the ult to open C Main.<br>
  · <b>trent (Fade)</b> — defensive ult thrown from A Hut twice (R9 in two matches) covering A site
  fights {badge("MODERATE")} — on A hits around rounds 8–10, expect prowlers from Hut; flash it off.<br>
  · <b>BABYBAY (Vyse)</b> + <b>valyn (Omen)</b> — both lean C-side on attack ults (C Link, C Door,
  C Lobby, C Mound) {badge("WEAK")} — steel garden / paranoia signals a C commit; call early rotate.</div>

  <details><summary>All 33 casts (click to expand)</summary>
  <table>
    <thead><tr><th>Player</th><th>Match</th><th>Round</th><th>Side</th><th>Time</th><th>Location</th></tr></thead>
    <tbody>{ult_cast_rows()}</tbody>
  </table>
  </details>
</div>

<h2>2 · Unconventional Buys (OP, Judge &amp; outliers)</h2>
<div class="sec-sub">Read directly from buy-phase inventory (last <code>ROUND_STARTING</code> replay frame,
when buys are final) for the KRÜ / 100T / C9 matches — these are <b>actual purchases</b>, not inferences.
The NRG and ENVY matches (no replay) still use kill-feed inference (locations marked *). Shorty is excluded:
buy data shows a pocket Shorty is the standard pro secondary on full buys, not an outlier.
Highlighted rows = exploitable signatures (4+ rounds).</div>

<div class="card">
  <table>
    <thead><tr><th>Weapon</th><th>Player</th><th>Rounds</th><th>Side</th><th>Economy</th>
    <th>Position at 15s</th><th>Main play area</th><th>Kill locations</th></tr></thead>
    <tbody>{buys_rows()}</tbody>
  </table>

  <div class="counter"><b class="cl">COUNTER-STRATS</b>
  · <b>leaf's Judge (11 rounds — the No.1 signature)</b> {badge("STRONG")} — bought in 4 of 5 matches,
  <b>always on defense</b>, 10 of 11 times in rounds 2–6 right after G2 won the pistol. 15s setup spots:
  B Main ×5 (+B Site ×1), C Main/C Mound ×2, A Tree ×1. Your bonus-round attack should pre-clear
  B Main and C Main close corners with util and fight at range — a dry contact inside his smoke is a lost duel.<br>
  · <b>trent's Odin (8 rounds, 7 on defense)</b> {badge("STRONG")} — buy-phase data confirms the A Tree
  anchor: 15s position was A Tree in all three measured rounds, with kills wallbanging A Main/A Tree and
  C Bend lines on the NRG match. No static holds in his spam lanes on G2 buy-round defense.<br>
  · <b>OP roles are split by side</b> {badge("MODERATE")} — jawgemo: 4 OP rounds, all defense full buys,
  all rounds 10–12, rotating A Stairs / B Main / C Site. leaf: 4 OP rounds, all attack full buys
  (R18–19), starting A Root/A Lobby then repositioning to C Main. BABYBAY OP'd once (def R11, A Hut).
  Flash-entry long angles vs their rich late-first-half defense; respect leaf's scoped lane control on
  their late-game attack rounds.<br>
  · <b>leaf's budget snipers</b> {badge("MODERATE")} — Outlaw ×6 (mixed sides, holds A Lobby/A Root lanes
  on attack, C Waterfall on defense) and a Marshal on <b>round 14 in two different matches</b> (attack,
  from A Lobby/A Rubble). Post-pistol round 2 of the second half: expect a budget scope holding the
  A lobby area before their exec develops.<br>
  · <b>Bulldogs are filler, not a pattern</b> {badge("WEAK")} — trent ×3, BABYBAY ×2, valyn ×1 on
  assorted force rounds; no positional consistency. Don't adapt to it.</div>

  <details><summary>All {sum(s["rounds"] for pls in d["weird_buys"].values() for s in pls.values())} unconventional-buy rounds (click to expand)</summary>
  <table>
    <thead><tr><th>Weapon</th><th>Player</th><th>Match</th><th>Round</th><th>Side</th>
    <th>Tier</th><th>Loadout val</th><th>15s position</th><th>Play area</th><th>Source</th></tr></thead>
    <tbody>{buy_detail_rows()}</tbody>
  </table>
  </details>
</div>

<h2>3 · Eco / Low-Money Stack Locations</h2>
<div class="sec-sub">Defensive setups at 15s on low-buy rounds (replay matches): pistols plus any round
where G2's parsed team loadout value was under 60% of the opponent's. That catches the forced buys the
economy band hides — e.g. R5 vs KRÜ: 10.8k (two rifles, three pistols) against a 22.3k full buy.</div>

<div class="card">
  <table>
    <thead><tr><th>Match</th><th>Round</th><th>Tier</th><th>G2 buy vs opp</th>
    <th>Players per zone (15s)</th>
    <th>Forward / aggressive players</th><th>Won</th></tr></thead>
    <tbody>{eco_rows()}</tbody>
  </table>

  <div class="counter"><b class="cl">COUNTER-STRATS</b>
  · <b>They stack broke too — 5 of 6 low-buy defenses had a 3-stack</b>, and the zone rotates
  (B, C, A on pistols; C then A on the KRÜ forced buys) {badge("STRONG")} — they pick one zone and
  flood it, leaving another site 1-thin. A default that gathers info before committing finds the
  thin site; against their saves it found it 3 of 3 times — <b>they lost every non-pistol low-buy
  defense observed</b>.<br>
  · <b>Forward aggression survives the bad economy</b> {badge("STRONG")} — forward-zone players in 5 of
  6 low-buy rounds, peaking at 4 of 5 players forward on the R5 vs KRÜ save (valyn, leaf, jawgemo,
  trent pushing on a 10.8k buy). It wins them pistols (3/3) but loses them broke rounds (0/3). Punish it:
  pre-aim the lobby pushes at 10–15s with a paired hold, trade the first contact, then hit the zone the
  aggression came from — it's now under-manned and under-gunned.<br>
  · <b>Their own low-buy attacks have no fixed look</b> (eco fast B at 38s vs 100T lost; semi-eco A vs
  KRÜ lost; semi-eco C at 50s vs C9 won) but pistol attack is A 4/5 {badge("MODERATE")} — on YOUR
  eco-defense rounds vs their pistol, stack A-lean; vs their bonus, play standard.</div>
</div>

<h2>4 · General Stack Locations (all round types)</h2>
<div class="sec-sub">Defensive zone occupation at 15s across 33 def rounds in the replay matches.
A "stack" = 3+ players in one zone.</div>

<div class="card grid2">
  <div>
    <h3 style="margin-top:0">Where the 3-stacks land (18 of 33 rounds)</h3>
    <table>
      <thead><tr><th>Zone</th><th>Rounds</th><th>Share</th></tr></thead>
      <tbody>{stack_dist_rows()}</tbody>
    </table>
  </div>
  <div>
    <h3 style="margin-top:0">Overall presence by zone at 15s</h3>
    <table>
      <thead><tr><th>Zone</th><th>Player-rounds</th><th>Share</th></tr></thead>
      <tbody>{presence_rows()}</tbody>
    </table>
  </div>
  <div style="grid-column:1/-1">
  <div class="counter"><b class="cl">COUNTER-STRATS</b>
  · <b>A is the default overload</b> {badge("STRONG")} — 10 of 18 stacks (56%) are A-side, and it grows
  on full buys (8 of 11 full-buy stacks were A). Their A stack typically includes jawgemo forward at
  A Main/A Door. On full-buy attacks, a B Main hit or a C Main hit with one A lurk catches the
  over-commit; A hits into this team need full util.<br>
  · <b>B never gets bodies</b> {badge("MODERATE")} — 3 stacks in 33 rounds, ~20% presence. B Upper is
  usually a single Vyse/Omen hold. Sell A with a fake (their stack feeds on A pressure), then flood B Main.<br>
  · <b>They stack MORE when rich</b> {badge("MODERATE")} — 11 of the 18 stacks came on full buys (20k+).
  Anti-stack reads matter most in the rounds after they win pistol/bonus.</div>
  </div>
</div>

<h2>5 · Site Execute Tendencies</h2>
<div class="sec-sub">All 5 matches, 46 attack rounds, site identified in 40 (replay positions → round
inference → kill-location fallback). Average commit time on measured execs: <b>62.5s</b> — G2 plays slow
defaults into late hits.</div>

<div class="card">
  <div class="grid2">
    <div>
      <h3 style="margin-top:0">Site distribution + conversion</h3>
      <table>
        <thead><tr><th>Site</th><th>Execs</th><th>Share</th><th>Won</th></tr></thead>
        <tbody>{exec_site_rows()}</tbody>
      </table>
    </div>
    <div>
      <h3 style="margin-top:0">By economy tier</h3>
      <table>
        <thead><tr><th>Tier</th><th>Rounds</th><th>Sites hit</th></tr></thead>
        <tbody>{exec_tier_rows()}</tbody>
      </table>
    </div>
  </div>

  <div class="counter"><b class="cl">COUNTER-STRATS</b>
  · <b>Pistols: A, A, A, A, C</b> {badge("STRONG")} — 4 of 5 pistols hit A. Set a 3-A pistol defense
  with one B Upper flex and one C anchor; retake-ready rather than spread thin.<br>
  · <b>Buy rounds still funnel A</b> (23 of 32 known semi-buy + full-buy execs) {badge("STRONG")} — but they
  win only ~64% of A hits. Their A entries come split: A Main + A Root/A Rubble double-lane (seen in
  every measured split). Hold cross-fire A Site + Hut, save retake util — they commit late (62s+),
  so early util is wasted util.<br>
  · <b>The C look is the dagger — 9/9 won</b> {badge("STRONG")} — usually full-buy, late (50–84s),
  through C Main with Raze ult from C Mound, sometimes split C Mound/C Lobby. After 50s with no A contact,
  rotate the flex TOWARD C, pre-position retake util, and watch the C Main lane for the jawgemo ult opener.<br>
  · <b>B is a change-up only</b> (3 execs, 1 won) {badge("MODERATE")} — usually mid-round off A Link
  control (split A Link + B Main vs C9). A B Main contact before 40s is more likely a fake or an eco rush
  than a real full-buy hit.</div>

  <details><summary>All 40 identified executes (click to expand)</summary>
  <table>
    <thead><tr><th>Match</th><th>Round</th><th>Tier</th><th>Site</th><th>Commit</th>
    <th>Style</th><th>Approach lanes</th><th>Won</th></tr></thead>
    <tbody>{exec_detail_rows()}</tbody>
  </table>
  </details>
</div>

<h2>Appendix</h2>
<div class="card">
  <details><summary>Raw qwen2.5:14b synthesis (local model output, uncurated)</summary>
  {synthesis_html()}
  </details>
  <div class="foot">
    <b>Methodology &amp; limitations.</b>
    Ult casts detected as the replay frame where a player's ult charges drop from full; Viper Pit casts
    are corrected to the last near-stationary window (&ge;4s under 150 u/s) before exit movement, since
    her charges only drop on leaving the pit. The NRG and ENVY series have no rib.gg 2D replay
    ("2D replay hasn't been enabled for this series"), so ult/stack analysis covers the KRÜ, 100T and C9
    matches only; buys and execs cover all 5 via kill-feed and round data. Unconventional buys are read
    from actual buy-phase inventory (round_loadouts, parsed from the last ROUND_STARTING frame of each
    round, after buy time ends) for the three replay matches; for NRG/ENVY they are inferred from kills,
    which misses buys that got no kill. Site executes are
    identified by 3+ attackers entering a site zone (replay), rib.gg round inference, or majority kill
    location (fallback); 6 of 46 attack rounds remained unclassified (saves / round ended early).
    Counter-strat synthesis drafted with local Ollama model qwen2.5:14b and curated against the raw numbers.
  </div>
</div>

</body>
</html>"""

Path("g2_lotus_counterstrats.html").write_text(HTML, encoding="utf-8")
print(f"Saved g2_lotus_counterstrats.html ({len(HTML):,} chars)")
