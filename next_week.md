# Next Week — Prioritized Improvements

## High Priority (Days 1–2)

### 1. Structured Logging & Audit Trail (2 hours)
**Why:** Debugging AMBIGUOUS profiles is blind without seeing which signals fired.
**Implementation:** JSON log events for:
- Each provider fetch (success, failure, latency)
- Each scoring decision (signal name, weight, confidence delta)
- Each conflict detected (field, values, resolution)
Store in Supabase `audit_logs` table for retrieval via dashboard.
**Test:** Mock two conflicting providers, assert audit trail captures both.

### 2. Circuit Breakers (3 hours)
**Why:** Stack Exchange returns 503 frequently. Currently we burn retries and clutter observability.
**Implementation:** `aiobreaker` wrapper around each provider. After 3 failures in 60s, fail-fast with cached result or partial profile.
**Test:** Mock 503 response, verify circuit opens, subsequent calls don't retry.

## Medium Priority (Days 3–4)

### 3. Merge Workflow (6 hours)
**Why:** User corrects a split decision manually (two persons → one).
**Implementation:** Admin endpoint POST `/admin/merge_persons` takes person_a, person_b, marks person_b as MERGED_INTO person_a, re-links source_links, dedupes attributes.
**Hard part:** Deciding which attributes win on conflict (timestamp? user choice?).
**Test:** Merge two persons, verify source_links follow, GET merged person returns all sources.

### 4. Redis Caching (4 hours)
**Why:** Multi-instance deployment (if scaling) loses state on restart. Single-instance needs 24h TTL refresh.
**Cost/Benefit:** Upstash free = $0 for this scope. Time: 2–3 hours. Worth it when >50 concurrent users.
**Not doing now:** Single Render instance is sufficient.

## Low Priority (Day 5 / Defer)

### 5. Webhook / Push Model (6 hours)
**Why:** Eliminates polling. Reduces 30s latency for AMBIGUOUS profiles.
**Blocker:** Requires rate limit budgeting (below) to prevent webhook storms.
**Defer until:** Webhook infra is stable, rate limiting is in place.

### 6. Rate Limit Budgeting (2 hours)
**Why:** GitHub 5k/hr is shared. One user doing 20 resolutions starves others.
**Implementation:** Token bucket per user, reserve quota before enqueuing fetch.
**Do this before item 5 (webhooks).

---

## Testing Strategy

| Feature | Unit | Integration | Notes |
|---------|------|-------------|-------|
| Logging | Mock providers, check log events | Real Supabase | Verify JSON structure |
| Breaker | Mock 503, check fail-fast | Real rate limit | Verify cache fallback |
| Merge | Mock person rows | Real DB constraints | Check FK integrity |
| Redis | Mock cache hits | Real Upstash | Verify TTL, eviction |
| Webhooks | Mock callback | Real endpoint | Test retry backoff |

## Rollout Plan

1. **Logging + Breakers** (stable features, ship immediately)
2. **Merge workflow** (admin-only, low risk)
3. **Pause, measure observability** (see if new features are actually needed)
4. **Redis + Rate limiting** (only if handling >10 concurrent resolutions)
5. **Webhooks** (polish, not MVP)