"""
Derives the affine transform from rib.gg coords → callout image pixel position.

Step 1: rib.gg → Valorant API coords (derived from 3 confirmed positions)
Step 2: api coords → callout image % (fitted by least-squares using callout labels)
"""
import numpy as np
from PIL import Image, ImageDraw
import base64, io, urllib.request, json

# ── Step 1: rib.gg → api affine transform ─────────────────────────────────
# Derived from 3 confirmed points:
#   B Main:         rib(3807, -2751) ↔ api(4877,  -48)
#   A Link:         rib(6890,  -432) ↔ api(6011, 2088)
#   Attacker Spawn: rib(3200,  2600) ↔ api(1401,  777)  [teal pixel center]
#
# Solution: api_x = 0.789*rib_x - 0.560*rib_y + 333
#           api_y = 0.5315*rib_x + 0.2147*rib_y - 1481

def rib_to_api(rx, ry):
    ax = 0.7890 * rx - 0.5600 * ry + 333
    ay = 0.5315 * rx + 0.2147 * ry - 1481
    return ax, ay

# Verify
for name, rx, ry, expected_ax, expected_ay in [
    ("B Main",   3807, -2751, 4877,  -48),
    ("A Link",   6890,  -432, 6011, 2088),
    ("Att Spawn",3200,  2600, 1401,  777),
]:
    ax, ay = rib_to_api(rx, ry)
    print(f"{name}: api=({ax:.0f},{ay:.0f})  expected=({expected_ax},{expected_ay})")


# ── Step 2: api → callout image %, least-squares fit ─────────────────────
# Visual estimates of callout label pixel positions in the 1656x1656 image
# (api_x, api_y, pct_x, pct_y)
CALIBRATION_POINTS = [
    # Attacker Spawn - measured precisely from teal pixels
    (1401,   777,  0.543, 0.784),
    # Callout labels visually estimated
    (9687,  1698,  0.513, 0.181),  # Defender Spawn
    (9516,  6093,  0.833, 0.260),  # A Drop
    (9260,  5046,  0.785, 0.205),  # A Top
    (7736,  5557,  0.863, 0.332),  # A Site
    (7917,  5557,  0.948, 0.308),  # A Hut
    (5288,  4160,  0.821, 0.429),  # A Main
    (6011,  2088,  0.694, 0.471),  # A Link
    (6150,  5557,  0.906, 0.405),  # A Tree
    (5609,  5204,  0.870, 0.435),  # A Door
    (4401,  3294,  0.809, 0.489),  # A Root
    (4401,  4918,  0.821, 0.537),  # A Rubble
    (8258,  3861,  0.773, 0.272),  # A Stairs
    (2686,  2927,  0.725, 0.622),  # A Lobby
    (7683,  1517,  0.525, 0.230),  # B Upper
    (6368,   668,  0.592, 0.344),  # B Site
    (4877,   -48,  0.423, 0.610),  # B Main
    (3565,   668,  0.495, 0.664),  # B Pillars
    (6677, -4266,  0.181, 0.350),  # C Site
    (7902, -4266,  0.205, 0.284),  # C Hall
    (5658, -5281,  0.109, 0.405),  # C Bend
    (5311, -3148,  0.220, 0.598),  # C Main
    (4819, -1753,  0.338, 0.634),  # C Door
    (6720, -1994,  0.308, 0.471),  # C Waterfall
    (7504, -1378,  0.402, 0.386),  # C Link
    (8937, -1752,  0.483, 0.242),  # C Gravel
    (3864, -1577,  0.293, 0.713),  # C Mound
    (3565, -1577,  0.257, 0.749),  # C Lobby (adjusted)
]

pts = np.array(CALIBRATION_POINTS)
api_x = pts[:, 0]
api_y = pts[:, 1]
cal_px = pts[:, 2]
cal_py = pts[:, 3]

