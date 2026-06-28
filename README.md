# Dev Profile Unifier

Pull, merge, and summarize public developer identities from GitHub, Stack Overflow, dev.to, and Hacker News — into a single canonical profile, stored in Supabase, exposed via FastAPI.

Render Link:  https://dev-profile-unifier.onrender.com

---

This is a FastAPI service that aggregates public profile data from four developer platforms, performs entity resolution to determine if multiple accounts belong to the same person, and enriches the canonical profile with an LLM-generated summary.

**Key capabilities:**
- Concurrent async fetching from GitHub, Stack Overflow, dev.to, and Hacker News
- Intelligent entity resolution using handle hints, name matching, email hinting, and cross-platform link-back signals
- Canonical profile creation with conflict tracking for ambiguous fields
- Background enrichment via LLM summary generation
- Comprehensive observability metrics and API call logging

---

## Architecture

### Data Flow

```
Client Request (POST /profiles/resolve)
    ↓
Check if profile exists (idempotent lookup via source_links)
    ↓
If new or needs refresh:
  ├─→ Concurrent provider fetches (GitHub, Stack Overflow, dev.to, Hacker News)
  ├─→ Store raw payloads in raw_source_data
  ├─→ Score each source (handle hints, name match, email match, link-back signals)
  ├─→ Merge canonical fields + detect conflicts
  ├─→ Update persons table with resolution status
  └─→ Generate LLM summary (enrichment in background)
    ↓
Return profile_id immediately (202 Accepted)
    ↓
Client polls GET /profiles/{profile_id} for updates
```

### Design Decisions

1. **Background Tasks**: Resolution runs asynchronously via FastAPI `BackgroundTasks`. The API returns a `profile_id` immediately so the client doesn't block.

2. **Idempotent Lookups**: If the same handles are searched again, the system reuses the existing profile. This prevents duplicate profiles in the database.

3. **Immutable Raw Data**: Raw API payloads are stored in `raw_source_data` without modification. They can be reprocessed later if scoring logic changes, without re-fetching from providers.

4. **Separate Resolution & Enrichment**: These are independent state machines. A profile can be `RESOLVED` while enrichment is `PENDING`, `LLM_RUNNING`, `READY`, or `FAILED`.

5. **Conflict Tracking**: When fields (bio, location, email) differ across sources, all variants are stored in `persons.conflicts` and returned to the client. Nothing is silently overwritten.

6. **Rate Limit Awareness**: GitHub rate limits are captured and exposed in `/health` so clients can monitor available quota.

---

## Schema Design

### Core Tables

**`raw_source_data`** — Immutable provider payloads
- Stores every API response received from each provider
- Re-derivable without re-fetching (audit trail)
- Indexed by `(source, source_handle)` for quick lookups

**`persons`** — Canonical merged profile
- One row per unique person (or ambiguous grouping)
- Tracks `resolution_status` (PENDING, RESOLVED, AMBIGUOUS, FAILED)
- Tracks `enrichment_status` (PENDING, LLM_RUNNING, READY, FAILED)
- Stores conflicts detected when sources disagree on a field
- Records provider statuses, retry count, last error, and resolution timing

**`source_links`** — Mapping of raw sources to canonical persons
- Many-to-one: multiple raw sources → one person
- Includes confidence score (0.0–1.0) for each match
- `confidence_notes` JSONB field stores which signals fired and their weights
- Unique constraint `(source, source_handle)` prevents one external account from linking to two canonical persons

**`person_attributes`** — Key/value attributes (source-specific)
- Survives adding a 5th provider with zero schema migrations (new attributes = new rows)
- Stores GitHub languages, Stack Overflow reputation, dev.to article count, etc.
- Indexed by `(person_id, source)` for efficient retrieval

**`api_call_log`** & **`llm_usage_log`** — Observability
- Track provider latency, errors, and success rates
- Record LLM token usage and estimated costs

### Why This Design?

