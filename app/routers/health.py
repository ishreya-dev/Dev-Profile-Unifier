from fastapi import APIRouter
from app.services.observer import metrics
from app.services.database import get_db

router = APIRouter()


@router.get("/health")
async def health():
    db = get_db()
    gh = metrics.github_rate_limit

    call_counts = metrics.call_counts
    llm_stats   = metrics.llm_stats
    profile_stats = await _profile_stats(db)
    enrichment_stats = await _enrichment_stats(db)
    api_latency = await _api_latency_avg(db)
    api_failures = await _api_failures(db)
    resolution_stats = await _resolution_stats(db)
    failed_profiles = await _failed_profiles(db)

    return {
        "status": "ok",
        "uptime_seconds": metrics.uptime_seconds(),
        "github_rate_limit": {
            "remaining": gh.get("remaining"),
            "limit":     gh.get("limit"),
            "reset_utc": gh.get("reset_utc"),
        },
        "api_calls_by_source": dict(call_counts),
        "api_latency_avg_ms": api_latency,
        "api_failures_by_source": api_failures,
        "llm": {
            "total_tokens":   llm_stats["total_tokens"],
            "prompt_tokens":  llm_stats["prompt_tokens"],
            "output_tokens":  llm_stats["output_tokens"],
            "calls":          llm_stats["calls"],
            "est_cost_usd":   llm_stats["est_cost_usd"],
        },
        "profiles": profile_stats,
        "enrichment": enrichment_stats,
        "resolution": resolution_stats,
        "failed_profiles": failed_profiles,
    }


async def _profile_stats(db) -> dict:
    resp = db.table("persons").select("resolution_status").execute()
    rows = resp.data or []
    return {
        "total":     len(rows),
        "resolved":  sum(1 for r in rows if r["resolution_status"] == "RESOLVED"),
        "ambiguous": sum(1 for r in rows if r["resolution_status"] == "AMBIGUOUS"),
        "pending":   sum(1 for r in rows if r["resolution_status"] == "PENDING"),
        "failed":    sum(1 for r in rows if r["resolution_status"] == "FAILED"),
    }


async def _enrichment_stats(db) -> dict:
    resp = db.table("persons").select("enrichment_status").execute()
    rows = resp.data or []
    return {
        "ready":   sum(1 for r in rows if r.get("enrichment_status") == "READY"),
        "pending": sum(1 for r in rows if r.get("enrichment_status") == "PENDING"),
        "failed":  sum(1 for r in rows if r.get("enrichment_status") == "FAILED"),
    }


async def _api_latency_avg(db) -> dict[str, int]:
    resp = db.table("api_call_log").select("source, latency_ms").execute()
    rows = resp.data or []
    totals: dict[str, list[int]] = {}
    for r in rows:
        if r.get("latency_ms") is not None:
            totals.setdefault(r["source"], []).append(r["latency_ms"])
    return {
        source: int(sum(vals) / len(vals))
        for source, vals in totals.items()
        if vals
    }


async def _api_failures(db) -> dict[str, int]:
    resp = db.table("api_call_log").select("source, status_code").execute()
    rows = resp.data or []
    failures: dict[str, int] = {}
    for r in rows:
        code = r.get("status_code")
        if code is not None and code >= 400:
            source = r["source"]
            if source == "stackexchange":
                source = "stackoverflow"
            failures[source] = failures.get(source, 0) + 1
    return failures


async def _resolution_stats(db) -> dict:
    resp = db.table("persons").select("last_resolution_ms").execute()
    rows = [r for r in (resp.data or []) if r.get("last_resolution_ms") is not None]
    if not rows:
        return {"avg_time_ms": 0}
    avg = sum(r["last_resolution_ms"] for r in rows) / len(rows)
    return {"avg_time_ms": int(avg)}


async def _failed_profiles(db) -> list[dict]:
    resp = (
        db.table("persons")
        .select("id, last_error, last_error_at")
        .eq("resolution_status", "FAILED")
        .execute()
    )
    return [
        {
            "id":           r["id"],
            "last_error":   r.get("last_error"),
            "last_error_at": str(r["last_error_at"]) if r.get("last_error_at") else None,
        }
        for r in (resp.data or [])
    ]
