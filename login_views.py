"""Interactive login view for OAuth 2.0 authorization code flow."""
from __future__ import annotations

import html
import logging
import secrets
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from config import Settings
from mcp.server.auth.provider import construct_redirect_uri
from oauth_store import OAuthStore

log = logging.getLogger("prospect_mcp.login")

LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sign in — Market Intel DB</title>
  <style>
    :root {
      --bg: #090d16;
      --card-bg: rgba(18, 24, 38, 0.95);
      --border: rgba(255, 255, 255, 0.08);
      --accent: #3b82f6;
      --accent-hover: #2563eb;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --error-bg: rgba(239, 68, 68, 0.15);
      --error-border: #ef4444;
      --error-text: #fca5a5;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: radial-gradient(circle at 50% 20%, #1e293b 0%, var(--bg) 70%);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      color: var(--text-main);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }
    .card {
      width: 100%;
      max-width: 420px;
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 1rem;
      padding: 2.25rem;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5), inset 0 1px 0 rgba(255, 255, 255, 0.06);
      backdrop-filter: blur(12px);
    }
    .header {
      text-align: center;
      margin-bottom: 2rem;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      background: rgba(59, 130, 246, 0.15);
      color: #60a5fa;
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      padding: 0.25rem 0.65rem;
      border-radius: 9999px;
      margin-bottom: 0.75rem;
      border: 1px solid rgba(59, 130, 246, 0.25);
    }
    .badge-dot {
      width: 6px;
      height: 6px;
      background: #60a5fa;
      border-radius: 50%;
    }
    h1 {
      font-size: 1.5rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      margin-bottom: 0.5rem;
    }
    p.subtitle {
      color: var(--text-muted);
      font-size: 0.875rem;
      line-height: 1.4;
    }
    .alert {
      background: var(--error-bg);
      border: 1px solid var(--error-border);
      color: var(--error-text);
      padding: 0.75rem 1rem;
      border-radius: 0.5rem;
      font-size: 0.875rem;
      margin-bottom: 1.5rem;
    }
    .form-group {
      margin-bottom: 1.25rem;
    }
    label {
      display: block;
      font-size: 0.825rem;
      font-weight: 500;
      color: var(--text-muted);
      margin-bottom: 0.4rem;
    }
    input[type="text"], input[type="password"] {
      width: 100%;
      background: rgba(15, 23, 42, 0.6);
      border: 1px solid var(--border);
      color: var(--text-main);
      padding: 0.75rem 1rem;
      border-radius: 0.5rem;
      font-size: 0.95rem;
      outline: none;
      transition: border-color 0.15s, box-shadow 0.15s;
    }
    input[type="text"]:focus, input[type="password"]:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2);
    }
    .submit-btn {
      width: 100%;
      background: var(--accent);
      color: #fff;
      font-weight: 600;
      font-size: 0.95rem;
      padding: 0.85rem;
      border: none;
      border-radius: 0.5rem;
      cursor: pointer;
      margin-top: 0.5rem;
      transition: background-color 0.15s, transform 0.05s;
    }
    .submit-btn:hover {
      background: var(--accent-hover);
    }
    .submit-btn:active {
      transform: scale(0.99);
    }
    .footer {
      margin-top: 1.75rem;
      text-align: center;
      font-size: 0.75rem;
      color: var(--text-muted);
      border-top: 1px solid var(--border);
      padding-top: 1rem;
      line-height: 1.5;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <div class="badge"><span class="badge-dot"></span> Market Intel DB</div>
      <h1>Sign in to Connect</h1>
      <p class="subtitle">Authenticate with your Market Intel PRE account to enable prospect data entry.</p>
    </div>

    {error_block}

    <form method="POST" action="/login">
      <input type="hidden" name="session_id" value="{session_id}">
      <div class="form-group">
        <label for="username">Username</label>
        <input type="text" id="username" name="username" value="{username}" autocomplete="username" required autofocus>
      </div>
      <div class="form-group">
        <label for="password">Password</label>
        <input type="password" id="password" name="password" autocomplete="current-password" required>
      </div>
      <button type="submit" class="submit-btn">Authorize Claude</button>
    </form>

    <div class="footer">
      Secured via OAuth 2.0 PKCE Authorization.<br>
      Only accounts with the <strong>PRE</strong> role are authorized to enter prospect data.
    </div>
  </div>
</body>
</html>
"""

EXPIRED_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><title>Session Expired — Market Intel DB</title>
  <style>
    body { background: #090d16; color: #f8fafc; font-family: sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
    .box { max-width: 400px; text-align: center; padding: 2rem; background: #121826; border-radius: 1rem; border: 1px solid rgba(255,255,255,0.08); }
    h1 { font-size: 1.3rem; margin-bottom: 0.75rem; }
    p { color: #94a3b8; font-size: 0.9rem; line-height: 1.5; }
  </style>
</head>
<body>
  <div class="box">
    <h1>Session Expired</h1>
    <p>This authorization session is invalid or has expired.</p>
    <p style="margin-top: 1rem;">Please return to Claude, open Connectors, and click <strong>Connect</strong> to try again.</p>
  </div>
</body>
</html>
"""


def render_login(session_id: str, username: str = "", error_message: str | None = None) -> HTMLResponse:
    error_block = f'<div class="alert">{html.escape(error_message)}</div>' if error_message else ""
    page = (
        LOGIN_PAGE_HTML
        .replace("{session_id}", html.escape(session_id))
        .replace("{username}", html.escape(username))
        .replace("{error_block}", error_block)
    )
    return HTMLResponse(page)


def create_login_handler(store: OAuthStore, settings: Settings):
    async def login_endpoint(request: Request) -> Response:
        if request.method == "GET":
            session_id = request.query_params.get("session_id", "").strip()
            if not session_id:
                return HTMLResponse(EXPIRED_PAGE_HTML, status_code=400)
            session = store.get_auth_session(session_id)
            if not session:
                return HTMLResponse(EXPIRED_PAGE_HTML, status_code=400)
            return render_login(session_id=session_id)

        if request.method == "POST":
            form = await request.form()
            session_id = str(form.get("session_id", "")).strip()
            username = str(form.get("username", "")).strip()
            password = str(form.get("password", ""))

            if not session_id:
                return HTMLResponse(EXPIRED_PAGE_HTML, status_code=400)

            session = store.get_auth_session(session_id)
            if not session:
                return HTMLResponse(EXPIRED_PAGE_HTML, status_code=400)

            if not username or not password:
                return render_login(session_id, username, "Username and password are required.")

            # Authenticate against Market Intel Django API
            try:
                async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                    # 1. Login
                    login_resp = await client.post(
                        f"{settings.api_base_url}/auth/login/",
                        json={"username": username, "password": password},
                    )
                    if login_resp.status_code != 200:
                        return render_login(session_id, username, "Invalid username or password.")

                    data = login_resp.json()
                    access_token = data.get("access") or data.get("access_token") or login_resp.cookies.get("access_token")
                    refresh_token = data.get("refresh") or login_resp.cookies.get("refresh_token")
                    if not access_token:
                        return render_login(session_id, username, "Login succeeded but no access token was returned.")

                    # 2. Check /auth/me/ to verify PRE role
                    me_resp = await client.get(
                        f"{settings.api_base_url}/auth/me/",
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                    if me_resp.status_code != 200:
                        return render_login(session_id, username, "Failed to verify account permissions.")

                    user_info = me_resp.json()
                    user = user_info.get("user", user_info) if isinstance(user_info, dict) else {}
                    role = user.get("role")
                    if isinstance(role, dict):
                        role = role.get("name") or role.get("code")
                    role = str(role or "").strip().upper()

                    if role != "PRE":
                        return render_login(
                            session_id,
                            username,
                            f"Account role is '{role or 'Unknown'}'. Only accounts with the PRE role are permitted.",
                        )

                    user_id = str(user.get("id") or user.get("pk") or "")
                    subject = f"{user_id}:{username}"

            except Exception as exc:
                log.exception("Error during OAuth login authentication")
                return render_login(session_id, username, f"Could not connect to authentication service: {exc}")

            # Authentication succeeded! Mint authorization code
            code = f"mcp_code_{secrets.token_urlsafe(32)}"
            store.save_auth_code(
                code=code,
                client_id=session.client_id,
                redirect_uri=session.redirect_uri,
                code_challenge=session.code_challenge or "",
                scopes=session.scopes,
                subject=subject,
                django_access_token=access_token,
                django_refresh_token=refresh_token,
                ttl_seconds=300,
            )

            # Consume the auth session
            store.delete_auth_session(session_id)

            # Redirect user back to Claude's redirect URI
            target_url = construct_redirect_uri(session.redirect_uri, code=code, state=session.state)
            return RedirectResponse(url=target_url, status_code=302)

        return Response(status_code=405)

    return login_endpoint
