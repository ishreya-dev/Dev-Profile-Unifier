# Dev Profile Unifier

Pull, merge, and summarize public developer identities from GitHub, Stack Overflow, dev.to, and Hacker News — into a single canonical profile, stored in Supabase, exposed via FastAPI.

Render Link: https://dev-profile-unifier.onrender.com

---

This is a FastAPI service that aggregates public profile data from four developer platforms, performs entity resolution to determine if multiple accounts belong to the same person, and enriches the canonical profile with an LLM-generated summary.

**Key capabilities:**
- Concurrent async fetching from GitHub, Stack Overflow, dev.to, and Hacker News
- Intelligent entity resolution using handle hints, name matching, email hinting, and cross-platform link-back signals
- Canonical profile creation with conflict tracking for ambiguous fields
- Background enrichment via OpenRouter LLM summary generation (with deterministic fallback)
- Downgrade protection: re-resolution never overwrites a RESOLVED profile with a degraded result
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
  └─→ Generate LLM summary via OpenRouter (enrichment in background)
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

6. **Downgrade Protection**: If a re-resolution run fetches fewer successful sources than the existing RESOLVED profile already has (e.g. GitHub fails transiently during a stale refresh), the existing result is kept. The new provider statuses are recorded for observability but resolution fields are not overwritten.

7. **Rate Limit Awareness**: GitHub rate limits are captured and exposed in `/health` so clients can monitor available quota.

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
- Stores GitHub languages, Stack Overflow reputation, dev.to article count, HN karma, etc.
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
| Handle hint provided | 0.35 | Caller supplied e.g. `{"github": "tj"}` and we found that handle |
| Exact name match | 0.25 | Provider display name = request name (case-insensitive) |
| Email match | 0.15 | Provider email matches `email_hint` |
| Link-back | 0.05 | Profile explicitly links to another platform (e.g. dev.to → GitHub URL) |

Signals are **cumulative and clamped to 1.0**.

Example: `hint_provided (0.35) + name_exact (0.25) = 0.60 confidence` → RESOLVED as single source.

### Resolution Outcomes

| Status | Condition | Meaning |
|--------|-----------|---------|
| `RESOLVED` | ≥2 sources each with hint_provided, OR ≥2 sources ≥ 0.50, OR any single source ≥ 0.60 | Confident enough to return as unified person |
| `AMBIGUOUS` | ≥1 source ≥ 0.35 | Some evidence, not conclusive — re-query with more hints |
| `FAILED` | No valid sources returned | All providers failed or name returned no matches |
| `PENDING` | Resolution in flight | Poll again |

### Why These Weights?

- **Handle hint (0.35)** is highest: the caller explicitly said "this is their account" — strongest possible signal
- **Name exact (0.25)** is second: names collide frequently but combined with a hint it's convincing. hint + name = 0.60 → RESOLVED on a single source
- **Email (0.15)**: reliable when exposed by the API, but most profiles don't expose a public email
- **Link-back (0.05)**: intentionally weak — a dev.to profile might link to GitHub as a fan, not as the same person
- **No fuzzy name or location**: both were removed — location is too often stale or wrong; fuzzy name causes too many false positives without a confirmation signal

### Downgrade Protection

When a RESOLVED profile is re-resolved (e.g. due to staleness), the new run's source count is compared to the existing profile's source count. If the new run produced fewer successful fetches (a provider failed transiently), the existing RESOLVED data is kept and only `provider_statuses` is updated. This prevents a transient GitHub failure from downgrading a RESOLVED profile to AMBIGUOUS.

### Handling Ambiguity

When a profile is `AMBIGUOUS`, the response includes all partial matches with their confidence scores and the signals that fired. The caller can re-query with additional hints (`email_hint`, more handles) to trigger a re-resolution.

### Edge Cases

- **Common names** (e.g., "John Smith") without hints → correctly returns `AMBIGUOUS`
- **Handle mismatch** (GitHub display name is "tj", not "TJ Holowaychuk") → `hint_provided` fires but `name_exact` does not → 0.35 confidence. If a second platform is also hint-provided, RESOLVED via the 2-hint rule
- **Platform disagreement** (GitHub: "Portland, OR" vs Stack Overflow: "London, UK") → both stored in `conflicts`, highest-confidence source wins for canonical field
- **Provider failure** (dev.to down) → resolution continues with available sources, `provider_statuses` records the failure

