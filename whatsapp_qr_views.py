"""Ephemeral, token-gated HTTP views for displaying WhatsApp pairing QR codes."""
from __future__ import annotations

import html
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from whatsapp_workflow import EphemeralQRStore

_SECURITY_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}

_HTML_CSP = (
    "default-src 'none'; "
    "img-src 'self'; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'"
)


def _render_qr_page(username: str, session_name: str, token: str, remaining_seconds: int) -> str:
    safe_user = html.escape(username or "Market Intel User", quote=True)
    safe_session = html.escape(session_name or "default", quote=True)
    safe_token = html.escape(token, quote=True)
    secs = max(1, int(remaining_seconds))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>WhatsApp QR Code — Market Intel DB</title>
  <style>
    :root {{
      --bg: #090d16;
      --card-bg: rgba(18, 24, 38, 0.96);
      --border: rgba(255, 255, 255, 0.1);
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #22c55e;
      --warn: #f59e0b;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: radial-gradient(circle at 50% 20%, #1e293b 0%, var(--bg) 70%);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      color: var(--text-main);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }}
    .card {{
      width: 100%;
      max-width: 420px;
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 1rem;
      padding: 2rem;
      text-align: center;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5), inset 0 1px 0 rgba(255, 255, 255, 0.06);
    }}
    .badge {{
      display: inline-block;
      background: rgba(34, 197, 94, 0.15);
      color: #4ade80;
      border: 1px solid rgba(34, 197, 94, 0.3);
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      padding: 0.25rem 0.75rem;
      margin-bottom: 0.9rem;
    }}
    h1 {{ font-size: 1.35rem; font-weight: 700; margin-bottom: 0.4rem; }}
    .meta {{ color: var(--text-muted); font-size: 0.85rem; margin-bottom: 1.25rem; }}
    .qr-box {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: #ffffff;
      padding: 1rem;
      border-radius: 0.75rem;
      margin-bottom: 1.25rem;
    }}
    .qr-box img {{
      display: block;
      width: 260px;
      height: 260px;
      max-width: 100%;
      image-rendering: pixelated;
    }}
    .steps {{
      text-align: left;
      color: var(--text-muted);
      font-size: 0.85rem;
      line-height: 1.55;
      background: rgba(15, 23, 42, 0.6);
      border: 1px solid var(--border);
      border-radius: 0.6rem;
      padding: 0.85rem 1rem;
      margin-bottom: 1rem;
    }}
    .steps strong {{ color: var(--text-main); }}
    .timer {{ font-size: 0.85rem; color: var(--accent); font-weight: 600; }}
    .timer.expired {{ color: var(--warn); }}
  </style>
</head>
<body>
  <main class="card">
    <div class="badge">Market Intel &middot; WhatsApp Link</div>
    <h1>Scan QR to Connect WhatsApp</h1>
    <p class="meta">Account: <strong>{safe_user}</strong> &middot; Session: <code>{safe_session}</code></p>
    <div class="qr-box">
      <img src="/whatsapp/qr/{safe_token}/image" alt="WhatsApp Pairing QR Code">
    </div>
    <div class="steps">
      1. Open <strong>WhatsApp</strong> on your phone.<br>
      2. Go to <strong>Settings &rarr; Linked Devices &rarr; Link a Device</strong>.<br>
      3. Point your camera at the QR code above, then tell Claude you scanned it.
    </div>
    <p id="timer" class="timer">Link expires in <span id="secs">{secs}</span>s</p>
  </main>
  <script>
    (function () {{
      var left = {secs};
      var el = document.getElementById("secs");
      var timerEl = document.getElementById("timer");
      var iv = setInterval(function () {{
        left -= 1;
        if (left <= 0) {{
          clearInterval(iv);
          timerEl.className = "timer expired";
          timerEl.textContent = "This QR link has expired. Ask Claude for a fresh QR code.";
        }} else if (el) {{
          el.textContent = String(left);
        }}
      }}, 1000);
    }})();
  </script>
</body>
</html>"""


def _render_expired_page() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>QR Code Expired — Market Intel DB</title>
  <style>
    body {
      background: #090d16;
      color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
      margin: 0;
    }
    .card {
      max-width: 420px;
      background: rgba(18, 24, 38, 0.96);
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 1rem;
      padding: 2rem;
      text-align: center;
    }
    h1 { font-size: 1.25rem; margin-bottom: 0.6rem; color: #fbbf24; }
    p { color: #94a3b8; font-size: 0.9rem; line-height: 1.5; }
  </style>
</head>
<body>
  <main class="card">
    <h1>WhatsApp QR Code Expired</h1>
    <p>This temporary QR link has expired or was already cleared after connecting. Return to Claude and ask for a fresh WhatsApp QR code.</p>
  </main>
</body>
</html>"""


def create_qr_page_handler(qr_store: EphemeralQRStore) -> Any:
    """Create Starlette GET handler for `/whatsapp/qr/{token}`."""

    async def qr_page_handler(request: Request) -> Response:
        token = request.path_params.get("token", "")
        rec = qr_store.get(token)
        headers = {**_SECURITY_HEADERS, "Content-Security-Policy": _HTML_CSP}
        if rec is None:
            return HTMLResponse(_render_expired_page(), status_code=410, headers=headers)
        remaining = max(1, int(rec.expires_at - qr_store.now()))
        return HTMLResponse(
            _render_qr_page(rec.username, rec.session_name, token, remaining),
            status_code=200,
            headers=headers,
        )

    return qr_page_handler


def create_qr_image_handler(qr_store: EphemeralQRStore) -> Any:
    """Create Starlette GET handler for `/whatsapp/qr/{token}/image`."""

    async def qr_image_handler(request: Request) -> Response:
        token = request.path_params.get("token", "")
        rec = qr_store.get(token)
        if rec is None:
            return Response(content=b"", status_code=410, headers=dict(_SECURITY_HEADERS))
        return Response(content=rec.image_bytes, media_type=rec.mime_type, headers=dict(_SECURITY_HEADERS))

    return qr_image_handler

