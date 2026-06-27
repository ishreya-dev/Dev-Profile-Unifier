# Dev Profile Unifier

Pull, merge, and summarize public developer identities from GitHub, Stack Overflow, dev.to, and Hacker News.

## Overview

This service accepts a developer name plus optional account handles, fetches public profile data from multiple sources, resolves whether those accounts belong to the same individual, and then enriches the merged profile with an LLM-generated summary.

Key capabilities:
- Concurrent provider ingestion for GitHub, Stack Overflow, dev.to, and Hacker News
- Entity resolution using handle hints, name matching, email hinting, and link-back signals
- Canonical profile creation in Supabase with conflict tracking
- Background enrichment via LLM summary generation
- Health and observability metrics through `/health`

## Requirements

- Python 3.12+ (project dependencies are pinned in `requirements.txt`)
- Supabase project with `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`
- GitHub token with read access to public user data
- Optional LLM API key for enrichment

## Setup

1. Create a `.env` file at the repository root.
2. Populate the environment variables:

```bash
SUPABASE_URL=
SUPABASE_SERVICE_KEY=
GITHUB_TOKEN=
GEMINI_API_KEY=
PROFILE_TTL_HOURS=24
```

3. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Running Locally

Start the API with Uvicorn:

```bash
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The application exposes:
- `POST /profiles/resolve` to launch resolution
- `GET /profiles/{profile_id}` to retrieve merged profile status and data
- `GET /health` for operational metrics

## API Usage

### Start a profile resolution

```bash
curl -X POST http://localhost:8000/profiles/resolve \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Linus Torvalds",
    "github": "torvalds",
    "stackoverflow": "12345",
    "devto": "linustorvalds",
    "hackernews": "torvalds",
    "email_hint": "linus@example.com"
  }'
```

The request returns a `profile_id` with `resolution_status` and `enrichment_status`. Resolution runs asynchronously in the background.

### Poll for results

```bash
curl http://localhost:8000/profiles/{profile_id}
```

### Health endpoint

```bash
curl http://localhost:8000/health
```

## Data Model

The service stores canonical profile state in Supabase using these core concepts:
- `persons`: merged profile metadata, resolution/enrichment status, conflicts, and computed completeness
- `source_links`: mapping of raw provider fetches to a canonical person with confidence and signal notes
- `person_attributes`: normalized key/value attributes from each provider
- `raw_source_data`: immutable raw provider payloads for auditability and reprovisioning

The resolution pipeline is designed to be idempotent: the same handle lookup returns an existing profile when it already exists.

## Resolution Logic

Profiles are scored per source based on evidence such as:
- direct handle hints from the request
- exact or fuzzy name matches
- email hint matches
- cross-platform link-back signals

A profile is considered `RESOLVED` once at least two sources have sufficient confidence.

If the request matches an existing ambiguous or resolved profile and new evidence is provided, the service re-runs resolution in the background.

## Notes

- `resolve_profile()` enqueues background resolution using FastAPI `BackgroundTasks`.
- Enrichment is tracked separately from resolution; a profile may be `RESOLVED` while enrichment is still `PENDING` or `FAILED`.
- `PROFILE_TTL_HOURS` controls staleness-based refresh behavior.

## Development and Testing

Run the test suite with:

```bash
pytest
```

## Project Structure

- `app/`: FastAPI application, routers, and services
- `ingestion/`: provider fetchers and pipeline orchestration
- `schema/init.sql`: database schema definition
- `tests/`: unit and integration tests

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
