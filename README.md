# Market Intel – Prospect Entry MCP Server

A **restricted** [Model Context Protocol](https://modelcontextprotocol.io) server (Python, stdio) that lets
Claude Desktop enter **new** prospects into the existing Market Intel DB through its existing Django REST API.
It is not a CRM: no database, no UI, no update, no delete, no bulk import, no generic HTTP tool.

## Tools (exactly three)

| Tool | Purpose | Writes? |
|---|---|---|
| `search_existing_prospects(query, country?, limit?)` | Find possible duplicates / parent-company IDs (minimal fields, limit ≤ 25) | No |
| `validate_prospect_data(prospect)` | Validate everything, verify **every** supplied email, issue a one-time token only if all passes | No |
| `create_confirmed_prospect(confirmation_token, user_has_explicitly_confirmed)` | Create ONE prospect from the exact reviewed payload | Yes (one `POST /prospects/`) |

> `user_has_explicitly_confirmed` is a small addition to the brief's input (`confirmation_token`) so Claude
> must assert that the user approved. The token itself is still required, single-use, and payload-bound.

### API allowlist (enforced in `api_client.py`; anything else raises `EndpointNotAllowed`)

| Method | Path (under `MARKET_INTEL_API_BASE_URL`, which ends in `/api`) |
|---|---|
| GET | `/prospects/` |
| POST | `/prospects/` |
| POST | `/auth/login/`, `/auth/refresh/` |
| GET | `/auth/me/` |
| POST | `/email-verifications/instant-verify/`, `/email-verifications/verify/` |

No PUT/PATCH/DELETE exists in the client; redirects are not followed; the model never supplies URLs, methods or headers.

## Codebase Verification & Alignment

The backend (`backend/prospects`, `backend/accounts`) and frontend (`frontend/src`) code has been thoroughly inspected (read-only):

1. **Authentication (`backend/accounts`):**
   - `POST /api/auth/login/` via `CookieTokenObtainPairView`: returns `{"access": "<token>"}` in response data and sets `access_token` and `refresh_token` HttpOnly cookies.
   - `POST /api/auth/refresh/` via `CookieTokenRefreshView`: reads `refresh_token` cookie when not provided in body, returns updated access token and resets cookies.
   - `GET /api/auth/me/` via `UserMeView`: returns top-level `{id, username, email, role, ...}`, where `role` is verified to be `'PRE'`.
2. **Instant Email Verification (`backend/prospects/verification_views.py`):**
   - `POST /api/email-verifications/instant-verify/` returns `{"verification_status": "VALID"|"INVALID"|"UNKNOWN", "reason": "..."}`.
   - Handled directly in `api_client.py` by checking both `verification_status` and fallback `status`.
3. **Persist Verification (`backend/prospects/verification_views.py`):**
   - `POST /api/email-verifications/verify/` takes `{prospect_id, email_address}` for company email, and `{prospect_id, email_address, prospect_contact_id}` for contact emails.
   - `api_client.py` uses `prospect_contact_id` for key contacts so the backend accurately associates the verification with the contact record.
4. **Acquisitions & Mergers (`backend/prospects/serializers.py` & `models.py`):**
   - `acquiring_companies_data`: array of `{acquiring_company: UUID, acquired_shares: Optional[float]}`.
   - `merging_companies` and `dissolved_companies`: lists of prospect UUIDs.
5. **Shareholdings (`backend/prospects/serializers.py` & `models.py`):**
   - `holder_type`: `'Company'` (with `company` UUID or `is_self=True`), `'Individual'` (with `contact_index` into `key_contacts`), `'Government'` (with `government_entity` UUID), or `'Other'` (with `other_name`).
6. **Duplicate Validation:**
   - Preflight search uses `GET /api/prospects/?search=...` (scoped to PRE's own prospects).
   - Creation `POST /api/prospects/` enforces global case-insensitive `Prospect.objects.filter(company_name__iexact=...).exists()`, returning HTTP 400 `{"error": "A prospect with this company name has already been entered."}`.
7. **Round-robin LQ Assignment & PRE Tracking:**
   - In `ProspectViewSet.perform_create`, `created_by` is stamped with `request.user.username`, and `assign_lq_to_prospect` assigns an LQ in round-robin batches of 5 automatically.
8. **Rejected by design:** Government shareholders and `market_event_ids` are rejected during preflight validation because their references cannot be safely verified through the permitted read APIs.

## Safety model

- Validation never creates anything. Tokens: 256-bit random, in-memory only, single-use, 10 min TTL (configurable),
  bound to the authenticated identity and to an HMAC of the exact payload (tamper ⇒ refused). Only a hash of the token is stored.
- Every supplied company/contact email must be `VALID` at validation **and again immediately before creation**. `INVALID`,
  `UNKNOWN`, timeouts, network errors, malformed or missing status all block. No override exists.
- Duplicates: a visible exact (case-insensitive) match blocks; similar names warn. "No match" is never proof of uniqueness; the
  backend's global check is authoritative and its error is returned as-is (never retried or re-spelled).
- Creation is one non-retried `POST`. On timeout/network error the result is `UNCERTAIN`: no retry, and an identical payload
  cannot be re-created in the same session; search first.
- After creation, emails are persisted via `/email-verifications/verify/`. Differences/failures are reported; the prospect is never deleted.
- Non-PRE accounts are rejected. Credentials/tokens are never logged or returned. HTTPS required except for localhost.
- Because tokens live in process memory, restarting the server (or Claude Desktop) invalidates pending tokens: just validate again.

## Windows installation

1. **Python 3.11+** (built/tested with 3.14.2): install from <https://www.python.org/downloads/> (tick *Add python.exe to PATH*), then `python --version`.
2. **Virtual environment** (PowerShell):
   ```powershell
   cd C:\path\to\repo\mcp\prospect-entry-server
   python -m venv .venv
   ```
3. **Dependencies:**
   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
4. **`.env`:** `Copy-Item .env.example .env`, then edit it (never commit it; it is git-ignored):
   ```env
   MARKET_INTEL_API_BASE_URL=https://your-host/api      # or http://localhost:8000/api for dev
   MARKET_INTEL_USERNAME=dedicated_pre_username
   MARKET_INTEL_PASSWORD=...
   ```
   Use a dedicated **PRE** account (not LQ/Manager/superuser).
5. **Health check** (authenticates and confirms the PRE role; prints no secrets):
   ```powershell
   .\.venv\Scripts\python.exe server.py --check
   # OK: authenticated as 'dedicated_pre_username' with role PRE.
   ```
6. **Register in Claude Desktop:** *Settings → Developer → Edit Config* (file `%APPDATA%\Claude\claude_desktop_config.json`).
   Merge in [claude_desktop_config.example.json](claude_desktop_config.example.json), using your absolute paths and escaped backslashes:
   ```json
   {
     "mcpServers": {
       "market-intel-prospect-entry": {
         "command": "C:\\path\\to\\repo\\mcp\\prospect-entry-server\\.venv\\Scripts\\python.exe",
         "args": ["C:\\path\\to\\repo\\mcp\\prospect-entry-server\\server.py"]
       }
     }
   }
   ```
   Secrets stay in `.env` (loaded from the server folder), not in this file.
7. **Restart Claude Desktop completely** (quit from the system-tray icon, then reopen).
8. **Confirm tools:** click the tools/connector icon in the chat box → `market-intel-prospect-entry` must list exactly
   `search_existing_prospects`, `validate_prospect_data`, `create_confirmed_prospect`.
9. **Logs:** `%APPDATA%\Claude\logs\mcp-server-market-intel-prospect-entry.log` (and `mcp.log`). Set `MCP_LOG_LEVEL=DEBUG` for more detail
   (secrets are still never logged). Common errors: `Configuration error` (fix `.env`), `only PRE accounts are permitted`
   (wrong account), `Login rejected` (credentials / auth-response assumption), `HTTP 403` (account lacks PRE permissions).

Optional: inspect with the MCP Inspector: `npx @modelcontextprotocol/inspector .\.venv\Scripts\python.exe server.py`.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

- **Mocked (default):** all tests with a fake client or `respx`-mocked HTTP. No network, no records created. These do **not** prove the real backend works.
- **Real protocol test:** `test_stdio_discovery.py` spawns the server over stdio and lists tools with the official MCP client.
- **Real backend integration (skipped by default):** on a **test** backend only:
  ```powershell
  $env:MI_INTEGRATION="1"; $env:MI_TEST_EMAIL="you@yourdomain.com"
  # optional: creates ONE prospect named "MCP Integration Test <id>"
  $env:MI_TEST_ALLOW_CREATE="1"
  .\.venv\Scripts\python.exe -m pytest tests\test_integration_real.py -q
  ```
  LQ-assignment/`created_by` checks must be confirmed manually in the app after a real test creation.

## Example Claude prompts

- *Search:* "Check whether ABC Technologies already exists in Market Intel."
- *Add:* "Add ABC Technologies in India. Address: 12 MG Road, Bengaluru. Industry: IT. Private company, Parent, Active, Services.
  Website https://abc.example, email info@abc.example. Contact: Mr. Arun Kumar, Operations Manager, arun@abc.example."
  → Claude asks for anything missing, searches, validates (verifies emails), shows the summary.
- *Confirm:* "Yes, create it." → Claude calls `create_confirmed_prospect` and reports the real prospect/contact IDs.
- *Branch:* "Add ABC Pune as a Branch of Mother Corp" → Claude searches for Mother Corp's ID first; never invents it.

## Remote Hosting & Deployment

The server supports two deployment modes:
1. **Local Stdio (Default):** Claude Desktop manages the Python process locally.
2. **Remote SSE / Streamable-HTTP:** Run the MCP server on a remote server/VM/container and connect over the network.

### 1. Run as a Remote SSE Server
```bash
# Start SSE server on port 8001
python server.py --sse --host 0.0.0.0 --port 8001
```
The server exposes:
- SSE stream: `http://<host>:8001/sse`
- Message endpoint: `http://<host>:8001/messages/`

### 2. Deploy with Docker / Docker Compose
A `Dockerfile` and `docker-compose.yml` are included in this directory:
```bash
# Build and start container in the background
docker compose up -d

# Check logs
docker compose logs -f
```

### 3. Connecting Claude Desktop to a Remote MCP Server
In `claude_desktop_config.json`, configure the remote server using the SSE URL:
```json
{
  "mcpServers": {
    "market-intel-remote": {
      "url": "https://mcp.yourdomain.com/sse"
    }
  }
}
```

> **Security for Remote Hosting:**
> - Always run behind an HTTPS reverse proxy (Nginx, Caddy, or Cloudflare Tunnel).
> - Restrict access via VPN, firewall, or reverse-proxy authentication headers so unauthorized clients cannot access prospect entry tools.

## Files

`server.py` (tools & transport CLI) · `config.py` · `api_client.py` (auth + allowlist) · `schemas.py` · `validation.py` ·
`workflow.py` (search/validate/create orchestration) · `confirmation_store.py` · `Dockerfile` · `docker-compose.yml` · `tests/`
