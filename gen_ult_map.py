import base64, json, os, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

_DIR = os.path.dirname(__file__)

# ── Callout data (source: valorant-api.com via lotus_callouts.json) ──────────
with open(os.path.join(_DIR, 'lotus_callouts.json')) as f:
    _cd = json.load(f)
_CALLOUTS = _cd['callouts']
_TX       = _cd['coordinateTransform']  # xMult, yMult, xScalar, yScalar

# ── Map image ────────────────────────────────────────────────────────────────
with open(os.path.join(_DIR, 'image.webp'), 'rb') as f:
    IMG_B64 = base64.b64encode(f.read()).decode()

# ── Coordinate transform ──────────────────────────────────────────────────────
# rib.gg API stores locationX/Y in a rotated game-space that differs from the
# official Valorant API coords that lotus_callouts.json uses.  The affine below
# converts between them (fit from confirmed anchor pairs: ATK spawn, B Main, A Link).
def _to_api(rx, ry):
    ax = 0.7890 * rx - 0.5600 * ry + 333
    ay = 0.5315 * rx + 0.2147 * ry - 1481
    return ax, ay

def to_pct(rx, ry):
    """rib.gg coords → (left%, top%) on the minimap."""
    ax, ay = _to_api(rx, ry)
    nx = ay * _TX['xMultiplier'] + _TX['xScalarToAdd']
    ny = ax * _TX['yMultiplier'] + _TX['yScalarToAdd']
    return round(max(0.0, min(100.0, nx * 100)), 2), \
           round(max(0.0, min(100.0, ny * 100)), 2)

def callout_name(rx, ry):
    """Nearest callout label to rib.gg coords (Euclidean in normalised space)."""
    ax, ay = _to_api(rx, ry)
    nx = ay * _TX['xMultiplier'] + _TX['xScalarToAdd']
    ny = ax * _TX['yMultiplier'] + _TX['yScalarToAdd']
    return min(_CALLOUTS,
               key=lambda c: (c['normalizedX'] - nx)**2 + (c['normalizedY'] - ny)**2
               )['name']

# ── Ult fires ─────────────────────────────────────────────────────────────────
# (player, agent, round, timer, score, result, colour, rib_x, rib_y, underground)
# Coordinates: DB values from rib.gg 2D-replay frames at the ult-fire transition.
# underground=True → player was in a Lotus tunnel; position is surface-projected.
_FIRES = [
    ('BABYBAY', 'Vyse',   17, '0:14', '7-9',   'WIN', '#f0c040',  3924,   5482, False),
    ('jawgemo', 'Raze',   18, '0:42', '8-9',   'WIN', '#e8383d', 10038,   4094, False),
    ('trent',   'Fade',   18, '0:44', '8-9',   'WIN', '#4da6ff',  4036,  -3070, False),
    ('valyn',   'Omen',   20, '0:32', '9-10',  'WIN', '#9b59b6',  2018,  -1092, True),
    ('leaf',    'Viper',  20, '+29s', '9-10',  'WIN', '#3ecf8e', 10168,   3369, True),
    ('jawgemo', 'Raze',   22, '0:48', '10-11', 'WIN', '#e8383d',  3876,  -2644, False),
    ('BABYBAY', 'Vyse',   24, '1:00', '12-11', 'WIN', '#f0c040',  4634,  -2287, False),
    ('trent',   'Fade',   24, '0:19', '12-11', 'WIN', '#4da6ff',  6453,  -5455, False),
]

fires = []
for ign, agent, rnd, timer, score, result, color, rx, ry, ug in _FIRES:
    px, py = to_pct(rx, ry)
    name   = callout_name(rx, ry) + (' ↓' if ug else '')
    fires.append(dict(ign=ign, agent=agent, rnd=rnd, timer=timer,
                      score=score, result=result, color=color,
                      px=px, py=py, callout=name, ug=ug))

