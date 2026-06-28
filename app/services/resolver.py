from __future__ import annotations
import os
import re
import urllib.parse
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.routers.schemas import ResolveRequest
from app.services.database import (
    MAX_RETRIES,
    upsert_person,
    update_person,
    update_enrichment_status,
    increment_retry,
    get_profile_by_handles,
    insert_source_link,
    insert_attributes,
    log_llm_usage,
    get_person_by_id,          # ADD THIS — needed for downgrade guard in _run_resolution
)
from app.services.enricher import generate_summary
from app.services.observer import metrics
from ingestion.pipeline import run_pipeline
from ingestion.providers import is_stale, has_new_evidence


# Confidence scoring weights (sum to 1.0)
W_HINT_PROVIDED   = 0.35
W_HANDLE_EXACT    = 0.20
W_NAME_EXACT      = 0.25
W_EMAIL_MATCH     = 0.15
W_LINK_BACK       = 0.05


COMPLETENESS_FIELDS = [
    "bio", "location", "avatar_url", "canonical_email",
    "llm_summary", "github", "stackexchange", "devto", "hackernews",
]


@dataclass
class ResolutionResult:
    source: str
    handle: str
    confidence: float
    notes: dict = field(default_factory=dict)


async def resolve_profile(req: ResolveRequest, background_tasks) -> tuple[str, str, str]:
    handles = _extract_handles(req)
    existing = await get_profile_by_handles(handles)

    if not existing:
        person_id = await upsert_person({
            "display_name":      req.name,
            "resolution_status": "PENDING",
            "enrichment_status": "PENDING",
            "first_query":       req.model_dump(exclude_none=True),
            "latest_query":      req.model_dump(exclude_none=True),
        })
        background_tasks.add_task(_run_resolution, person_id, req)
        return person_id, "PENDING", "PENDING"

    if existing.resolution_status == "RESOLVED":
        if is_stale(existing) or _enrichment_incomplete(existing):
            background_tasks.add_task(_run_resolution, existing.id, req)
        return existing.id, "RESOLVED", existing.enrichment_status

    if existing.resolution_status == "AMBIGUOUS":
        if has_new_evidence(existing.latest_query or {}, req) or _enrichment_incomplete(existing):
            await update_person(existing.id, {
                "latest_query":      req.model_dump(exclude_none=True),
                "resolution_status": "PENDING",
            })
            background_tasks.add_task(_run_resolution, existing.id, req)
            return existing.id, "PENDING", existing.enrichment_status
        return existing.id, "AMBIGUOUS", existing.enrichment_status

    if existing.resolution_status == "FAILED":
        if existing.retry_count < MAX_RETRIES:
            background_tasks.add_task(_run_resolution, existing.id, req)
            return existing.id, "PENDING", existing.enrichment_status
        return existing.id, "FAILED", existing.enrichment_status

    if existing.resolution_status == "PENDING":
        return existing.id, "PENDING", existing.enrichment_status

    return existing.id, existing.resolution_status, existing.enrichment_status


