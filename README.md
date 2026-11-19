# Market Intel DB — Prospect Research & Entry MCP Server

A **restricted, security-hardened** [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server combining:
1. **An intelligent, evidence-based Company & Event Research Engine** aligned dynamically with Vortexen's actual services (`https://vortexen.com/`).
2. **Blueprint-driven validation and explicit confirmation workflows** for **Government Entities**, **Market Events**, and **Prospects** in Market Intel DB.

This server enforces a **strict read-only research model** and **single-use HMAC-bound confirmation tokens** for every database creation or update. It is **not** an independent CRM and never performs unapproved writes or deletions.

---

## 1. Architecture & Modules

```text
mcp/prospect-entry-server/
├── server.py                  # FastMCP server, tool registration, OAuth/Bearer middleware, hardened CORS
├── api_client.py              # Strict allowlisted Market Intel Django REST API client (GET/POST/PATCH only)
├── config.py                  # Validated environment settings & secret scrubbing helpers
├── schemas.py                 # Authoritative Prospect, GovernmentEntity, and MarketEvent schemas + blueprint
├── validation.py              # Pure business-rule validation, duplicate matching, diff calculation
├── workflow.py                # Search -> validate -> confirm -> create/update orchestration
├── confirmation_store.py      # Short-lived, single-use, HMAC-signed confirmation token store
├── oauth_provider.py          # OAuth 2.0 Authorization Code + PKCE provider with SSRF-guarded CIMD
├── oauth_store.py             # SQLite OAuth storage (SHA-256 hashed tokens/codes, WAL mode)
├── login_views.py             # Hardened interactive PRE login UI with strict security headers
└── research/                  # Read-only intelligent company & event research subsystem
    ├── service.py             # Top-level ResearchService orchestrator
    ├── company_identity.py    # Legal-name normalization, official domain selection, duplicate matching
    ├── search.py              # Brave / Tavily / Serper / DuckDuckGo search adapter
    ├── fetcher.py             # SSRF-safe, size-bounded, robots-aware HTML/JSON parser
    ├── extractor.py           # Prioritized 6-section extractor with field-level caching
    ├── offerings.py           # Highest-priority Primary Industries & Market Offerings extractor
    ├── corporate_structure.py # Corporate hierarchy, enum mapping & Market Intel parent auto-linking
    ├── ownership.py           # Evidence-backed shareholders (separated from directors, no guessed %)
    ├── contacts.py            # Strict Key Contacts extraction + Market Intel email verification
    ├── market_events.py       # Event verification, exhibitor/sponsor/speaker discovery & company research
    ├── vortexen_profile.py    # Dynamic https://vortexen.com/ crawler, versioned profile & refresh
    ├── provider_router.py     # Multi-provider AI gateway (Groq, Anthropic Claude, OpenAI) + cost tracker
    ├── prompts.py             # Compact JSON prompts with <untrusted_source_data> isolation
    ├── verifier.py            # Grounding checks, conflict resolution & strict contact validation
    ├── scoring.py             # Source authority hierarchy, confidence scoring & Vortexen relevance rubric
    ├── cache.py               # Persistent SQLite research cache (identities, sources, fields, LLM, usage)
    ├── security.py            # SSRF/DNS-rebinding defense, MIME/size limits, injection & secret filters
    └── models.py              # Pydantic models, controlled verification statuses & ResearchBudget
```

### Removed WhatsApp Functionality
All WhatsApp MCP tools, schemas, workflow logic, QR code endpoints, WAHA endpoints, WhatsApp session/role settings, and WhatsApp tests have been **completely removed**. Only **Government Entities**, **Market Events**, **Prospects**, and the **Research Engine** remain active.

---

## 2. Research Engine & Six-Section Workflow

When researching a company (`research_company_prospect`), the assistant follows this pipeline:

```text
get_prospect_blueprint()
   │
   ▼
Company Identity Resolution & Duplicate Check (search_existing_prospects)
   │
   ▼
Priority Web Crawling (Offerings/Products/Services/Solutions/Industries first)
   │
   ▼
6-Section Evidence Extraction & Field-Level Cache Reuse
   ├─ 1. Market Offerings & Primary Industries (Highest Priority)
   ├─ 2. General Information (Official Name, Country, Address, Phone, Verified Email, Web, LinkedIn)
   ├─ 3. Corporate Structure & Parent Auto-Linking (Parent/Branch/Subsidiary, Acquired/Merged)
   ├─ 4. Shareholding / Ownership (Verified holders & % only; directors separated)
   ├─ 5. Market Events (Matched to real Market Event UUIDs)
   └─ 6. Key Contacts (Name + Designation + Source-grounded VALID official email)
   │
   ▼
Dynamic Vortexen Profile Alignment (https://vortexen.com/)
   │
   ▼
Structured Read-Only Result (Evidence, Conflicts, Missing Fields, Prerequisites, Token Usage)
   │
   ▼
validate_prospect_data() ──▶ Explicit User Approval ──▶ create_confirmed_prospect()
```

### Highest Research Priority: Market Offerings & Primary Industries
- Internal website crawler prioritizes `/products`, `/services`, `/solutions`, `/industries`, `/capabilities`, and `/divisions` pages before secondary pages.
- Classifies **Primary Industries** from actual business activity and customer/product evidence — never solely from a company name.
- Leaves offerings or industries `UNAVAILABLE` if evidence is missing rather than generating generic filler text.

### Key Contacts Strict Rules
An AI-extracted contact is included in `prospect_candidate["key_contacts"]` **only** when:
1. `contact_name`, `designation`, and `official_email` are all present.
2. The email literally appears in a fetched source (pattern-guessed emails are rejected).
3. Company association is verified.
4. `POST /api/email-verifications/instant-verify/` returns `VALID`.

Incomplete or unverified contacts are kept in `contact_candidates` with their `exclusion_reason` and never block other company fields.

---

## 3. Dynamic Vortexen Business Profile

`VortexenProfileManager` crawls public pages on `https://vortexen.com/` (plus `sitemap.xml` when present) using `SafeFetcher`, extracts a versioned `VortexenProfile` (services, products, solutions, capabilities, target industries, geographic footprint, source URLs, retrieval timestamp, and content hash), and persists it in the research cache.

- Zero Vortexen services are hardcoded in Python prompts or logic.
- Call `refresh_vortexen_profile(force=True)` whenever the website updates; if the content hash is unchanged, the TTL extends with **zero LLM tokens**.
- Every researched prospect receives a transparent `VortexenRelevance` evaluation (`HIGH`, `MEDIUM`, `LOW`, or `INSUFFICIENT_DATA`) with explicit score breakdown and unverified gaps.

---

## 4. Event-Based Prospect Research (`research_market_event`)

1. Searches existing Market Intel Market Events first to reuse real UUIDs.
2. Resolves and fetches the official event website and exhibitor/sponsor/speaker/partner pages (respecting `robots.txt` and reporting access-restricted directories without bypassing auth or CAPTCHAs).
3. Deduplicates discovered organizations across roles.
4. Runs the **full six-section company research pipeline** concurrently (bounded by `RESEARCH_MAX_CONCURRENCY` and `max_participants`), sharing the persistent cache.
5. Attaches Vortexen alignment and participation evidence to every company candidate without performing any automatic DB writes.

---

## 5. Multi-Provider AI Architecture & Token Optimization

### Providers & Intelligent 4-Stage Routing
Configure API keys for **Groq**, **Anthropic Claude**, and/or **OpenAI API** (note: consumer ChatGPT subscriptions do not provide programmatic API access; use official API keys):

| Stage | Purpose | Default Routing |
|---|---|---|
| **Stage 1: Deterministic Python** | URL normalization, HTML/JSON-LD/table parsing, duplicate matching, enum validation, email/phone extraction, cache lookup | Zero LLM tokens |
| **Stage 2: Lightweight Primary Model** | Structured field extraction (`industries_and_offerings`, `general_information`, `key_contacts`, `event_participants`) | `RESEARCH_PRIMARY_LLM_PROVIDER` (default `groq` / `llama-3.3-70b-versatile`) |
| **Stage 3: Advanced Escalation Model** | Ambiguous corporate structures, unresolved source conflicts, low-confidence cross-checks | `RESEARCH_ESCALATION_LLM_PROVIDER` (default `anthropic` / `claude-sonnet-4-20250514`) |
| **Stage 4: Source Verification** | Verifies extracted quotes/emails against fetched source text and runs Market Intel `instant-verify` | Grounding + API check |

Only providers whose API keys are configured are enabled. If a provider encounters transient errors, routing falls back across remaining configured providers automatically. If no LLM keys are configured, Stage 1 deterministic extraction continues working cleanly.

### Token Optimization & Cost Tracking
- Compact, field-scoped JSON prompts bounded by `RESEARCH_MAX_CONTEXT_CHARS`.
- Persistent SQLite caching (`research_cache.db`) across **company identities**, **fetched sources**, **field-level verified values + source hashes**, **LLM provider responses**, and **Vortexen profile**.
- Field-level incremental refresh: skips verified fresh fields unless expired or source content hash changes.
- In-flight request deduplication and hard per-company budgets (`RESEARCH_MAX_LLM_CALLS_PER_COMPANY`, `RESEARCH_MAX_TOKENS_PER_COMPANY`).
- Every research response reports `usage_summary` and separates `reported_cost_usd` from `estimated_cost_usd`.

---

## 6. Evidence Verification & Security Controls

### Field-Level Verification Statuses
`VERIFIED` | `PARTIALLY_VERIFIED` | `UNAVAILABLE` | `CONFLICTING` | `REQUIRES_REVIEW`

Source authority priority: Official Website / Government Registry / Regulatory Filing > Annual Report / Stock Exchange > Market Intel DB > Official Event Website > Official Press Release > Business Directory / Professional Profile > Industry Publication > Web Search Snippet.

### Security Controls
- **Strict API Allowlist**: Only approved `/prospects/`, `/government-entities/`, `/market-events/`, `/auth/*`, and `/email-verifications/*` routes using `GET`, `POST`, and `PATCH`. `DELETE` and `PUT` are always blocked.
- **SSRF & DNS Rebinding Protection**: Blocks non-HTTP(S) schemes, credentials in URLs, private/loopback/link-local/CGNAT/metadata IPs (`127.0.0.1`, `10.0.0.0/8`, `169.254.169.254`, `::1`, `fc00::/7`), internal hostnames, non-standard ports, unsafe redirects (max 5 hops, each re-validated), disallowed MIME types, and oversized responses; resolves and connects directly to validated public IPs at TCP connect time.
- **Prompt Injection Defense**: External pages are stripped of scripts/styles/hidden control chars, scanned for injection patterns, escaped, and fenced inside `<untrusted_source_data>` tags with system instructions forbidding execution.
- **OAuth 2.0 + PKCE & Transport Security**:
  - Remote HTTP/SSE transports **refuse to start** without `MCP_OAUTH_ENABLED=true` or `MCP_AUTH_TOKEN`.
  - OAuth codes and MCP tokens are stored as SHA-256 hashes in `oauth.db`; authorization codes are single-use; PKCE S256 enforced; CIMD client fetches use SSRF protection.
  - Strict `PRE` role enforcement across both API authentication and OAuth login.
  - Explicit CORS allowlist (`MCP_CORS_ALLOWED_ORIGINS`); wildcard `*` with credentials is rejected at startup.
  - Constant-time Bearer token comparison (`MCP_ALLOW_QUERY_TOKEN=false` by default).
  - Automatic secret redaction filter on all logs.

---

## 7. Available MCP Tools

| Tool | Purpose | Writes DB? |
|---|---|---|
| `get_prospect_blueprint()` | Authoritative Prospect schema, enums, lookups, conditional rules | No |
| `research_company_prospect(...)` | Full 6-section evidence-backed company research + Vortexen relevance | No |
| `research_market_event(...)` | Event verification, participant discovery + company research | No |
| `get_research_cache_status(...)` | Inspect cache freshness, enabled providers, cumulative token usage | No |
| `refresh_vortexen_profile(force?)` | Refresh cached `https://vortexen.com/` business profile | No |
| `search_existing_prospects(...)` | Search existing prospects for duplicates / parent UUIDs | No |
| `search_government_entities(...)` | Search existing government entities for lookup UUIDs | No |
| `search_market_events(...)` | Search existing market events for lookup UUIDs | No |
| `get_record_details(...)` | Fetch full details of one prospect/gov entity/event | No |
| `validate_prospect_data(...)` | Validate prospect payload + emails + lookups; issues token | No |
| `create_confirmed_prospect(...)` | Create prospect after explicit approval + re-verification | Yes (`POST`) |
| `validate_government_entity(...)` | Validate new government entity; issues token | No |
| `create_confirmed_government_entity(...)` | Create government entity after explicit approval | Yes (`POST`) |
| `validate_market_event(...)` | Validate new market event; issues token | No |
| `create_confirmed_market_event(...)` | Create market event after explicit approval | Yes (`POST`) |
| `validate_record_creation(...)` | Generic creation validation dispatcher | No |
| `create_confirmed_record(...)` | Generic confirmation dispatcher | Yes (`POST`/`PATCH`) |
| `validate_record_update(...)` | Merge edits, validate, compute before/after diff, issue token | No |
| `update_confirmed_record(...)` | Apply approved patch with stale concurrency guard | Yes (`PATCH`) |

---

## 8. Installation, Configuration & Deployment

### Environment Variables
Copy `.env.example` to `.env` and fill in your credentials (never commit `.env`):
- **Required**: `MARKET_INTEL_API_BASE_URL`, `MARKET_INTEL_USERNAME`, `MARKET_INTEL_PASSWORD`
- **HTTP Auth & Security**: `MCP_AUTH_TOKEN` (or `MCP_OAUTH_ENABLED=true` + `MCP_OAUTH_ISSUER_URL`), `MCP_CORS_ALLOWED_ORIGINS`, `MCP_ALLOW_QUERY_TOKEN=false`, `MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS`, `MCP_OAUTH_REFRESH_TOKEN_TTL_SECONDS`
- **AI Providers**: `GROQ_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `RESEARCH_PRIMARY_LLM_PROVIDER`, `RESEARCH_ESCALATION_LLM_PROVIDER`, `RESEARCH_FALLBACK_LLM_PROVIDER`, `RESEARCH_PRIMARY_MODEL`, `RESEARCH_ESCALATION_MODEL`, `RESEARCH_FALLBACK_MODEL`
- **Search**: `RESEARCH_SEARCH_PROVIDER`, `BRAVE_SEARCH_API_KEY`, `TAVILY_API_KEY`, `SERPER_API_KEY`, `RESEARCH_SEARCH_ENABLE_DDG_FALLBACK`
- **Research & Cache**: `VORTEXEN_WEBSITE_URL`, `VORTEXEN_PROFILE_TTL_SECONDS`, `RESEARCH_CACHE_DB_PATH`, TTLs, concurrency & budget settings

### Local Setup (stdio)
```powershell
cd mcp\prospect-entry-server
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe server.py --check
```

### MCP Client Configuration (Claude Desktop stdio)
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

### Remote & Docker Deployment
```bash
docker compose up -d --build
```
Remote HTTP/SSE mode requires either OAuth (`--oauth` / `MCP_OAUTH_ENABLED=true`) or Bearer token auth (`MCP_AUTH_TOKEN`):
```bash
python server.py --transport streamable-http --host 0.0.0.0 --port 8001
```
Persistent SQLite files (`/data/oauth.db`, `/data/research_cache.db`) are stored in the mounted `/data` volume. Production deployments run via `.github/workflows/deploy.yml` (test -> build/push ECR -> SSM rollout with preflight `--check` and automatic rollback).

---

## 9. Testing & Troubleshooting

Run the full mocked test suite:
```powershell
.\.venv\Scripts\python.exe -m pytest -v
```
All unit, security, provider, cache, research, OAuth, and regression tests run completely mocked against `respx` and `FakeClient`. Real backend tests in `tests/test_integration_real.py` remain skipped unless `MI_INTEGRATION=1` is explicitly set against a test backend.

### Troubleshooting
- **Startup fails with `Remote HTTP/SSE transport requires authentication`**: Set `MCP_OAUTH_ENABLED=true` (with `MCP_OAUTH_ISSUER_URL`) or `MCP_AUTH_TOKEN` before launching HTTP/SSE mode.
- **CORS error in browser client**: Add the exact `https://origin` (no trailing slash or `*`) to `MCP_CORS_ALLOWED_ORIGINS`.
- **`enabled_ai_providers` is empty**: Verify `GROQ_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY` is configured in `.env`; Stage 1 deterministic extraction still works without LLM keys.
- **Contact excluded during research**: AI contacts require `contact_name`, `designation`, a source-grounded `official_email`, and `VALID` status from Market Intel email verification.
