"""
Intercepts all network requests while loading a rib.gg series page.
Runs a real Chromium browser so it passes Cloudflare automatically.

Run:  python find_api.py
      python find_api.py --series 83369
      python find_api.py --headless   (no visible browser window)

Also dumps the page's embedded __NEXT_DATA__ JSON if present.
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, Request, Response
except ImportError:
    print("Playwright not installed.  Run:")
    print("  pip install playwright")
    print("  playwright install chromium")
    sys.exit(1)

SERIES_URL = "https://www.rib.gg/series/paper-rex-vs-t1-champions-tour-2025-pacific-kickoff-main-event/{series_id}"
DUMP_DIR = Path(__file__).parent / "api_dumps"


def intercept(series_id: int, headless: bool = False):
    target_url = SERIES_URL.format(series_id=series_id)
    print(f"\nOpening: {target_url}")
    print("Waiting for all network activity to settle...\n")

    captured: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-service-autorun",
                "--password-store=basic",
                "--disable-infobars",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        page = context.new_page()
        # Hide the navigator.webdriver flag that Cloudflare checks
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        def on_request(req: Request):
            url = req.url
            if "be-prod.rib.gg" in url or "rib.gg/api" in url:
                captured.append({
                    "method": req.method,
                    "url": url,
                    "post_data": req.post_data,
                })

        def on_response(resp: Response):
            url = resp.url
            if ("be-prod.rib.gg" in url or "rib.gg/api" in url) and resp.status < 400:
                # Try to read the JSON body (may fail for 304s)
                for entry in captured:
                    if entry["url"] == url and "response_preview" not in entry:
                        try:
                            body = resp.json()
                            entry["response_preview"] = json.dumps(body, indent=2)[:800]
                            entry["response_keys"] = list(body.keys()) if isinstance(body, dict) else f"[list, len={len(body)}]"
                        except Exception:
                            pass
                        break

        page.on("request",  on_request)
        page.on("response", on_response)

        # Use "load" not "networkidle" — CF challenge keeps network busy and
        # would cause networkidle to never fire. After load, wait for any
        # remaining XHR calls to complete.
        page.goto(target_url, wait_until="load", timeout=45_000)
        print("  Page load event fired. Waiting 8s for async requests...")
        page.wait_for_timeout(8_000)

        # Also scan for __NEXT_DATA__ embedded in the page HTML
        next_data_raw = page.evaluate("""
            () => {
                const el = document.getElementById('__NEXT_DATA__');
                return el ? el.textContent : null;
            }
        """)

        html_content = page.content()
        browser.close()

    # ── Report API calls ──────────────────────────────────────────────────
    print("=" * 65)
    print(f"  be-prod.rib.gg REQUESTS CAPTURED ({len(captured)} total)")
    print("=" * 65)

    if not captured:
        print("\n  No requests to be-prod.rib.gg were intercepted.")
        print("  The series data is likely embedded in the HTML page (SSR).")
    else:
        for i, entry in enumerate(captured, 1):
            print(f"\n  [{i}] {entry['method']}  {entry['url']}")
            if entry.get("post_data"):
                print(f"       body: {entry['post_data'][:200]}")
            if entry.get("response_keys"):
                print(f"       response keys: {entry['response_keys']}")
            if entry.get("response_preview"):
                print(f"       preview: {entry['response_preview'][:300]}")

    # ── Check __NEXT_DATA__ ───────────────────────────────────────────────
    print(f"\n{'=' * 65}")
    if next_data_raw:
        try:
            next_data = json.loads(next_data_raw)
            DUMP_DIR.mkdir(exist_ok=True)
            out = DUMP_DIR / f"next_data_{series_id}.json"
            out.write_text(json.dumps(next_data, indent=2), encoding="utf-8")
            print(f"  __NEXT_DATA__ found and saved -> {out}")
            print(f"  Top-level keys: {list(next_data.keys())}")
            props = next_data.get("props", {})
            page_props = props.get("pageProps", {})
            print(f"  pageProps keys: {list(page_props.keys())}")
            # Look for series/match data
            for key in ("series", "match", "matches", "seriesData", "initialData", "data"):
                if key in page_props:
                    val = page_props[key]
                    print(f"\n  pageProps['{key}'] keys: "
                          f"{list(val.keys()) if isinstance(val, dict) else f'list len={len(val)}'}")
        except json.JSONDecodeError:
            print("  __NEXT_DATA__ found but could not parse as JSON")
    else:
        print("  No __NEXT_DATA__ found in page HTML.")
        print("  Checking for other embedded data patterns...")

        # Look for common patterns in the raw HTML
        for pattern in ["__NUXT__", "window.__data__", "window.__INITIAL_STATE__",
                        "window.__APP_STATE__", "initialState", "serverData"]:
            if pattern in html_content:
                print(f"  Found: {pattern}")

        DUMP_DIR.mkdir(exist_ok=True)
        html_out = DUMP_DIR / f"page_{series_id}.html"
        html_out.write_text(html_content, encoding="utf-8")
        print(f"\n  Full page HTML saved -> {html_out}")
        print("  Search it for embedded JSON data.")

    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series",   type=int, default=83369, help="Series ID to probe")
    ap.add_argument("--headless", action="store_true",     help="Run without visible browser")
    args = ap.parse_args()
    intercept(args.series, headless=args.headless)


if __name__ == "__main__":
    main()