async def _run_resolution(person_id: str, req: ResolveRequest) -> None:
    start = time.monotonic()
    now = datetime.now(timezone.utc).isoformat()

    try:
        await update_person(person_id, {"last_attempted_at": now})

        handles = _extract_handles(req)
        raw = await run_pipeline(handles)

        provider_statuses = {
            source: "FAILED" if "error" in data else "SUCCESS"
            for source, data in raw.items()
        }
        new_success_count = sum(1 for v in provider_statuses.values() if v == "SUCCESS")

        # ── Downgrade guard ───────────────────────────────────────────────────
        # If the profile was already RESOLVED and this re-run fetched *fewer*
        # successful sources than are already on record (e.g. GitHub failed
        # transiently during a stale refresh), do NOT overwrite the richer
        # existing result with a degraded one.  Record the new provider
        # statuses for observability, then bail out.
        existing_snapshot = await get_person_by_id(person_id)
        existing_status = existing_snapshot.get("resolution_status") if existing_snapshot else None
        existing_source_count = len(existing_snapshot.get("sources") or []) if existing_snapshot else 0

        if existing_status == "RESOLVED" and new_success_count < existing_source_count:
            print(
                f"[resolver] Skipping overwrite for {person_id}: "
                f"re-run got {new_success_count} source(s) but existing RESOLVED "
                f"profile has {existing_source_count}. Keeping existing data."
            )
            # Still persist the provider statuses so /health reflects the
            # transient failure — but leave all resolution fields untouched.
            await update_person(person_id, {"provider_statuses": provider_statuses})
            return
        # ─────────────────────────────────────────────────────────────────────

        await update_person(person_id, {"provider_statuses": provider_statuses})

        results: list[ResolutionResult] = []

        for source, data in raw.items():
            if "error" in data:
                print(f"[resolver] Skipping {source}: {data['error']}")
                continue
            try:
                handle = handles.get(source) or data.get("handle", "")
                conf, notes = _score(req, source, data, handle)
                results.append(ResolutionResult(source, handle, conf, notes))
            except Exception as exc:
                print(f"[resolver] Error scoring {source}: {exc}")
                continue

        if not results:
            await update_person(person_id, {
                "resolution_status": "FAILED",
                "last_error": "No providers returned valid data",
                "last_error_at": now,
            })
            return

        try:
            canonical, conflicts = _merge_canonical(req, results, raw)
            completeness = _completeness_score(canonical, results)
            status = _resolution_status(results)

            update_fields: dict = {
                **canonical,
                "resolution_status": status,
                "conflicts": conflicts,
                "completeness_score": completeness,
                "latest_query": req.model_dump(exclude_none=True),
                "last_resolved_at": datetime.now(timezone.utc).isoformat(),
            }

            await update_person(person_id, update_fields)
        except Exception as exc:
            print(f"[resolver] Error merging results for {person_id}: {exc}")
            await update_person(person_id, {
                "resolution_status": "FAILED",
                "last_error": str(exc),
                "last_error_at": now,
            })
            return

        for r in results:
            raw_id = raw[r.source].get("_raw_source_id")
            if raw_id:
                try:
                    await insert_source_link(
                        person_id, raw_id, r.source, r.handle, r.confidence, r.notes,
                    )
                except Exception as exc:
                    print(f"[resolver] skipped source link insert for {person_id}/{r.source}: {exc}")

            attrs = _extract_attributes(r.source, raw[r.source])
            if attrs:
                try:
                    await insert_attributes(person_id, r.source, attrs)
                except Exception as exc:
                    print(f"[resolver] skipped attribute insert for {person_id}/{r.source}: {exc}")

        run_id = f"{person_id[:8]}-{int(time.monotonic() * 1000) % 1_000_000}"
        await update_enrichment_status(person_id, "LLM_RUNNING")
        try:
            print(f"[enricher][{run_id}] calling generate_summary, canonical keys: {list(canonical.keys())}")
            print(f"[enricher][{run_id}] OPENROUTER_API_KEY set: {bool(os.environ.get('OPENROUTER_API_KEY'))}")
            summary, usage = await generate_summary(canonical, raw)
            print(f"[enricher][{run_id}] summary result: {summary[:80] if summary else 'EMPTY'}")

            await update_person(person_id, {
                "llm_summary": summary,
                "enrichment_status": "READY",
            })
            await log_llm_usage(
                person_id,
                usage["prompt_tokens"],
                usage["output_tokens"],
                usage["model"],
            )
            print(f"[enricher][{run_id}] persisted to DB, enrichment_status=READY")
        except Exception as exc:
            print(f"[enricher][{run_id}] failed for {person_id}: {exc}")
            await update_enrichment_status(person_id, "FAILED")

        elapsed_ms = int((time.monotonic() - start) * 1000)
        await update_person(person_id, {"last_resolution_ms": elapsed_ms})
        metrics.record_resolution_time(elapsed_ms)

    except Exception as exc:
        print(f"[resolver] failed for {person_id}: {exc}")
        await increment_retry(person_id, str(exc))


def _extract_handles(req: ResolveRequest) -> dict[str, str | None]:
    return {
        "github":        req.github,
        "stackexchange": req.stackoverflow,
        "devto":         req.devto,
        "hackernews":    req.hackernews,
    }


def _enrichment_incomplete(profile) -> bool:
    status = getattr(profile, "enrichment_status", None)
    if status in ("READY", "LLM_RUNNING"):
        return False
    completeness = getattr(profile, "completeness_score", None)
    if completeness is not None:
        return completeness < 1.0
    for field in ("bio", "location", "avatar_url", "llm_summary"):
        value = getattr(profile, field, None)
        if value in (None, ""):
            return True
    return False


def _score(req: ResolveRequest, source: str, data: dict, handle: str) -> tuple[float, dict]:
    score = 0.0
    notes: dict = {}

    hint_handle = getattr(req, source, None) or getattr(req, _alias(source), None)
    if hint_handle and hint_handle.lower() == handle.lower():
        score += W_HINT_PROVIDED
        notes["hint_provided"] = {
            "weight": W_HINT_PROVIDED,
            "explanation": f"+{W_HINT_PROVIDED} Caller supplied handle '{handle}' directly",
        }

    api_name = (data.get("name") or data.get("display_name") or "").strip()
    req_name = req.name.strip()
    if api_name and req_name:
        if api_name.lower() == req_name.lower():
            score += W_NAME_EXACT
            notes["name_exact"] = {
                "weight": W_NAME_EXACT,
                "explanation": f"+{W_NAME_EXACT} Exact name match: '{api_name}'",
            }

    if req.email_hint:
        api_email = data.get("email") or ""
        if api_email and _emails_match(api_email, req.email_hint):
            score += W_EMAIL_MATCH
            notes["email_match"] = {
                "weight": W_EMAIL_MATCH,
                "explanation": f"+{W_EMAIL_MATCH} Email matches hint",
            }

    score_lb, note_lb = _check_link_back(source, data, req)
    if note_lb:
        score += score_lb
        notes.update(note_lb)

    confidence = min(score, 1.0)
    return confidence, notes