# Fit: pct = a*api_x + b*api_y + c
A = np.column_stack([api_x, api_y, np.ones(len(pts))])
coeff_x, _, _, _ = np.linalg.lstsq(A, cal_px, rcond=None)
coeff_y, _, _, _ = np.linalg.lstsq(A, cal_py, rcond=None)

print(f"\nFitted x: api_x*{coeff_x[0]:.4e} + api_y*{coeff_x[1]:.4e} + {coeff_x[2]:.4f}")
print(f"Fitted y: api_x*{coeff_y[0]:.4e} + api_y*{coeff_y[1]:.4e} + {coeff_y[2]:.4f}")

def api_to_pct(ax, ay):
    px = coeff_x[0]*ax + coeff_x[1]*ay + coeff_x[2]
    py = coeff_y[0]*ax + coeff_y[1]*ay + coeff_y[2]
    return px, py

def rib_to_pct(rx, ry):
    ax, ay = rib_to_api(rx, ry)
    return api_to_pct(ax, ay)

print("\nResiduals (calibration points):")
for (ax, ay, cpx, cpy) in CALIBRATION_POINTS:
    fx, fy = api_to_pct(ax, ay)
    print(f"  ({ax:5.0f},{ay:5.0f}) expected=({cpx:.3f},{cpy:.3f}) got=({fx:.3f},{fy:.3f}) err=({abs(fx-cpx):.3f},{abs(fy-cpy):.3f})")

print("\nUlt fire positions in callout image:")
ULT_FIRES = [
    ('BABYBAY','Vyse',  17,'0:14','7-9', 'WIN', '#f0c040', 6890,   -432),
    ('jawgemo','Raze',  18,'0:42','8-9', 'WIN', '#e8383d', 3807,  -2751),
    ('trent',  'Fade',  18,'0:44','8-9', 'WIN', '#4da6ff', 4036,  -3070),
    ('valyn',  'Omen',  20,'0:32','9-10','WIN', '#9b59b6', 2018,  -1092),
    ('leaf',   'Viper', 20,'+29s plant','9-10','WIN','#3ecf8e',10168, 3369),
    ('jawgemo','Raze',  22,'0:48','10-11','WIN','#e8383d', 3876,  -2644),
    ('BABYBAY','Vyse',  24,'1:00','12-11','WIN','#f0c040', 4634,  -2287),
    ('trent',  'Fade',  24,'0:19','12-11','WIN','#4da6ff', 6453,  -5455),
]

results = []
for ign, agent, rnd, timer, score, won, color, rx, ry in ULT_FIRES:
    px, py = rib_to_pct(rx, ry)
    print(f"  {ign} R{rnd}: rib({rx},{ry}) -> ({px:.3f},{py:.3f})")
    results.append((ign, agent, rnd, timer, score, won, color, round(px*100,2), round(py*100,2)))

# ── Step 3: draw debug overlay on callout image ──────────────────────────
callout = Image.open('image.webp').convert('RGBA')
CW, CH = callout.size
draw = ImageDraw.Draw(callout)

COLORS_BY_IGN = {r[0]:r[6] for r in results}

for ign, agent, rnd, timer, score, won, color, px_pct, py_pct in results:
    px = int(px_pct/100 * CW)
    py = int(py_pct/100 * CH)
    r, g, b = int(color[1:3],16), int(color[3:5],16), int(color[5:7],16)
    draw.ellipse([px-12, py-12, px+12, py+12], fill=(r,g,b,220), outline=(0,0,0,255))
    draw.text((px+14, py-8), f"R{rnd}", fill=(r,g,b,255))

buf = io.BytesIO()
callout.save(buf, 'PNG')
b64 = base64.b64encode(buf.getvalue()).decode()

with open('callout_debug.html', 'w') as f:
    f.write(f'<html><body style="background:#111"><img src="data:image/png;base64,{b64}" width="800"><p style="color:#fff">Ult positions on callout image using affine transform.</p></body></html>')
print('\nSaved callout_debug.html')
