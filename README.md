# Dev Profile Unifier

Pull, merge, and summarize public developer identities from GitHub, Stack Overflow, dev.to, and Hacker News — into a single canonical profile, stored in Supabase, exposed via FastAPI.

---

## Overview

Given a person's name and optional platform handles, this service:

1. Concurrently fetches public profile data from four developer platforms
2. Scores each account's likelihood of belonging to the searched person
3. Merges matching accounts into a canonical profile with conflict tracking
4. Generates an LLM summary of the person's skills and focus areas
5. Returns everything via a REST API with full observability

---

## Architecture

### Data Flow

```
POST /profiles/resolve
    ↓
Check existing profile (idempotent lookup via source_links)
    ↓
If new or stale:
  ├─→ Concurrent async fetches (GitHub, Stack Overflow, dev.to, Hacker News)
  ├─→ Store raw payloads → raw_source_data
  ├─→ Score each source (handle hint, name match, email match, link-back)
  ├─→ Merge canonical fields + detect conflicts
  ├─→ Persist to persons + source_links + person_attributes
  └─→ Generate LLM summary in background → update persons.llm_summary
    ↓
Return profile_id immediately (client polls GET /profiles/{id})
```

### Key Design Decisions

**1. Background Tasks**
Resolution runs asynchronously via FastAPI `BackgroundTasks`. The API returns a `profile_id` immediately so the client is never blocked. Clients poll `GET /profiles/{id}` for updates.

**2. Idempotent Lookups**
If the same handles are searched again, the existing profile is returned. No duplicate profiles. If the profile is stale (older than `PROFILE_TTL_HOURS`) or enrichment is incomplete, a refresh runs in the background while still returning the cached result immediately (stale-while-revalidate).

**3. Immutable Raw Data**
Raw API responses are stored in `raw_source_data` without modification. If resolution logic changes, profiles can be re-derived without re-fetching from providers.

**4. Separate Resolution and Enrichment State Machines**
A profile can be `RESOLVED` while enrichment is still `PENDING` or `LLM_RUNNING`. These are independent — a provider failure does not block LLM enrichment, and an LLM failure does not fail the resolution.

**5. Conflict Visibility**
When two sources disagree on a field (e.g., GitHub says "Portland, OR", Stack Overflow says "London, UK"), both values are stored in `persons.conflicts` and returned to the API caller. Nothing is silently overwritten.

**6. Rate Limit Awareness**
GitHub rate limits are captured from response headers and exposed in `/health`. Providers with circuit breakers (dev.to) fail fast after repeated failures and auto-reset after a cooldown period.

---

## Schema Design

### Tables

**`raw_source_data`** — Immutable provider payloads
- One row per fetch per source per handle
- Full raw API response stored in `payload` JSONB — nothing is discarded
- `payload_version` allows re-processing without re-fetching if logic changes
- Indexed by `(source, source_handle)` for fast lookups

**`persons`** — Canonical unified profile
- One row per unique person (or ambiguous grouping)
- Tracks `resolution_status`: `PENDING | RESOLVED | AMBIGUOUS | FAILED`
- Tracks `enrichment_status`: `PENDING | LLM_RUNNING | READY | FAILED`
- `conflicts` JSONB stores field disagreements across sources (e.g., location mismatch)
- `provider_statuses` tracks which platforms succeeded or failed per resolution run
- `completeness_score` measures how many canonical fields are filled
- Full audit trail: `last_resolved_at`, `last_attempted_at`, `last_error`, `retry_count`

**`source_links`** — Mapping of raw sources → canonical persons
- Many-to-one: multiple raw source records → one canonical person
- `confidence` (0.0–1.0) — how certain we are this account belongs to this person
- `confidence_notes` JSONB — which signals fired and their individual weights
- `matched_on` JSONB — list of signal names that contributed (e.g., `["hint_provided", "name_exact"]`)
- Unique constraint on `(source, source_handle)` — one GitHub account cannot link to two canonical persons

**`person_attributes`** — Source-attributed key/value attributes
- Stores GitHub languages, Stack Overflow reputation, dev.to article counts, HN karma, etc.
- Unique index on `(person_id, source, attr_key)` — upsertable, no duplicates
- **Zero schema migrations needed to add a fifth data source** — new attributes are new rows

**`api_call_log`** — Provider observability
- Per-call tracking: source, endpoint, status code, latency

**`llm_usage_log`** — LLM cost tracking
- Per-call token usage, model used, linked to the person being enriched

### Why This Schema Survives a Fifth Data Source

Adding LinkedIn (or any new provider) next month requires:

1. A new fetcher at `ingestion/linkedin.py`
2. Register it in `ingestion/providers.py`
3. **Zero database migrations** — new attributes land in `person_attributes` as new rows
4. New confidence signals go in `resolver.py` weight constants only
5. New raw payloads land in `raw_source_data` with `source = 'linkedin'`

No `ALTER TABLE`. No new columns. The key/value design of `person_attributes` and the JSONB flexibility of `confidence_notes` absorb any new provider cleanly.

### Schema (abbreviated)

```sql
-- Raw provider payloads (immutable)
raw_source_data: id, source, source_handle, fetched_at, payload JSONB, payload_version

-- Canonical unified person
persons: id, display_name, location, bio, avatar_url, llm_summary,
         resolution_status, enrichment_status, conflicts JSONB,
         provider_statuses JSONB, completeness_score, ...audit fields

-- Raw ↔ Canonical linkage with confidence
source_links: id, person_id, raw_source_id, source, source_handle,
              confidence, confidence_notes JSONB, matched_on JSONB

-- Source-attributed attributes (zero-migration extensible)
person_attributes: id, person_id, source, attr_key, attr_value JSONB

-- Observability
api_call_log: source, endpoint, status_code, latency_ms
llm_usage_log: person_id, prompt_tokens, output_tokens, model
```

Full schema: [`schema/init.sql`](schema/init.sql)

---

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
- Code review and bug fixing (orphaned module-level variable, undefined `handle` parameter)
- README drafting and accuracy checking

All architectural decisions, scoring weights, schema design, and code structure were my own. Claude was used as a pair programmer, not a code generator — every suggestion was reviewed, tested, and adapted.

---

## License

MIT