def _check_link_back(source: str, data: dict, req: ResolveRequest) -> tuple[float, dict]:
    if source == "devto":
        gh_url = data.get("github_url") or ""
        if req.github and gh_url:
            match = re.search(r"github\.com/([a-z0-9-]+)", gh_url.lower())
            if match and match.group(1) == req.github.lower():
                return W_LINK_BACK, {
                    "devto_links_github": {
                        "weight": W_LINK_BACK,
                        "explanation": f"+{W_LINK_BACK} dev.to profile links back to github.com/{req.github}",
                    },
                }

    if source == "github":
        blog = (data.get("blog") or "").lower()
        if req.devto and f"dev.to/{req.devto}".lower() in blog:
            return W_LINK_BACK, {
                "github_links_devto": {
                    "weight": W_LINK_BACK,
                    "explanation": f"+{W_LINK_BACK} GitHub blog links to dev.to/{req.devto}",
                },
            }
        if req.stackoverflow and "stackoverflow.com" in blog:
            weight = W_LINK_BACK * 0.5
            return weight, {
                "github_links_stackoverflow": {
                    "weight": weight,
                    "explanation": f"+{weight} GitHub blog links to Stack Overflow",
                },
            }

    return 0.0, {}


def _alias(source: str) -> str:
    return {"stackexchange": "stackoverflow"}.get(source, source)


def _token_overlap(a: str, b: str) -> float:
    ta = set(re.split(r"\W+", a.lower())) - {""}
    tb = set(re.split(r"\W+", b.lower())) - {""}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _emails_match(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    return a == b


def _resolution_status(results: list[ResolutionResult]) -> str:
    
    print(f"[resolver] _resolution_status called with {len(results)} results")  # temp
    hint_provided = [r for r in results if "hint_provided" in r.notes]
    print(f"[resolver] hint_provided count: {len(hint_provided)}")  # temp

    if not results:
        return "AMBIGUOUS"
    confident = [r for r in results if r.confidence >= 0.50]
    if len(confident) >= 2:
        return "RESOLVED"
    if any(r.confidence >= 0.60 for r in results):
        return "RESOLVED"
    # ADD THIS: 2+ explicit hints = caller confirmed both handles directly
    hint_provided = [r for r in results if "hint_provided" in r.notes]
    if len(hint_provided) >= 2:
        return "RESOLVED"
    if any(r.confidence >= 0.35 for r in results):
        return "AMBIGUOUS"
    return "AMBIGUOUS"


def _merge_canonical(
    req: ResolveRequest,
    results: list[ResolutionResult],
    raw: dict,
) -> tuple[dict, list[dict]]:
    ranked = sorted(results, key=lambda r: -r.confidence)
    fields: dict = {"display_name": req.name}
    conflicts: list[dict] = []
    seen: dict[str, dict] = {}

    for r in ranked:
        data = raw.get(r.source, {})
        for field in ["bio", "location", "avatar_url", "canonical_email"]:
            value = data.get(field) if field != "canonical_email" else data.get("email")
            if field == "bio" and not value:
                value = data.get("summary") or data.get("about")
            if not value:
                continue
            if field not in seen:
                seen[field] = {"value": value, "source": r.source}
                fields[field] = value
            elif seen[field]["value"].lower() != str(value).lower():
                conflict = {"field": field, seen[field]["source"]: seen[field]["value"]}
                conflict[r.source] = value
                conflicts.append(conflict)

    return {k: v for k, v in fields.items() if v is not None}, conflicts


def _completeness_score(canonical: dict, results: list[ResolutionResult]) -> float:
    # FIX: was checking `any(r.source in [...])` which is always True when any
    # result exists — making completeness_score always return 1.0 regardless of
    # whether canonical fields (bio, location, etc.) were actually populated.
    # Now correctly checks whether the field is actually present in canonical,
    # or whether the source platform was successfully reached (platform presence
    # means those platform-specific COMPLETENESS_FIELDS are counted as filled).
    sources_present = {r.source for r in results}
    filled = sum(
        1 for f in COMPLETENESS_FIELDS
        if canonical.get(f) or f in sources_present
    )
    return min(1.0, filled / len(COMPLETENESS_FIELDS))


def _extract_attributes(source: str, data: dict) -> dict:
    if source == "github":
        return {
            "languages":    data.get("languages", {}),
            "public_repos": data.get("public_repos"),
            "followers":    data.get("followers"),
            "top_repos":    data.get("repos", []),
        }
    if source == "stackexchange":
        return {
            "reputation":   data.get("reputation"),
            "top_tags":     data.get("top_tags", []),
            "answer_count": data.get("answer_count"),
        }
    if source == "devto":
        return {
            "article_count": data.get("article_count"),
            "top_tags":      data.get("top_tags", {}),
        }
    if source == "hackernews":
        return {
            "karma":       data.get("karma"),
            "top_domains": data.get("top_domains", {}),
        }
    return {}