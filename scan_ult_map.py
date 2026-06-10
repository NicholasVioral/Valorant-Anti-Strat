"""
scan_ult_map.py
---------------
Screenshot a rib.gg 2D replay map, send it to a local Ollama vision model,
and extract ult-cast locations from the visual.

Workflow:
  1. Playwright loads the rib.gg 2D replay URL
  2. Screenshot is taken after the map renders
  3. The map canvas region is cropped (auto-detected or manual bounds)
  4. llama3.2-vision (via Ollama) identifies ult indicators and their positions
  5. Positions are printed and optionally overlaid on the map image

Usage:
    python scan_ult_map.py --url "https://www.rib.gg/2d/series/103866/map/1?round=1"
    python scan_ult_map.py --url "..." --round 5 --model llama3.2-vision:11b
    python scan_ult_map.py --url "..." --save-screenshot   # keep raw screenshot
    python scan_ult_map.py --url "..." --crop 120,60,740,720  # manual crop: x0,y0,x1,y1
"""

import argparse
import base64
import json
import sys
import time
import urllib.request
from io import BytesIO
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────
OLLAMA_URL   = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "llama3.2-vision:11b"
PAGE_WAIT_MS  = 12_000  # ms to wait after navigation for canvas to render

PROMPT = """You are analyzing a screenshot of a Valorant 2D minimap replay from rib.gg.

Your task: identify every player who currently has their ultimate ability READY or who just USED their ultimate (look for glowing rings, bright highlighted dots, special icons, or any visual indicator that distinguishes an "ult ready" or "ult active" player from a normal player dot).

For each such player dot you find:
1. Estimate their position as a percentage of the image — (left%, top%) where 0,0 is top-left and 100,100 is bottom-right.
2. Note the dot color if visible.
3. Note which team they appear to be on (typically blue team vs red/orange team, or left side vs right side based on color).

Return ONLY a JSON array. Example format:
[
  {"left_pct": 42.5, "top_pct": 31.0, "color": "blue", "team": 1, "ult_state": "ready"},
  {"left_pct": 67.2, "top_pct": 58.3, "color": "red",  "team": 2, "ult_state": "casting"}
]

If you see NO ult indicators, return an empty array: []
Do not include any explanation outside the JSON array."""


# ── Playwright screenshot ──────────────────────────────────────────────────────

def screenshot_map(url: str, round_num: int | None = None,
                   save_path: Path | None = None) -> bytes:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright not installed: pip install playwright && playwright install chromium")

    # Append round param if not already in URL
    if round_num is not None and "round=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}round={round_num}"

    print(f"  Loading: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        # 2D replay is a heavy SPA — "load" never fires; use domcontentloaded
        # then wait for the canvas/map to finish rendering.
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(PAGE_WAIT_MS)

        # Dismiss cookie consent if present
        for sel in ["button:has-text('Allow all')", "button:has-text('Allow All')",
                    "button:has-text('Accept')", "button:has-text('OK')"]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=1500):
                    btn.click()
                    page.wait_for_timeout(1500)
                    break
            except Exception:
                pass

        png_bytes = page.screenshot(full_page=False)
        browser.close()

    if save_path:
        save_path.write_bytes(png_bytes)
        print(f"  Screenshot saved: {save_path}")

    return png_bytes


# ── Image crop ────────────────────────────────────────────────────────────────

def crop_map_region(png_bytes: bytes, crop: tuple | None = None) -> bytes:
    """
    Crop to the minimap canvas.
    If crop=(x0,y0,x1,y1) is given, use that directly.
    Otherwise try to auto-detect the map canvas by looking for the largest
    dark square region (the minimap background is near-black).
    """
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("Pillow not installed: pip install pillow")

    img = Image.open(BytesIO(png_bytes))

    if crop:
        x0, y0, x1, y1 = crop
        return _pil_to_bytes(img.crop((x0, y0, x1, y1)))

    # rib.gg 2D layout at 1400×900: player panels on left (~0-220) and right (~1180-1400),
    # map canvas in the centre (~430-1060, ~55-870). Scale to actual screenshot size.
    W, H = img.size
    x0 = int(W * 0.307)   # ~430/1400
    y0 = int(H * 0.061)   # ~55/900
    x1 = int(W * 0.757)   # ~1060/1400
    y1 = int(H * 0.967)   # ~870/900
    cropped = img.crop((x0, y0, x1, y1))
    # Force square so the vision model sees the correct aspect ratio
    side = min(cropped.size)
    cropped = cropped.crop((0, 0, side, side))
    return _pil_to_bytes(cropped)