---

## LLM Enrichment

### Why OpenRouter Instead of Gemini

The assessment recommended Gemini free tier. During development, two blockers were encountered:

1. **Zero free quota**: The project's free tier returned `429 RESOURCE_EXHAUSTED` immediately — resolvable only by adding billing

Per the assessment constraint — *"Do not pay for anything"* — billing was not added.

**OpenRouter** was chosen as the replacement because:
- Genuinely free tier, no credit card required
- OpenAI-compatible API (drop-in replacement, minimal code change)
- `openrouter/free` model slug auto-selects from available free models — no hardcoded model names that go stale
- Transparent token usage and cost tracking in response metadata

### Summary Generation

The LLM is prompted to write exactly 2 sentences based on aggregated platform data. Key prompt design decisions:

- `max_tokens: 120` — tight budget prevents the model reasoning at length instead of answering. 120 tokens is sufficient for 2 clean sentences
- System prompt provides a concrete example of the exact output format expected
- User prompt supplies only data and a single instruction: "use only the facts below, do not invent anything"
- The previous instruction "mention their primary languages and notable work" was removed — it caused reasoning loops when data was sparse ("we must mention languages but we have no data...")
- Top repos by star count are included in the prompt so the model has concrete "notable work" to reference

### Reasoning Leak Handling

Some free OpenRouter models output their reasoning process before (or instead of) the actual answer. `enricher.py` handles this with:

- Line-by-line filtering: splits on `\n` (not just `\n\n`) since free models often use single newlines between reasoning steps
- Pattern matching against known reasoning phrases ("let me think", "this creates a conflict", "we must", etc.)
- Backwards walk through paragraphs to find the last clean prose block
- If every paragraph is filtered as reasoning/metadata, falls back to the deterministic summary

### Deterministic Fallback

If OpenRouter is unavailable, returns a rate-limit empty response, or all content is filtered as reasoning leak, a deterministic summary is built from the merged profile data:
- Name, location, bio from canonical fields
- Top languages from GitHub
- Top repos by star count with descriptions
- Stack Overflow tags and reputation
- GitHub followers and HN karma

Token usage and estimated cost are tracked per-profile in `llm_usage_log` and surfaced in `/health`.

---

## Setup Instructions

### Prerequisites

