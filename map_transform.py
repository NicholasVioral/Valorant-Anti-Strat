"""
map_transform.py
----------------
Converts Valorant game coordinates (locationX/locationY from rib.gg API)
to pixel positions on the Lotus labeled map image (1092x1092).

Calibrated from match 239296 (Lotus), round 13, t=2996ms.
4 anchor pairs. Visually verified — all players land in correct zones.

Usage:
    from map_transform import game_to_image

    px, py = game_to_image(location_x, location_y)

    # For a different output size:
    px, py = game_to_image(location_x, location_y, img_w=800, img_h=800)
"""

_AX =  0.0470467
_BX =  0.0606453
_CX =  284.9939

_AY = -0.1201754
_BY =  0.0215656
_CY = 1208.0179

_CALIB_W = 1092
_CALIB_H = 1092


def game_to_image(gx: float, gy: float,
                  img_w: int = _CALIB_W,
                  img_h: int = _CALIB_H):
    """
    Convert raw locationX/locationY → pixel on the Lotus labeled map image.

    Args:
        gx, gy:      locationX, locationY from rib.gg API frames endpoint
        img_w/img_h: output image size (default 1092x1092)

    Returns:
        (px, py) as ints, clamped to image bounds
    """
    ix = _AX * gx + _BX * gy + _CX
    iy = _AY * gx + _BY * gy + _CY

    px = int(ix * img_w / _CALIB_W)
    py = int(iy * img_h / _CALIB_H)

    px = max(0, min(img_w - 1, px))
    py = max(0, min(img_h - 1, py))
    return px, py


if __name__ == "__main__":
    # Visual verification — should match the calibration screenshot
    players = [
        ("G2 BABYBAY  (ATK Spawn)", 3524,  2677, "teal cluster"),
        ("G2 JAWGEMO  (ATK Spawn)", 3086,  2635, "teal cluster"),
        ("KRU mwzera  (A Site)",    7558,  5692, "far right"),
        ("KRU Dante   (C Main)",    4946, -3538, "left side"),
        ("KRU Saadhak (B Main)",    4868,  -588, "center"),
        ("KRU silentzz(C Hall)",    6049, -3765, "upper left"),
        ("KRU Less    (B Main)",    5203,  -371, "center"),
    ]
    print(f"{'Player':<32} {'img_x':>6} {'img_y':>6}  {'Zone'}")
    print("-" * 60)
    for name, gx, gy, zone in players:
        px, py = game_to_image(gx, gy)
        print(f"{name:<32} {px:6d} {py:6d}  {zone}")
