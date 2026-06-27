# Dev Profile Unifier

Pull, merge, and summarize public developer identities from GitHub, Stack Overflow, dev.to, and Hacker News.

## Overview

This is a FastAPI service that aggregates public profile data from four developer platforms, performs entity resolution to determine if multiple accounts belong to the same person, and enriches the canonical profile with an LLM-generated summary.

**Key capabilities:**
- Concurrent async fetching from GitHub, Stack Overflow, dev.to, and Hacker News
- Intelligent entity resolution using handle hints, name matching, email hinting, and cross-platform link-back signals
- Canonical profile creation with conflict tracking for ambiguous fields
- Background enrichment via Gemini LLM summary generation
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
  └─→ Generate Gemini summary (enrichment in background)
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
- **Gemini API key** (optional; fallback summary if missing)

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
- **Gemini API key**: Get from https://makersuite.google.com/app/apikey
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

## API Usage

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

```bash
curl http://localhost:8000/health
```

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

```bash
pytest
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

Each provider account is scored 0.0–1.0 based on how confident we are it belongs to the searched person:

| Signal | Weight | What it means |
|--------|--------|---------------|
| User provided exact handle | 0.35 | Caller said `{"name": "Jane", "github": "jdoe"}` |
| Display name matches exactly | 0.25 | GitHub profile name = "Jane Doe" and request is "Jane Doe" |
| Email matches email_hint | 0.15 | GitHub commit email = "jane@acme.com" and email_hint = "jane@acme.com" |
| Account links to other source | 0.05 | dev.to profile lists "GitHub: jdoe" (weak — could be admiration link) |
| Handle in request (not explicit) | 0.20 | We found "jdoe" but user didn't provide the github field |

**Scoring logic**: Signals are cumulative. A GitHub account that matches on hint (0.35) + name (0.25) = 0.60 confidence.

### Resolution Status

A profile reaches each status based on source confidence:

- **RESOLVED**: ≥2 sources score ≥0.50 confidence each
  - Example: GitHub (0.60) + Stack Overflow (0.55) = RESOLVED
  - We're confident enough to show this as a unified person
  
- **AMBIGUOUS**: Some evidence (≥1 source ≥0.40) but not confident
  - Example: GitHub (0.45) only = AMBIGUOUS
  - Could be the person, could be a common name. User may need to provide more hints.
  
- **FAILED**: No sources found or all failed to fetch
  - Check `last_error` for why

### Handling Ambiguity

When a profile is AMBIGUOUS, the API returns it with `resolution_status: "AMBIGUOUS"` and includes all the partial matches the user can inspect:

```bash
{
  "profile_id": "550e8400...",
  "resolution_status": "AMBIGUOUS",
  "persons": [
    {
      "display_name": "Jane Doe",
      "sources": [
        { "source": "github", "handle": "jdoe", "confidence": 0.45 },
        { "source": "dev.to", "handle": "janedoe", "confidence": 0.38 }
      ]
    }
  ]
}
```

The client should re-query with more hints (e.g., email_hint, additional handles) to disambiguate.

### Why These Weights?

- **Hint is highest (0.35)**: The user explicitly said "this is their GitHub handle" — strongest signal.
- **Name is second (0.25)**: Names often collide (Jane Doe is common), but combined with handle, it's convincing.
- **Email (0.15)**: Email is exposed by some APIs (GitHub commit emails) but not all (Stack Overflow hides it). Moderate weight.
- **Link-back (0.05)**: A dev.to profile *might* link to the real GitHub, but it could also be a fan linking to an idol. Very weak signal.
- **Handle only (0.20)**: If we find a handle match but the user didn't suggest it, it's suggestive but not definitive (many people share handles).

### Why No Location or Fuzzy Name?

- **Location** is too ambiguous and frequently wrong (people list college towns, work cities, or typos).
- **Fuzzy name matching** (e.g., "Tom" ≈ "Thomas") causes too many false positives without explicit confirmation.

If you want these signals later, document them and re-weight everything.

### Edge Cases

1. **Common names** (e.g., "John Smith")
   - Without email_hint or explicit handles, will be AMBIGUOUS
   - This is correct — we shouldn't guess

2. **Platform name differences**
   - GitHub: "Jane Doe Smith"
   - Stack Overflow: "Jane D"
   - Fuzzy matching is disabled to avoid guessing. User should provide email_hint or extra handles.

3. **Stale data**
   - If a profile was RESOLVED but the person's GitHub profile changed dramatically, re-run resolution with `?refresh=true` to re-fetch.

### Testing Your Confidence Scores

Run a test search for a well-known open-source dev. Example:

```bash
curl -X POST http://localhost:8000/profiles/resolve \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Linus Torvalds",
    "github": "torvalds",
    "email_hint": "torvalds@linux-foundation.org"
  }'
```

Expected: All sources score ≥0.50, status = RESOLVED.

```bash
curl -X POST http://localhost:8000/profiles/resolve \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Jane Smith"
  }'
```

Expected: Multiple sources score 0.25–0.45, status = AMBIGUOUS (ask for email or handles).

## Future Work

Planned improvements are tracked in `next_week.md` and include caching, rate-limit management, webhook notifications, and audit logging.
