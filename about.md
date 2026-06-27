# About Dev Profile Unifier

Dev Profile Unifier is a FastAPI service that pulls public developer data from multiple sources, resolves whether those sources belong to the same person, and exposes a merged profile with confidence signals and enrichment.

## Repository structure

### Root files
- `README.md` — project overview, setup instructions, API usage, and conceptual architecture.
- `about.md` — this file: repository purpose, file/folder responsibilities, and specifications.
- `next_week.md` — prioritized roadmap for future improvements.
- `requirements.txt` — pinned Python dependencies used by the project.
- `pytest.ini` — Pytest configuration for running the test suite.
- `.env.example` — example environment variables required to run the app.
- `render.yaml` — deployment configuration for Render or similar platform.
- `schema/init.sql` — Supabase/PostgreSQL database schema for tables, indexes, and triggers.

### `app/`
The application package contains the FastAPI HTTP server, routers, and service logic.

- `app/main.py` — FastAPI application entrypoint. It loads environment variables, initializes the Supabase client on startup, and registers the API routers.
- `app/routers/`
  - `profiles.py` — exposes `/profiles/resolve` and `/profiles/{profile_id}`. It uses background tasks for asynchronous resolution.
  - `health.py` — exposes `/health` and collects observability metrics from database tables and runtime metrics.
  - `schemas.py` — Pydantic request/response models and the normalized `PersonProfile` response shape.
- `app/services/`
  - `database.py` — Supabase client initialization and helper functions for inserting, updating, and querying canonical profiles, raw sources, attributes, source links, and logs.
  - `resolver.py` — core resolution logic. It decides whether to create a new profile, reuse an existing one, or retry ambiguous/failed resolution. It scores provider evidence, merges canonical fields, records conflicts, and orchestrates enrichment.
  - `enricher.py` — summary generation service. It builds a prompt from the merged profile and raw provider data, calls Gemini if available, and falls back to a deterministic summary if the LLM API key is missing or the call fails.
  - `observer.py` — in-memory metrics tracking for API calls, GitHub rate limiting, LLM usage, uptime, and resolution timing.

### `ingestion/`
This package contains provider fetchers and the pipeline orchestration used to fetch source data.

- `base.py` — base async HTTP fetcher with shared request helpers.
- `github.py` — GitHub fetcher. It retrieves user profile, repositories, and recent events, and extracts languages, activity, and repo metadata.
- `stackexchange.py` — Stack Overflow fetcher. It resolves either a user ID or name search, fetches user profile, top tags, and answers.
- `devto.py` — dev.to fetcher. It loads profile details and recent articles, aggregates tags, and builds article metadata.
- `hackernews.py` — Hacker News fetcher. It retrieves user profile data and recent submissions/comments through official and Algolia endpoints.
- `pipeline.py` — runs concurrent fetches for all requested providers, stores raw source payloads, and returns provider responses with error handling.
- `providers.py` — provider registry and configuration metadata. It defines available sources, TTL settings, priority, and staleness/evidence helper logic.

### `schema/`
- `init.sql` — database schema for all persistent tables.
  - `raw_source_data` stores immutable provider payloads.
  - `persons` stores canonical profile state, resolution/enrichment status, conflicts, provider statuses, and audit timestamps.
  - `person_attributes` stores normalized source-specific attributes in key/value form.
  - `source_links` records how each raw source maps to a canonical person, with confidence and notes.
  - `api_call_log` and `llm_usage_log` capture observability data.
  - The schema also includes a trigger to keep `persons.updated_at` current.

### `tests/`
- `test_api.py` — API surface tests for health, resolve, and profile retrieval behavior.
- `test_ingestion.py` — ingestion tests for provider fetchers and HTTP mocking.
- `test_resolver.py` — resolution logic tests including scoring, conflict detection, fallback summary behavior, and background task orchestration.

## Specifications

### API contract
- `POST /profiles/resolve`
  - Request body: `ResolveRequest` with `name` plus optional `github`, `stackoverflow`, `devto`, `hackernews`, and `email_hint`.
  - Response: `ResolveResponse` with `profile_id`, `resolution_status`, `enrichment_status`, and a message.
  - Status code: `202 Accepted`.
- `GET /profiles/{profile_id}`
  - Returns a `PersonProfile` object or `404` if not found.
- `GET /health`
  - Returns runtime and database statistics for uptime, GitHub rate limit, API call counts, failure rates, resolution metrics, and enrichment metrics.

### Profile model
A canonical profile contains:
- identity fields: `display_name`, `location`, `bio`, `avatar_url`
- `llm_summary` from enrichment
- `resolution_status`: `PENDING`, `RESOLVED`, `AMBIGUOUS`, or `FAILED`
- `enrichment_status`: `PENDING`, `LLM_RUNNING`, `READY`, or `FAILED`
- `completeness_score` and `provider_statuses`
- `sources`: list of per-source `SourceContribution` records with confidence and matching signals
- `attributes`: source-specific normalized attributes
- `conflicts`: field-level conflict details when multiple source values disagree
- query metadata and audit timestamps

### Resolution logic
- The resolver uses explicit handle hints and name/email matches.
- Scoring includes direct handle hints, exact/fuzzy name matching, email hint matching, and link-back evidence.
- A profile is marked `RESOLVED` when at least two sources have high confidence (≥ 0.6).
- When only weaker evidence exists, the profile may remain `AMBIGUOUS`.
- If a profile already exists and new evidence arrives, resolution may re-run in the background.
- The system is designed to be idempotent for repeated handle lookups.

### Enrichment
- After resolution, the `enricher` generates a summary paragraph using Gemini if `GEMINI_API_KEY` is present.
- If Gemini is unavailable or fails, a fallback summary is built deterministically from the merged profile and provider metadata.
- Enrichment status is independent from resolution status.

### Environment variables
Required or supported variables:
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `GITHUB_TOKEN`
- `GEMINI_API_KEY`
- `STACK_EXCHANGE_KEY`
- `PROFILE_TTL_HOURS` (optional, defaults to 24)
- `APP_ENV`, `LOG_LEVEL` (optional runtime settings)

### Database approach
- `raw_source_data` keeps immutable raw payloads for audit and reprocessing.
- `person_attributes` is key/value oriented so new provider attributes can be stored without schema migrations.
- `source_links` enforces unique `(source, source_handle)` so one external account cannot map to multiple canonical persons.
- `persons` stores provenance, conflict metadata, resolution/enrichment state, and retry logic.

## Operational notes
- The app is launched via `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000` in development.
- `app/main.py` initializes the Supabase client and metrics at startup.
- `BackgroundTasks` are used to keep `/profiles/resolve` fast while resolution and enrichment continue asynchronously.
- `metrics` are recorded in memory and surfaced by `/health`.

## Deployment and future work
- `render.yaml` contains deployment configuration for Render.
- `next_week.md` lists planned improvements such as Redis caching, circuit breakers, webhooks, merge workflows, audit logs, and rate-limit budgeting.