- **Key/value attributes**: Adding provider #5 requires no ALTER TABLE statements.
- **Unique index on `source_links(source, source_handle)`**: Enforces database-level uniqueness — one GitHub account cannot map to two canonical persons.
- **Conflict storage**: Clients see exactly which sources disagreed and on what fields.
- **Immutable raw data**: Supports replaying resolution logic without re-fetching.

---

## Entity Resolution Strategy

### Confidence Scoring

Each provider account is scored 0.0–1.0 based on matching signals:

| Signal | Weight | When It Fires |
|--------|--------|---------------|
| **Handle hint provided** | 0.55 | Caller supplied e.g. `{"github": "torvalds"}` and we found that user |
| **Exact name match** | 0.25 | Provider name = request name (case-insensitive) |
| **Fuzzy name match** | 0.12 | Name token overlap ≥ 0.8 (e.g. "Jane A. Doe" ≈ "Jane Doe") |
| **Location match** | 0.10 | Provider location matches request location |
| **Email match** | 0.30 | Provider email contains or matches email_hint |
| **Link-back** | 0.20 | Profile explicitly links to another platform (e.g. dev.to → GitHub) |

**Scoring logic:** Signals are cumulative and clamped to 1.0. Example: handle hint (0.55) + name exact (0.25) = 0.80 confidence.

### Resolution Outcomes

- **RESOLVED**: ≥2 sources with confidence ≥ 0.6
  - Example: GitHub 0.65 + Stack Overflow 0.60 = confident merge
  
- **AMBIGUOUS**: Some evidence (≥ 0.4 confidence) but not enough cross-source agreement
  - Example: GitHub 0.50 only, or low confidence across all sources
  - Client may re-query with additional hints (email, more handles)
  
- **FAILED**: No sources found or all provider fetches failed after `MAX_RETRIES=3`
  - Check `last_error` field for details
  
- **PENDING**: Resolution in flight or hasn't started yet

### Idempotency

- Calling `/profiles/resolve` with the same handles returns the existing profile
- If resolution is already RESOLVED and not stale, no re-run occurs
- If AMBIGUOUS and new evidence arrives (e.g., a new handle), re-run starts in background

### Why These Weights?

- **Handle hint (0.55)** is highest: the user explicitly said "this is their account"
- **Email (0.30)** is high but not as high as handle: emails can be shared or outdated
- **Name (0.25)** is moderate: names collide frequently
- **Fuzzy name (0.12)** is weak: reduces false positives
- **Location (0.10)** is very weak: often outdated or vague
- **Link-back (0.20)** is intentionally moderate: could be a fan linking to an idol

---

## Setup Instructions

### Prerequisites

- **Python 3.12+**
- **Supabase account** (free tier works)
- **GitHub token** (for OAuth access to public user data)
- **OpenRouter API key** (optional; fallback summary if missing)

### 1. Clone and Create Virtual Environment

```bash
cd Dev-Profile-Unifier
python -m venv venv

# Activate (Windows)
venv\Scripts\activate

# Activate (macOS/Linux)
source venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Set Up Supabase

1. Create a free Supabase project at [supabase.com](https://supabase.com)
2. In the SQL editor, run the schema from `schema/init.sql` to create tables
3. Copy your project URL and service key (from Settings → API)

### 4. Create `.env` File

Copy `.env.example` and populate with your credentials:

```bash
cp .env.example .env
```

Then edit `.env`:

```bash
# Required
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your-service-role-key
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Optional
OPENROUTER_API_KEY=AIza...
STACK_EXCHANGE_KEY=your-stack-exchange-key
PROFILE_TTL_HOURS=24
APP_ENV=development
LOG_LEVEL=INFO
```

**Getting tokens:**
- **GitHub token**: Create at https://github.com/settings/tokens (needs `public_repo` scope for public data)
- **OpenRouter API key**: Get from https://makersuite.google.com/app/apikey
- **Stack Exchange key**: Create at https://stackapps.com/apps/oauth/register

### 5. Run Locally

**With auto-reload (for development):**

```bash
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 --reload-dir app
```

**Without reload (for stability):**

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The server starts at `http://0.0.0.0:8000`. View docs at `http://localhost:8000/docs`.

