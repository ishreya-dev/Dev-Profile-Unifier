# Next Week — Prioritized Improvements

## Bugs Fixed This Session

| Bug | File | Fix |
|-----|------|-----|
| Re-resolution overwrote RESOLVED profile when a provider failed transiently | `resolver.py` | Downgrade guard: if new run has fewer sources than existing RESOLVED profile, keep existing |
| `completeness_score` always returned 1.0 regardless of actual field population | `resolver.py` | Fixed check — was `any(r.source in [...])` which is always True; now checks `f in sources_present` |
| Two hint-provided sources resolved to AMBIGUOUS instead of RESOLVED | `resolver.py` | Added 2-hint rule: if ≥2 sources both have `hint_provided`, return RESOLVED |
| LLM reasoning leaked into `llm_summary` | `enricher.py` | Reduced `max_tokens` 300→120, added line-level reasoning filter, simplified prompt |
| Prompt caused reasoning loops when bio/location were null | `enricher.py` | Removed "mention their primary languages and notable work" instruction; now passes top repos by stars so model has concrete data |

---

## This Week (High Priority Only)

### 1. Fix `_resolution_status` deployment 
The 2-hint rule fix is in the code but not yet confirmed live on the running server. Restart uvicorn, delete the existing TJ profile, re-resolve, and verify `resolution_status: RESOLVED` comes back.

### 2. Structured Audit Logging
**Why:** Right now debugging an AMBIGUOUS profile is blind — no record of which signals fired or why.

**What:** Add a JSON log event per resolution run recording:
- Which signals fired per source (hint, name match, email, link-back)
- Final confidence per source
- Resolution status decision and why

Store in existing `api_call_log` pattern or a new `resolution_log` column on `persons`. No new table needed if stored as JSONB.

### 3. LLM Fallback Quality
**Why:** OpenRouter free tier returns empty responses under load fairly often. The deterministic fallback fires correctly but produces awkward output (trailing space after language list: "JavaScript, Go, Shell, Ruby .").

**What:**
- Fix the trailing punctuation bug in `_build_fallback_summary` (the `. ` after language list when bio is absent)
- Add top 2 repos by stars to fallback output so it matches LLM quality when LLM is unavailable
- Log which path fired (LLM vs fallback) so it's visible in `/health`

### 4. Circuit Breaker for Flaky Providers
**Why:** dev.to and Stack Exchange return 503 regularly. Currently every failure burns a full retry timeout before moving on, slowing resolution by several seconds.

**What:** Wrap each provider with a simple counter-based circuit breaker. After 3 failures in 60 seconds, skip that provider immediately and mark `provider_statuses[source] = "CIRCUIT_OPEN"`. Auto-reset after 60s cooldown. dev.to already has partial circuit breaker logic — generalise it to all providers.

### 5. `name_exact` Not Firing for GitHub 
**Why:** GitHub handle `tj` has display name "TJ Holowaychuk" which should match `req.name` exactly and add +0.25, bringing confidence to 0.60 (RESOLVED on single source). Currently not firing — likely the GitHub fetcher returns the name under a different key or with different casing.

**What:** Add a debug print in `_score` to log `api_name` vs `req_name` for GitHub, identify the mismatch, fix the field key in `github.py` or the comparison in `_score`.