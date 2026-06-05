"""
Export anti-strat report to Google Docs.

First-time setup (one-time):
  1. Go to https://console.cloud.google.com/
  2. Create a project → enable "Google Drive API"
  3. Credentials → Create OAuth 2.0 Client ID (Desktop app)
  4. Download JSON → save as  google_credentials.json  next to this file
  5. Run: python export_gdocs.py --team "G2 Esports" --series 103891 --map Pearl
     (browser will open to authorize — token saved for future runs)

Usage:
  python export_gdocs.py --team "G2 Esports" --series 103891 --map Pearl
"""

import argparse
import io
import os
import re
import sys
import subprocess
from pathlib import Path

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
except ImportError:
    print("Run: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib")
    sys.exit(1)

SCOPES       = ["https://www.googleapis.com/auth/drive.file"]
CREDS_FILE   = Path(__file__).parent / "google_credentials.json"
TOKEN_FILE   = Path(__file__).parent / "google_token.json"


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_service():
    if not CREDS_FILE.exists():
        print(
            "\nERROR: google_credentials.json not found.\n"
            "Steps:\n"
            "  1. Visit https://console.cloud.google.com/\n"
            "  2. Create a project → APIs & Services → Enable 'Google Drive API'\n"
            "  3. APIs & Services → Credentials → Create OAuth 2.0 Client ID (Desktop)\n"
            "  4. Download JSON → rename to google_credentials.json → place next to this file\n"
            "  5. Re-run this script.\n"
        )
        sys.exit(1)

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds)


# ── Text → HTML ───────────────────────────────────────────────────────────────

def _text_to_html(text: str, team: str, map_filter: str | None) -> str:
    title = f"Anti-Strat Report — {team}"
    if map_filter:
        title += f" ({map_filter})"

    lines = text.splitlines()
    body_parts = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            body_parts.append("<br>")
            continue

        # Major section headers (═══)
        if re.fullmatch(r"[═=]{10,}", stripped):
            continue
        if re.fullmatch(r"[-─]{10,}", stripped):
            body_parts.append("<hr style='border:1px solid #ccc;margin:6px 0'>")
            continue

        # Round header e.g.  R16 [heav] 8-7 → B [WIN]
        if re.match(r"^R\d+\s+\[", stripped):
            color = "#1a7a2a" if "[WIN]" in stripped else "#b22222"
            body_parts.append(
                f"<p style='margin:6px 0;font-weight:bold;color:{color}'>{_esc(stripped)}</p>"
            )
            continue

        # All-caps section banners (e.g.  PLAYER ROSTER, WEAPON USAGE …)
        if re.match(r"^[A-Z][A-Z \-/&]+$", stripped) and len(stripped) > 6:
            body_parts.append(
                f"<h2 style='margin:18px 0 4px;color:#1f497d'>{_esc(stripped)}</h2>"
            )
            continue

        # Map section  [Pearl]  ATK: 10 rounds …
        if stripped.startswith("[") and "]" in stripped and "rounds" in stripped.lower():
            body_parts.append(
                f"<p style='margin:10px 0 2px;font-weight:bold;color:#1f497d'>{_esc(stripped)}</p>"
            )
            continue

        # Bullet lines starting with [!]
        if stripped.startswith("[!]"):
            body_parts.append(
                f"<li style='margin:3px 0'>{_esc(stripped[3:].strip())}</li>"
            )
            continue

        # Indented stats/route lines (player → zones)
        indent = len(line) - len(line.lstrip())
        if indent >= 4:
            body_parts.append(
                f"<p style='margin:1px 0;padding-left:{min(indent,12)*5}px;"
                f"font-family:monospace;font-size:12px'>{_esc(stripped)}</p>"
            )
            continue

        body_parts.append(f"<p style='margin:3px 0'>{_esc(stripped)}</p>")

    body_html = "\n".join(body_parts)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: Arial, sans-serif; font-size: 13px; color: #222;
         max-width: 900px; margin: 30px auto; padding: 0 20px; }}
  h1   {{ color: #b22222; }}
  h2   {{ color: #1f497d; border-bottom: 2px solid #1f497d; padding-bottom: 4px; }}
  hr   {{ border: 1px solid #ccc; }}
  li   {{ margin: 4px 0; }}
</style>
</head>
<body>
<h1>{_esc(title)}</h1>
{body_html}
</body>
</html>"""


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Main ──────────────────────────────────────────────────────────────────────

def export_gdocs(team: str, series_id: int | None, map_filter: str | None):
    # 1. Run analysis and capture output
    print("Running analysis (30-90s for AI section)…")
    cmd = [sys.executable, str(Path(__file__).parent / "antistrat.py"), "--team", team]
    if series_id:
        cmd += ["--series", str(series_id)]
    if map_filter:
        cmd += ["--map", map_filter]

    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        cwd=str(Path(__file__).parent),
    )
    raw = result.stdout or result.stderr
    if not raw.strip():
        print("ERROR: antistrat.py produced no output.")
        sys.exit(1)

    print("Analysis complete. Converting to HTML…")

    # 2. Convert to HTML
    html = _text_to_html(raw, team, map_filter)

    # 3. Upload to Google Drive as Google Doc
    print("Authenticating with Google…")
    service = _get_service()

    safe = team.replace(" ", "_")
    mtag = f"_{map_filter}" if map_filter else ""
    stag = f"_s{series_id}" if series_id else ""
    doc_name = f"Anti-Strat_{safe}{mtag}{stag}"

    file_metadata = {
        "name": doc_name,
        "mimeType": "application/vnd.google-apps.document",
    }
    media = MediaIoBaseUpload(
        io.BytesIO(html.encode("utf-8")),
        mimetype="text/html",
        resumable=False,
    )

    print("Uploading to Google Docs…")
    uploaded = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id,webViewLink",
    ).execute()

    link = uploaded.get("webViewLink", "")
    file_id = uploaded.get("id", "")
    print(f"\nDone! Google Doc created:")
    print(f"  {link}")
    print(f"\nFile ID: {file_id}")
    return link


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--team",   required=True)
    ap.add_argument("--series", type=int, default=None)
    ap.add_argument("--map",    default=None)
    args = ap.parse_args()

    export_gdocs(args.team, args.series, args.map)