---

**6. Rate Limit Awareness**
GitHub rate limits are captured from response headers and exposed in `/health`. Providers with circuit breakers (dev.to) fail fast after repeated failures and auto-reset after a cooldown period.

### Start a Profile Resolution

```bash
curl -X POST http://localhost:8000/profiles/resolve \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Linus Torvalds",
    "github": "torvalds",
    "stackoverflow": "12345",
    "devto": "linustorvalds",
    "hackernews": "torvalds",
    "email_hint": "torvalds@linux-foundation.org"
  }'
```

**Response (202 Accepted):**
```json
{
  "profile_id": "550e8400-e29b-41d4-a716-446655440000",
  "resolution_status": "PENDING",
  "enrichment_status": "PENDING",
  "message": "Resolution started. Poll GET /profiles/{profile_id} for results."
}
```

### Poll for Results

```bash
curl http://localhost:8000/profiles/550e8400-e29b-41d4-a716-446655440000
```

**Response (when resolved):**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "display_name": "Linus Torvalds",
  "location": "Portland, OR",
  "bio": "Linux kernel creator",
  "avatar_url": "https://avatars.githubusercontent.com/...",
  "llm_summary": "Linus Torvalds is a legendary Finnish-American software engineer best known for creating the Linux kernel...",
  "resolution_status": "RESOLVED",
  "enrichment_status": "READY",
  "completeness_score": 0.92,
  "provider_statuses": {
    "github": "SUCCESS",
    "stackexchange": "SUCCESS",
    "devto": "SUCCESS",
    "hackernews": "SUCCESS"
  },
  "sources": [
    {
      "source": "github",
      "handle": "torvalds",
      "confidence": 0.85,
      "matched_on": ["hint_provided", "name_exact"],
      "explanation": ["+0.55 Handle hint provided", "+0.25 Exact name match"]
    }
  ],
  "attributes": {
    "github": {
      "languages": {"C": 45, "Python": 12},
      "public_repos": 250,
      "followers": 200000
    },
    "stackexchange": {
      "reputation": 5000,
      "top_tags": ["linux", "kernel", "c"]
    }
  },
  "conflicts": [],
  "last_resolved_at": "2025-06-27T12:34:56.789Z",
  "created_at": "2025-06-27T12:30:00.000Z",
  "updated_at": "2025-06-27T12:34:56.789Z"
}
```

### Health Endpoint

**`person_attributes`** — Source-attributed key/value attributes
- Stores GitHub languages, Stack Overflow reputation, dev.to article counts, HN karma, etc.
- Unique index on `(person_id, source, attr_key)` — upsertable, no duplicates
- **Zero schema migrations needed to add a fifth data source** — new attributes are new rows

Returns uptime, GitHub rate limit, API call statistics, LLM token usage, and profile statistics.

---

## Environment Variables

Copy and customize `.env.example`:

```bash
# ========== REQUIRED ==========

# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

# GitHub
GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwxyz

# ========== OPTIONAL ==========

# LLM Enrichment (if missing, falls back to deterministic summary)
OPENROUTER_API_KEY=AIzaSyDxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Stack Exchange / Stack Overflow
STACK_EXCHANGE_KEY=your-app-key

# Caching & Refresh
PROFILE_TTL_HOURS=24       # How long before a RESOLVED profile becomes "stale"

# Logging
APP_ENV=development        # or 'production'
LOG_LEVEL=INFO            # DEBUG, INFO, WARNING, ERROR
```

---

## Testing

Run the test suite:

-- Observability
api_call_log: source, endpoint, status_code, latency_ms
llm_usage_log: person_id, prompt_tokens, output_tokens, model
```

Run specific tests:

```bash
pytest tests/test_api.py -v
pytest tests/test_resolver.py -v
pytest tests/test_ingestion.py -v
```

---

## Project Structure