# ── HTML helpers ──────────────────────────────────────────────────────────────
PLAYER_ORDER = ['BABYBAY', 'valyn', 'leaf', 'jawgemo', 'trent']

def _markers():
    out = []
    for f in fires:
        px, py, c = f['px'], f['py'], f['color']
        tip_x = 'left:-148px;text-align:right;' if px > 65 else 'left:20px;'
        tip_y = 'top:-42px;'                     if py > 88 else 'top:-6px;'
        if f['ug']:
            dot   = f'<div style="width:8px;height:8px;border-radius:50%;background:{c};opacity:.55;margin:2px auto"></div>'
            mstyle = f'left:{px}%;top:{py}%;background:transparent;border:2px dashed {c};box-shadow:0 0 8px {c}55;'
            extra  = f'<div class="tip-ug">⬇ underground · position approximate</div>'
        else:
            dot    = ''
            mstyle = f'left:{px}%;top:{py}%;background:{c};box-shadow:0 0 0 2px #000,0 0 8px {c}88;'
            extra  = ''
        out.append(
            f'<div class="dot" style="{mstyle}">{dot}'
            f'<div class="tip" style="{tip_x}{tip_y}">'
            f'<b style="color:{c}">{f["ign"]} R{f["rnd"]}</b>'
            f'<span class="tip-sub">{f["timer"]} · {f["callout"]}</span>'
            f'{extra}</div></div>'
        )
    return '\n'.join(out)

def _legend():
    out = []
    for ign in PLAYER_ORDER:
        fs = [f for f in fires if f['ign'] == ign]
        if not fs: continue
        c  = fs[0]['color']
        out.append(
            f'<div class="leg-row">'
            f'<span class="leg-dot" style="background:{c};box-shadow:0 0 5px {c}88"></span>'
            f'<span style="color:{c};font-weight:700">{ign}</span>'
            f'<span class="dim"> ({fs[0]["agent"]})</span>'
            f'<span class="leg-rounds">{", ".join("R"+str(f["rnd"]) for f in fs)}</span>'
            f'</div>'
        )
    return '\n'.join(out)

def _rows():
    out = []
    for f in fires:
        bc  = 'bw' if f['result'] == 'WIN' else 'bl'
        ug  = ' <span class="ug-tag" title="Underground tunnel — position approximate">↓</span>' if f['ug'] else ''
        out.append(
            f'<tr>'
            f'<td><span class="ign-dot" style="background:{f["color"]}"></span>{f["ign"]}</td>'
            f'<td class="dim">{f["agent"]}</td>'
            f'<td class="num">R{f["rnd"]}</td>'
            f'<td>{f["timer"]}</td>'
            f'<td>{f["score"]}</td>'
            f'<td class="dim">{f["callout"]}{ug}</td>'
            f'<td><span class="badge {bc}">{f["result"]}</span></td>'
            f'</tr>'
        )
    return '\n'.join(out)