def _pil_to_bytes(img) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── Ollama vision call ────────────────────────────────────────────────────────

def ask_ollama(image_bytes: bytes, model: str = DEFAULT_MODEL,
               prompt: str = PROMPT) -> str:
    img_b64 = base64.b64encode(image_bytes).decode()

    payload = json.dumps({
        "model":  model,
        "prompt": prompt,
        "images": [img_b64],
        "stream": False,
        "options": {"temperature": 0.0},
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    print(f"  Sending to Ollama ({model}) ...")
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read())

    return body.get("response", "")


# ── Parse ─────────────────────────────────────────────────────────────────────

def parse_positions(raw: str) -> list[dict]:
    """Extract the JSON array from the model response."""
    raw = raw.strip()
    # Find first '[' and last ']'
    start = raw.find("[")
    end   = raw.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []


# ── Render overlay ────────────────────────────────────────────────────────────

def save_overlay(image_bytes: bytes, positions: list[dict], out_path: Path):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("  Pillow not available — skipping overlay image")
        return

    img  = Image.open(BytesIO(image_bytes)).convert("RGBA")
    draw = ImageDraw.Draw(img)
    W, H = img.size

    for i, pos in enumerate(positions):
        px = int(pos["left_pct"] / 100 * W)
        py = int(pos["top_pct"]  / 100 * H)
        color = pos.get("color", "yellow")
        # Normalize color string to RGB
        color_map = {
            "blue": (80, 180, 255), "red": (255, 80, 80),
            "yellow": (255, 220, 0), "green": (80, 220, 80),
            "orange": (255, 160, 40), "purple": (180, 80, 255),
            "white": (255, 255, 255),
        }
        rgb = color_map.get(color.lower(), (255, 220, 0))

        r = 12
        draw.ellipse([px - r, py - r, px + r, py + r],
                     outline=(0, 0, 0, 255), width=2, fill=(*rgb, 180))
        draw.text((px + r + 3, py - 6), f"{i+1}", fill=(255, 255, 255, 255))

    img.save(out_path)
    print(f"  Overlay saved: {out_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Scan rib.gg 2D map with Ollama vision")
    ap.add_argument("--url",             required=True, help="rib.gg 2D replay URL")
    ap.add_argument("--round",           type=int,      help="Round number to view")
    ap.add_argument("--model",           default=DEFAULT_MODEL)
    ap.add_argument("--crop",            type=str,      help="x0,y0,x1,y1 manual crop")
    ap.add_argument("--save-screenshot", action="store_true", help="Keep the raw screenshot")
    ap.add_argument("--out",             default="ult_scan_overlay.png")
    args = ap.parse_args()

    crop = None
    if args.crop:
        crop = tuple(int(v) for v in args.crop.split(","))

    here = Path(__file__).parent

    # 1. Screenshot
    print("\n[1] Screenshotting rib.gg 2D replay ...")
    ss_path = (here / "rib_2d_screenshot.png") if args.save_screenshot else None
    png = screenshot_map(args.url, round_num=args.round, save_path=ss_path)
    print(f"  Screenshot: {len(png)//1024} KB")

    # 2. Crop to map region
    print("\n[2] Cropping to minimap region ...")
    map_png = crop_map_region(png, crop=crop)
    crop_path = here / "rib_2d_map_crop.png"
    crop_path.write_bytes(map_png)
    print(f"  Crop saved for inspection: {crop_path}")

    # 3. Ask Ollama
    print("\n[3] Asking Ollama vision model ...")
    raw_response = ask_ollama(map_png, model=args.model)
    print(f"\n  Raw response:\n{raw_response}\n")

    # 4. Parse
    positions = parse_positions(raw_response)
    print(f"[4] Parsed {len(positions)} ult position(s):")
    for i, p in enumerate(positions, 1):
        print(f"  {i}. left={p.get('left_pct'):.1f}%  top={p.get('top_pct'):.1f}%"
              f"  color={p.get('color')}  team={p.get('team')}  state={p.get('ult_state')}")

    if not positions:
        print("  (No ult positions detected — check rib_2d_map_crop.png to verify the crop)")
        sys.exit(0)

    # 5. Overlay
    print("\n[5] Rendering overlay ...")
    save_overlay(map_png, positions, here / args.out)
    print(f"\nDone. Open {args.out} to verify positions.")


if __name__ == "__main__":
    main()