```
Dev-Profile-Unifier/
├── app/
│   ├── main.py                    # FastAPI app, middleware, lifespan
│   ├── routers/
│   │   ├── profiles.py            # POST /resolve, GET /{id}
│   │   ├── health.py              # GET /health observability
│   │   └── schemas.py             # Pydantic models
│   ├── services/
│   │   ├── database.py            # Supabase client & queries
│   │   ├── resolver.py            # Entity resolution logic
│   │   ├── enricher.py            # LLM summary generation
│   │   └── observer.py            # In-memory metrics
│   └── core/
│       └── limiter.py             # Rate limiter (slowapi)
├── ingestion/
│   ├── base.py                    # BaseFetcher async HTTP client
│   ├── github.py                  # GitHub provider
│   ├── stackexchange.py           # Stack Overflow provider
│   ├── devto.py                   # dev.to provider
│   ├── hackernews.py              # Hacker News provider
│   ├── pipeline.py                # Concurrent fetching orchestration
│   └── providers.py               # Provider registry & metadata
├── schema/
│   └── init.sql                   # PostgreSQL/Supabase schema
├── tests/
│   ├── test_api.py                # API endpoint tests
│   ├── test_ingestion.py          # Provider fetcher tests
│   └── test_resolver.py           # Resolution logic tests
├── requirements.txt               # Python dependencies
├── .env.example                   # Environment template
├── README.md                      # This file
├── about.md                       # Project details
└── next_week.md                   # Future improvements
```

---

## What Would Be Different With More Time

See [next_week.md](next_week.md) for detailed prioritization, but in brief:

### 1. **Redis Caching**
- Current: In-process memory only (lost on restart)
- Improvement: Cache resolved profiles and provider responses in Redis
- Benefit: Share cache across multiple instances, survive restarts

### 2. **Circuit Breakers**
- Current: Each provider failure uses a retry
- Improvement: Wrap each provider with circuit breaker (e.g. `aiobreaker`)
- Benefit: Stop hammering a flaky provider; fast-fail after repeated 503s

### 3. **Webhooks / Push Model**
- Current: Clients poll `GET /profiles/{id}`
- Improvement: Webhook callback or SSE stream on resolution completion
- Benefit: Eliminate polling latency, reduce database load

### 4. **Profile Merge Workflow**
- Current: No way to merge two canonical persons if discovered duplicate
- Improvement: Admin endpoint to merge person A into person B
- Benefit: Deduplication after the fact, re-link source_links

### 5. **Full Audit Trail**
- Current: Limited error tracking
- Improvement: Append-only audit log of every signal, conflict, provider failure
- Benefit: Debug AMBIGUOUS outcomes, demonstrate intelligence to stakeholders

### 6. **Rate-Limit Budgeting**
- Current: GitHub 5000 req/hr shared globally
- Improvement: Token bucket or per-user quota
- Benefit: Prevent one user from starving others during batch operations

### 7. **Parallel LLM Calls**
- Current: Sequential enrichment
- Improvement: Batch multiple profiles for concurrent LLM processing
- Benefit: Lower latency for bulk operations

---

## Notes

- The system is designed to be **idempotent**: repeated calls with the same handles return the existing profile rather than creating duplicates.
- **Stale-while-revalidate**: If a profile is cached but older than `PROFILE_TTL_HOURS`, it refreshes in the background while returning the cached result immediately.
- **Conflict visibility**: When merging canonical fields from multiple sources, disagreements are stored and returned in the API response, giving clients full transparency.
- **Background enrichment**: LLM summary generation runs asynchronously, so the resolution endpoint returns immediately even if enrichment is still pending.

---

## Contributing

To extend this project:
1. Add a new provider: Create `ingestion/newprovider.py` and register in `ingestion/providers.py`
2. Modify scoring: Edit weights and signals in `app/services/resolver.py`
3. Add attributes: New provider attributes are stored automatically via `person_attributes` key/value design
4. Run tests: `pytest` to ensure no regressions

---

## License

MIT (or specify your preferred license)

## Entity Resolution Strategy

### Confidence Scoring