# ── Page ──────────────────────────────────────────────────────────────────────
HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>G2 — Lotus Ult Map</title>
<style>
:root{{--bg:#08090d;--surf:#10131a;--bdr:#1c2030;--gold:#f0c040;--dim:#50606e;--txt:#ccd6e8}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--txt);font-family:"Segoe UI",system-ui,sans-serif;padding:32px}}
h1{{font-size:21px;font-weight:700;letter-spacing:.06em;color:#fff;margin-bottom:3px}}
.sub{{color:var(--dim);font-size:12px;margin-bottom:30px}}

.card{{background:var(--surf);border:1px solid var(--bdr);border-radius:8px;padding:22px;margin-bottom:22px}}
.card-title{{font-size:10px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--gold);margin-bottom:18px}}

/* map */
.map-wrap{{display:flex;gap:24px;align-items:flex-start;flex-wrap:wrap}}
.map-box{{position:relative;width:580px;height:580px;flex-shrink:0;border:1px solid var(--bdr);border-radius:4px;overflow:hidden}}
.map-box img{{width:100%;height:100%;display:block;object-fit:fill;filter:brightness(.82) contrast(1.06)}}
.map-over{{position:absolute;inset:0}}

/* markers */
.dot{{position:absolute;width:14px;height:14px;border-radius:50%;transform:translate(-50%,-50%);cursor:pointer;z-index:10;transition:transform .12s}}
.dot:hover{{transform:translate(-50%,-50%) scale(1.9);z-index:30}}
.tip{{display:none;position:absolute;white-space:nowrap;background:rgba(4,6,12,.96);border:1px solid rgba(255,255,255,.15);border-radius:5px;padding:6px 11px;font-size:11px;z-index:40;line-height:1.5}}
.dot:hover .tip{{display:block}}
.tip b{{display:block;font-size:12px;letter-spacing:.04em}}
.tip-sub{{display:block;color:#6a7e92;font-size:11px}}
.tip-ug{{color:#e09030;font-size:10px;font-style:italic;margin-top:2px}}

/* legend */
.legend{{display:flex;flex-direction:column;gap:10px;min-width:190px}}
.leg-title{{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);margin-bottom:2px}}
.leg-row{{display:flex;align-items:center;gap:7px;font-size:13px}}
.leg-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
.leg-rounds{{margin-left:auto;font-size:11px;color:#566070}}
.leg-note{{font-size:11px;color:var(--dim);font-style:italic;margin-top:12px;line-height:1.65}}

/* table */
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;font-size:10px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--dim);padding:5px 12px;border-bottom:1px solid var(--bdr)}}
td{{padding:9px 12px;border-bottom:1px solid #0f1118;vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#0d1016}}
.num{{font-variant-numeric:tabular-nums;color:#7a8898}}
.dim{{color:var(--dim)}}
.ign-dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;vertical-align:middle}}
.badge{{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.06em;padding:2px 7px;border-radius:3px}}
.bw{{background:#152a1e;color:#3ecf8e}}
.bl{{background:#2a1515;color:#e8383d}}
.ug-tag{{color:#e09030;cursor:help}}
.foot{{font-size:11px;color:var(--dim);margin-top:10px;font-style:italic}}
</style>
</head>
<body>

<h1>G2 Esports — Ult Fire Locations</h1>
<div class="sub">Lotus &nbsp;·&nbsp; T-Side Rounds 13–24 &nbsp;·&nbsp; Match 239296 vs KRU Esports &nbsp;·&nbsp; Hover markers for details</div>

<div class="card">
  <div class="card-title">Map — Ult Fire Locations</div>
  <div class="map-wrap">
    <div class="map-box">
      <img src="data:image/webp;base64,{IMG_B64}" alt="Lotus callout map">
      <div class="map-over">
{_markers()}
      </div>
    </div>
    <div class="legend">
      <div class="leg-title">Players</div>
{_legend()}
      <div class="leg-note">
        Hover a dot for round,<br>timer &amp; callout.<br><br>
        <strong style="color:var(--txt)">8 / 8 ult fires → round wins.</strong>
      </div>
    </div>
  </div>
</div>

<div class="card">
  <div class="card-title">Ult Fires — Detail</div>
  <table>
    <thead><tr>
      <th>Player</th><th>Agent</th><th>Round</th><th>Timer</th><th>Score</th><th>Location</th><th>Result</th>
    </tr></thead>
    <tbody>
{_rows()}
    </tbody>
  </table>
  <div class="foot">
    Timer = round clock at ult fire. Score from G2 perspective.
    Coordinates from rib.gg 2D replay DB; positions via Valorant API minimap transform (lotus_callouts.json).
    ↓ = underground tunnel — rib.gg reports no floor/Z data, marker is surface projection.
  </div>
</div>

</body>
</html>"""

out = os.path.join(_DIR, 'lotus_ult_map.html')
with open(out, 'w', encoding='utf-8') as fh:
    fh.write(HTML)
print(f'Saved: {out}')