- Python 3.12+
- Supabase account (free tier)
- GitHub Personal Access Token
- OpenRouter API key (free, no card — sign up at [openrouter.ai](https://openrouter.ai))

### 1. Clone and Create Virtual Environment

```bash
git clone https://github.com/ishreya-dev/Dev-Profile-Unifier.git
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
 `.env`:

```env
GITHUB_TOKEN=git_token
STACK_EXCHANGE_KEY=your_stack_exchange_key
SUPABASE_URL=https://xxxxxxxxxxxx.supabase.co
SUPABASE_SERVICE_KEY=api_key
OPENROUTER_API_KEY=your_OPENROUTER_API_KEY
# App
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
    "name": "TJ Holowaychuk",
    "github": "tj",
    "hackernews": "tjholowaychuk"
  }'
```

**Response (202 Accepted):**
```json
{
  "profile_id": "c7a4591e-6fe6-43cd-b60c-7fc04c6720be",
  "resolution_status": "PENDING",
  "enrichment_status": "PENDING",
  "message": "Resolution started. Poll GET /profiles/{profile_id} for results."
}
```

### Poll for Results

```bash
curl http://localhost:8000/profiles/c7a4591e-6fe6-43cd-b60c-7fc04c6720be
```

**Response (when resolved):**
```json
{
  "id": "c7a4591e-6fe6-43cd-b60c-7fc04c6720be",
  "display_name": "TJ Holowaychuk",
  "location": null,
  "bio": null,
  "avatar_url": "https://avatars.githubusercontent.com/u/25254?v=4",
  "llm_summary": "TJ Holowaychuk is a developer working primarily in JavaScript, Go, Shell, and Ruby, known for projects like commander.js (28,291★) and n (19,518★). He has 51,777 GitHub followers and 900 Hacker News karma.",
  "resolution_status": "RESOLVED",
  "enrichment_status": "READY",
  "completeness_score": 0.75,
  "provider_statuses": {
    "github": "SUCCESS",
    "hackernews": "SUCCESS",
    "stackexchange": "NOT_FOUND",
    "devto": "SKIPPED"
  },
  "sources": [
    {
      "source": "github",
      "handle": "tj",
      "confidence": 0.35,
      "matched_on": ["hint_provided"],
      "explanation": ["+0.35 Caller supplied handle 'tj' directly"]
    },
    {
      "source": "hackernews",
      "handle": "tjholowaychuk",
      "confidence": 0.35,
      "matched_on": ["hint_provided"],
      "explanation": ["+0.35 Caller supplied handle 'tjholowaychuk' directly"]
    }
  ],
  "attributes": {
    "github": {
      "languages": {"JavaScript": 13, "Go": 10, "Shell": 2, "Ruby": 1},
      "public_repos": 296,
      "followers": 51777
    },
    "hackernews": {
      "karma": 900,
      "top_domains": {"tjholowaychuk.com": 2, "github.com": 2, "apex.sh": 1}
    }
  },
  "conflicts": [],
  "last_resolved_at": "2026-06-28T14:26:02.203273+00:00",
  "created_at": "2026-06-28T14:25:58.945204+00:00",
  "updated_at": "2026-06-28T14:26:11.674383+00:00"
}
```

### Health Check

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "uptime_seconds": 349,
  "github_rate_limit": {
    "remaining": 4968,
    "limit": 5000,
    "reset_utc": "2026-06-28T14:26:24+00:00"
  },
  "api_calls_by_source": {"github": 8, "stackexchange": 4, "devto": 3},
  "api_latency_avg_ms": {"github": 690, "stackexchange": 671, "devto": 733},
  "api_failures_by_source": {"devto": 2},
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
  "enrichment": {"ready": 8, "pending": 0, "failed": 1},
  "resolution": {"avg_time_ms": 14272}
}
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
├── render.yaml
├── README.md
├── about.md
└── next_week.md
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

## Testing

```bash
pytest

# Verbose
pytest tests/ -v

# Specific file
pytest tests/test_resolver.py -v
```

---

## Known Limitations

- **`bio` and `location` may be null**: Some developers (e.g. GitHub handle `tj`) don't populate these fields on their profiles. This is correct behaviour — the system does not invent or infer missing fields.
- **OpenRouter free tier rate limits**: The free model occasionally returns empty responses under load. The deterministic fallback summary fires automatically in this case.
- **dev.to and Hacker News have no name-search API**: These platforms can only be resolved if a handle is explicitly provided in the request, or if another platform's profile links to them. This is a structural API limitation, not an implementation gap.
- **`completeness_score` reflects platform coverage**: The score counts which COMPLETENESS_FIELDS are present in the canonical profile or were successfully fetched from a platform. It does not guarantee every field is populated (a platform can succeed but return null for bio/location).

---

## What I Would Do Differently With More Time

See [`next_week.md`](next_week.md) for full prioritization. In brief:

1. **Redis caching** — in-process metrics are lost on restart; Redis would persist across deploys and share state across multiple instances
2. **Webhook / SSE** — replace polling with push notifications on resolution completion
3. **Profile merge endpoint** — if two canonical persons are discovered to be the same, merge their `source_links` and `person_attributes`
4. **Audit log** — append-only log of every signal, conflict, and provider failure for debugging AMBIGUOUS outcomes
5. **Rate-limit budgeting** — per-user token buckets to prevent one caller from exhausting the GitHub quota
6. **Fuzzy name matching** — disabled now to avoid false positives, re-enable with a confidence floor

---

## Use of AI Tools

Claude (claude.ai) was used throughout this project for:
- Debugging API integration issues (Gemini quota error, dev.to endpoint correction)
- Code review and bug fixing (reasoning leak in LLM output, completeness score always returning 1.0, re-resolution downgrade bug, resolution status not firing correctly for 2-hint profiles)
- README drafting and accuracy checking

All architectural decisions, scoring weights, schema design, and code structure were my own. Claude was used as a pair programmer, not a code generator — every suggestion was reviewed, tested, and adapted.

---

## License

MIT

## Author

```
Shreya
shreya24singhs@gmail.com
ishreya-dev
```