Each provider account is scored 0.0–1.0 based on matching signals:

| Signal | Weight | When It Fires |
|--------|--------|---------------|
| Handle hint provided | 0.35 | Caller supplied `{"github": "torvalds"}` and we found that handle |
| Exact name match | 0.25 | Provider display name = request name (case-insensitive) |
| Email match | 0.15 | Provider email matches `email_hint` |
| Link-back | 0.05 | Profile explicitly links to another platform (e.g. dev.to → GitHub URL) |
| Handle only (not hinted) | 0.20 | Handle found but user did not explicitly provide it |

Signals are **cumulative and clamped to 1.0**.

Example: `hint_provided (0.35) + name_exact (0.25) = 0.60 confidence`

### Resolution Outcomes

| Status | Condition | Meaning |
|--------|-----------|---------|
| `RESOLVED` | ≥2 sources ≥ 0.50, OR any single source ≥ 0.60 | Confident enough to show as unified person |
| `AMBIGUOUS` | ≥1 source ≥ 0.35 | Some evidence, not conclusive — re-query with more hints |
| `FAILED` | No valid sources returned | All providers failed or name returned no matches |
| `PENDING` | Resolution in flight | Poll again |

### Why These Weights?

- **Handle hint (0.35)** is highest: the caller explicitly said "this is their account" — strongest possible signal
- **Name exact (0.25)** is second: names collide frequently but combined with a hint it's convincing
- **Email (0.15)**: reliable when exposed by the API, but not all platforms expose it
- **Link-back (0.05)**: a dev.to profile might link to GitHub, but could also be a fan linking to an idol — intentionally weak
- **No location or fuzzy name**: location is too often stale or wrong; fuzzy name causes too many false positives without confirmation

### Handling Ambiguity

When a profile is `AMBIGUOUS`, the response includes all partial matches with their confidence scores and the signals that fired. The caller can re-query with additional hints (`email_hint`, more handles) to trigger a re-resolution.

### Edge Cases

- **Common names** (e.g., "John Smith") without hints → correctly returns `AMBIGUOUS`
- **Handle mismatch** (GitHub name is "tj", not "TJ Holowaychuk") → `hint_provided` fires but `name_exact` does not → lower confidence → `AMBIGUOUS`
- **Platform disagreement** (GitHub: "Portland, OR" vs Stack Overflow: "London, UK") → both stored in `conflicts`, highest-confidence source wins for canonical field
- **Provider failure** (dev.to down) → resolution continues with available sources, `provider_statuses` records the failure

---

## LLM Enrichment

### Why OpenRouter Instead of Gemini

The assessment recommended Gemini free tier. During development, two blockers were encountered:

1. **Wrong key format**: Google AI Studio generates OAuth-style tokens (`AQ.Ab...`) rather than standard API keys (`AIzaSy...`) depending on project configuration
2. **Zero free quota**: The project's free tier showed `limit: 0` for `generate_content_free_tier_requests`, returning `429 RESOURCE_EXHAUSTED` immediately — resolvable only by adding billing

Per the assessment constraint — *"Do not pay for anything. If you find yourself reaching for a credit card, stop and ask us"* — billing was not added.

**OpenRouter** was chosen as the replacement because:
- Genuinely free tier, no credit card required
- OpenAI-compatible API (drop-in replacement, minimal code change)
- `openrouter/free` model slug auto-selects from available free models — no hardcoded model names that go stale
- Transparent token usage and cost tracking in response metadata
- Used model: auto-selected via `openrouter/free` (typically Llama or Mistral variants)

The switch is documented in `enricher.py` and the fallback summary generator ensures the system works even when the LLM is unavailable.

### Summary Generation

The LLM is prompted to write exactly one paragraph (2–3 sentences, under 200 words) based on aggregated platform data. It is explicitly instructed not to invent information not present in the data.

Token usage and estimated cost are tracked per-profile in `llm_usage_log` and surfaced in `/health`.

---

## Observability

The `/health` endpoint returns:

