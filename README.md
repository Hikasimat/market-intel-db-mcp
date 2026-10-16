# Market Intel – Prospect Entry MCP Server

A **restricted, security-hardened** [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that equips Claude Desktop with authorized, blueprint-driven capabilities to research, validate, create, link, and edit records across three core data modules in Market Intel DB:

1. **Government Entities** (`/api/government-entities/`)
2. **Market Events** (`/api/market-events/`)
3. **Prospects** (`/api/prospects/`)

This server functions strictly within the authenticated Market Intel Django REST API. It is **not** an independent CRM or secondary database.

---

## 1. Supported Data Modules & Capabilities

### A. Government Entities
- Search existing government entities by name, country, and state classification.
- Retrieve complete details of an existing entity.
- Validate, confirm, and create missing government entities with deduplication and state/central hierarchy checks.
- Safely update existing government entities using validated before-and-after diff reviews.
- Return real database UUIDs for linking into prospect shareholding associations.

### B. Market Events
- Search existing market events (conferences, expos, trade summits) by event title and host country.
- Retrieve full details of an event.
- Validate, confirm, and create missing market events.
- Safely update existing market events using validated diff reviews.
- Return verified database UUIDs for linking into prospect `market_event_ids`.

### C. Prospects
- Discover the complete live prospect blueprint across all 7 business sections.
- Search existing prospects for deduplication and parent-company resolution.
- Validate prospect payloads including corporate hierarchy, offerings, contacts, shareholdings, and lookups.
- Enforce strict instant email verification for corporate and key-contact emails (`VALID` required).
- Create confirmed prospects with verified parent, government entity, and market event links.
- Safely update existing prospects with before-and-after diffs, re-verifying changed emails and lookups.

---

## 2. Mandatory Security & Safety Policies

- **Strict No-Delete Policy:** Absolutely no delete functionality is exposed or permitted. The server does not support DELETE HTTP methods, delete tools, cascade cleanup, or automatic deletion.
- **Strict Allowed Endpoints:** The API client strictly rejects any endpoint or HTTP method outside its allowlist. Only `GET`, `POST`, and `PATCH` are used; `PUT` and `DELETE` are prohibited.
- **Staged Operations & Confirmation Tokens:**
  - Validation **never writes** to the database.
  - Validation produces an immutable snapshot and issues a short-lived (10-minute), single-use token bound to the authenticated user's identity, operation type, entity type, and an HMAC-SHA256 of the payload.
  - Commit tools require the token and explicit confirmation (`user_has_explicitly_confirmed=True`).
- **Concurrent Modification (Stale Record) Detection:**
  - Before applying any update, the server re-fetches the current database record and verifies that no editable field was modified concurrently after the user reviewed the diff. If modified, the update aborts with `STALE_ABORTED`.
- **Mandatory Email Verification:**
  - Every corporate and contact email must pass `/api/email-verifications/instant-verify/` returning `VALID`. `INVALID`, `UNKNOWN`, timeouts, and service errors block prospect creation.
  - Preflight validation verifies emails, and emails are re-verified immediately before creation.
- **Role Enforcement:** Only accounts with the `PRE` (Prospect Research Entry) role are authorized. Non-PRE accounts are rejected immediately upon authentication.

---

## 3. Available MCP Tools

| Tool | Purpose | Writes? |
|---|---|---|
| `get_prospect_blueprint()` | Discovers complete schema, field constraints, choices, lookups, and validation rules | No |
| `search_existing_prospects(query, country?, limit?)` | Searches prospects for duplicates and parent company IDs | No |
| `search_government_entities(query, country?, is_state_government?, limit?)` | Searches government entities for lookup resolution | No |
| `search_market_events(query, host_country?, limit?)` | Searches market events for lookup resolution | No |
| `get_record_details(entity_type, record_id)` | Retrieves full record details for prospect, government_entity, or market_event | No |
| `validate_prospect_data(prospect)` | Validates full prospect payload, verifies emails & lookups, issues confirmation token | No |
| `create_confirmed_prospect(confirmation_token, user_has_explicitly_confirmed)` | Creates prospect using verified token after explicit user approval | Yes (`POST`) |
| `validate_government_entity(entity)` | Validates new government entity, checks duplicates, issues confirmation token | No |
| `create_confirmed_government_entity(confirmation_token, user_has_explicitly_confirmed)` | Creates government entity after explicit approval | Yes (`POST`) |
| `validate_market_event(event)` | Validates new market event, checks duplicates, issues confirmation token | No |
| `create_confirmed_market_event(confirmation_token, user_has_explicitly_confirmed)` | Creates market event after explicit approval | Yes (`POST`) |
| `validate_record_creation(entity_type, data)` | Generic creation validator across all three modules | No |
| `create_confirmed_record(confirmation_token, user_has_explicitly_confirmed)` | Generic confirmed record creator | Yes (`POST`) |
| `validate_record_update(entity_type, record_id, changes)` | Merges changes, validates full record, computes before/after diff, issues update token | No |
| `update_confirmed_record(confirmation_token, user_has_explicitly_confirmed)` | Applies approved update with stale concurrency check | Yes (`PATCH`) |

---

## 4. API Allowlist

Enforced strictly in `api_client.py` before any network access occurs:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/auth/login/` | Obtain JWT access and refresh cookies |
| `POST` | `/api/auth/refresh/` | Refresh expired access token |
| `GET` | `/api/auth/me/` | Verify user identity and PRE role |
| `POST` | `/api/email-verifications/instant-verify/` | Instant verification for email addresses |
| `POST` | `/api/email-verifications/verify/` | Persist email verification record |
| `GET` | `/api/prospects/` | Search/list visible prospects |
| `POST` | `/api/prospects/` | Create a validated prospect |
| `GET` | `/api/prospects/<uuid>/` | Retrieve detail of a prospect |
| `PATCH`| `/api/prospects/<uuid>/` | Update editable fields on a prospect |
| `GET` | `/api/government-entities/` | Search/list government entities |
| `POST` | `/api/government-entities/` | Create a government entity |
| `GET` | `/api/government-entities/<uuid>/` | Retrieve detail of a government entity |
| `PATCH`| `/api/government-entities/<uuid>/` | Update editable fields on a government entity |
| `GET` | `/api/market-events/` | Search/list market events |
| `POST` | `/api/market-events/` | Create a market event |
| `GET` | `/api/market-events/<uuid>/` | Retrieve detail of a market event |
| `PATCH`| `/api/market-events/<uuid>/` | Update editable fields on a market event |

Any attempt to call unapproved paths, methods (e.g., `DELETE`, `PUT`), or arbitrary URLs raises `EndpointNotAllowed` without sending any request.

---

## 5. Blueprint-Driven Research Workflow

When Claude researches a company, it follows this strict 5-step sequence:

```
Step 1: Inspect Blueprint
  └─ Call get_prospect_blueprint() to obtain full schema, rules, choices, and lookups.

Step 2: Research Company
  └─ Search reliable public corporate filings, official websites, and regulatory portals.

Step 3: Research Every Field
  ├─ Corporate Structure & Parent Auto-Linking (Parent/Branch/Subsidiary/Merger/Acquisition)
  ├─ General Information (Name, Country, Address, Industry, Sector, Status, Phone, Email, Web, LinkedIn)
  ├─ Shareholding / Ownership (Company, Individual, Government, Other, with % distribution)
  ├─ Market Offerings (Products, Services, Solutions, Multiple)
  ├─ Market Events (Associated industry conferences and summits)
  ├─ Key Contacts (Name, Designation, Verified Email, Phone, LinkedIn)
  └─ Government Entity (Government Entity Name * for public sector/government shareholding)

Step 4: Resolve Lookups (Dependency Order)
  ├─ 1. Search existing Government Entities -> Create missing if approved -> Retrieve UUID
  ├─ 2. Search existing Market Events -> Create missing if approved -> Retrieve UUID
  ├─ 3. Search existing Prospects for Parent Company -> Retrieve UUID
  └─ 4. Re-verify emails with instant verification

Step 5: Produce Structured Review
  └─ Present complete data, sources, and diff to user -> Obtain explicit confirmation -> Commit
```

---

## 6. Setup and Installation

### Prerequisites
- Python 3.11+ (tested on Python 3.14.2)
- Network access to Market Intel API endpoint

### 1. Local Setup
```powershell
cd C:\path\to\mcp\prospect-entry-server
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. Configure Environment (`.env`)
Copy `.env.example` to `.env`:
```env
MARKET_INTEL_API_BASE_URL=https://api.yourdomain.com/api
MARKET_INTEL_USERNAME=your_pre_user
MARKET_INTEL_PASSWORD=your_pre_password
MARKET_INTEL_TOKEN_TTL_SECONDS=600
MCP_LOG_LEVEL=INFO
```

### 3. Verify Connection
```powershell
.\.venv\Scripts\python.exe server.py --check
```
Output:
```
OK: authenticated as 'your_pre_user' with role PRE.
```

### 4. Claude Desktop Configuration
In `%APPDATA%\Claude\claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "market-intel": {
      "command": "C:\\path\\to\\mcp\\prospect-entry-server\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\mcp\\prospect-entry-server\\server.py"]
    }
  }
}
```
Restart Claude Desktop completely from system tray.

### 5. Remote Docker / SSE Deployment
The server supports remote deployment via Docker and Server-Sent Events (SSE):
```bash
docker compose up -d --build
```
Or directly:
```bash
python server.py --sse --host 0.0.0.0 --port 8001
```
Configure remote Claude Desktop using the SSE URL:
```json
{
  "mcpServers": {
    "market-intel-remote": {
      "url": "https://mcp.yourdomain.com/sse"
    }
  }
}
```

---

## 7. Running Tests

Run the comprehensive mocked test suite:
```powershell
.\.venv\Scripts\pytest -v
```
All 148 unit and mock integration tests execute across all 3 modules:
- Allowlist and network restriction tests
- Stdio protocol discovery
- Blueprint schema discovery
- Government entity search, creation, duplicate prevention, and update
- Market event search, creation, duplicate prevention, and update
- Prospect creation with lookups and email verification
- Staged confirmation token security and single-use enforcement
- Concurrency stale-record detection