```json
{
  "status": "ok",
  "uptime_seconds": 349,
  "github_rate_limit": {
    "remaining": 4968,
    "limit": 5000,
    "reset_utc": "2026-06-27T11:26:24+00:00"
  },
  "api_calls_by_source": { "github": 8, "stackexchange": 4, "devto": 3 },
  "api_latency_avg_ms": { "github": 690, "stackexchange": 671, "devto": 733 },
  "api_failures_by_source": { "devto": 2 },
  "llm": {
    "total_tokens": 715,
    "prompt_tokens": 315,
    "output_tokens": 400,
    "calls": 2,
    "est_cost_usd": 0.000144
  },
  "profiles": {
    "total": 9,
    "resolved": 6,
    "ambiguous": 3,
    "pending": 0,
    "failed": 0
  },
  "enrichment": { "ready": 8, "pending": 0, "failed": 1 },
  "resolution": { "avg_time_ms": 14272 }
}
```

---

## Setup Instructions

### Prerequisites

- Python 3.12+
- Supabase account (free tier)
- GitHub Personal Access Token
- OpenRouter API key (free, no card — sign up at [openrouter.ai](https://openrouter.ai))

### 1. Clone and Create Virtual Environment

```bash
git clone https://github.com/your-username/Dev-Profile-Unifier.git
cd Dev-Profile-Unifier

python -m venv venv

# Windows
venv\Scripts\activate

# macOS/Linux
source venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Set Up Supabase

1. Create a free project at [supabase.com](https://supabase.com)
2. In the SQL Editor, run `schema/init.sql` to create all tables
3. Copy your project URL and service role key from **Settings → API**

### 4. Create `.env` File

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Required
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Optional — LLM enrichment (fallback summary used if missing)
OPENROUTER_API_KEY=sk-or-v1-...

# Optional — Stack Exchange (higher rate limits with key)
STACK_EXCHANGE_KEY=your-stack-exchange-key

# Optional — tuning
PROFILE_TTL_HOURS=24
APP_ENV=development
LOG_LEVEL=INFO
```

**Getting credentials:**
- **GitHub token**: [github.com/settings/tokens](https://github.com/settings/tokens) — `public_repo` read scope
- **OpenRouter key**: [openrouter.ai/keys](https://openrouter.ai/keys) — free, no card
- **Stack Exchange key**: [stackapps.com/apps/oauth/register](https://stackapps.com/apps/oauth/register)

### 5. Run Locally

```bash
# Development (auto-reload)
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Production
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

API docs: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## API Usage

### Resolve a Profile

```bash
curl -X POST http://localhost:8000/profiles/resolve \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Sindre Sorhus",
    "github": "sindresorhus",
    "devto": "sindresorhus",
    "hackernews": "sindresorhus"
  }'
```

**Response:**
```json
{
  "profile_id": "e607d48a-2421-4a6c-99bf-2277cc6778db",
  "resolution_status": "PENDING",
  "enrichment_status": "PENDING",
  "message": "Resolution started. Poll GET /profiles/{profile_id} for results."
}
```

### Get Resolved Profile

```bash
curl http://localhost:8000/profiles/e607d48a-2421-4a6c-99bf-2277cc6778db
```

**Response (when resolved):**
```json
{
  "id": "e607d48a-...",
  "display_name": "Sindre Sorhus",
  "bio": "Full-Time Open-Sourcerer. Focused on Swift & JavaScript.",
  "llm_summary": "Sindre Sorhus is a full-time open-sourcerer with 1,100+ public repositories...",
  "resolution_status": "RESOLVED",
  "enrichment_status": "READY",
  "completeness_score": 1.0,
  "sources": [
    {
      "source": "github",
      "handle": "sindresorhus",
      "confidence": 0.6,
      "matched_on": ["hint_provided", "name_exact"]
    }
  ],
  "conflicts": []
}
```

### Health Check

```bash
curl http://localhost:8000/health
```

---

## Testing

```bash
pytest

# Verbose
pytest tests/ -v

# Specific file
pytest tests/test_resolver.py -v
```

---

## Project Structure

```
Dev-Profile-Unifier/
├── app/
│   ├── main.py                    # FastAPI app, middleware, lifespan
│   ├── routers/
│   │   ├── profiles.py            # POST /resolve, GET /{id}
│   │   ├── health.py              # GET /health observability
│   │   └── schemas.py             # Pydantic models
│   ├── services/
│   │   ├── database.py            # Supabase client & queries
│   │   ├── resolver.py            # Entity resolution + confidence scoring
│   │   ├── enricher.py            # LLM summary via OpenRouter
│   │   └── observer.py            # In-memory metrics
│   └── core/
│       └── limiter.py             # Rate limiter (slowapi)
├── ingestion/
│   ├── base.py                    # BaseFetcher async HTTP client
│   ├── github.py                  # GitHub provider
│   ├── stackexchange.py           # Stack Overflow provider
│   ├── devto.py                   # dev.to provider (with circuit breaker)
│   ├── hackernews.py              # Hacker News provider
│   ├── pipeline.py                # Concurrent fetch orchestration
│   └── providers.py               # Provider registry and staleness logic
├── schema/
│   └── init.sql                   # Full PostgreSQL schema
├── tests/
│   ├── test_api.py
│   ├── test_ingestion.py
│   └── test_resolver.py
├── requirements.txt
├── .env.example
├── README.md
└── next_week.md                   # Prioritized future improvements
```

---

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `SUPABASE_URL` | ✅ | Your Supabase project URL |
| `SUPABASE_SERVICE_KEY` | ✅ | Supabase service role key (from Settings → API) |
| `GITHUB_TOKEN` | ✅ | GitHub Personal Access Token (public_repo scope) |
| `OPENROUTER_API_KEY` | Optional | OpenRouter key for LLM summaries (fallback used if missing) |
| `STACK_EXCHANGE_KEY` | Optional | Stack Exchange app key (higher rate limits) |
| `PROFILE_TTL_HOURS` | Optional | Hours before a RESOLVED profile is considered stale (default: 24) |
| `APP_ENV` | Optional | `development` or `production` |
| `LOG_LEVEL` | Optional | `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: INFO) |

---

## What I Would Do Differently With More Time

See [`next_week.md`](next_week.md) for full prioritization. In brief:

1. **Redis caching** — in-process metrics are lost on restart; Redis would persist across deploys and share state across multiple instances
2. **Webhook / SSE** — replace polling with push notifications on resolution completion
3. **Profile merge endpoint** — if two canonical persons are discovered to be the same, merge their `source_links` and `person_attributes`
4. **Audit log** — append-only log of every signal, conflict, and provider failure for debugging AMBIGUOUS outcomes
5. **Rate-limit budgeting** — per-user token buckets to prevent one caller from exhausting the GitHub quota
6. **Fuzzy name matching with confirmation** — disabled now to avoid false positives, but could be re-enabled with a confidence floor and explicit user confirmation step

---

## Use of AI Tools

Claude (claude.ai) was used throughout this project for:
- Debugging API integration issues (Gemini quota error, dev.to endpoint correction)
https://github.com/ishreya-dev/Dev-Profile-Unifier/pull/1/conflict?name=README.md&ancestor_oid=b9f0be992a65bd82f19a381771dbcf1f1927f5b4&base_oid=88d7078c56c9786c32607a1e3a2c7757c7489714&head_oid=aaa74a3cc5955b0a49289dac0aefb170ce9e9135- Code review and bug fixing (orphaned module-level variable, undefined `handle` parameter)
- README drafting and accuracy checking

All architectural decisions, scoring weights, schema design, and code structure were my own. Claude was used as a pair programmer, not a code generator — every suggestion was reviewed, tested, and adapted.

---

## License

MIT
Planned improvements are tracked in `next_week.md` and include caching, rate-limit management, webhook notifications, and audit logging.

## Author

```bash
Shreya 
shreya24singhs@gmail.com
ishreya-dev
```
