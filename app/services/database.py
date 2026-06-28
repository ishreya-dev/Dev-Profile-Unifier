from __future__ import annotations
import os
from pathlib import Path
from datetime import datetime, timezone
from supabase import create_client, Client
from dotenv import load_dotenv
from app.routers.schemas import PersonProfile, SourceContribution, RawFetchedSource

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

MAX_RETRIES = 3

_client: Client | None = None


def init_db_client() -> None:
    global _client
    url  = os.environ["SUPABASE_URL"]
    key  = os.environ["SUPABASE_SERVICE_KEY"]
    _client = create_client(url, key)


def get_db() -> Client:
    if _client is None:
        init_db_client()
    return _client


# ── Write helpers ─────────────────────────────────────────────────────────────

async def insert_raw_source(source: str, handle: str, payload: dict, meta: dict) -> str:
    db = get_db()
    res = db.table("raw_source_data").insert({
        "source":        source,
        "source_handle": handle,
        "payload":       payload,
        "fetch_meta":    meta,
    }).execute()
    return res.data[0]["id"]


async def upsert_person(fields: dict) -> str:
    db = get_db()
    res = db.table("persons").insert(fields).execute()
    return res.data[0]["id"]


async def update_person(person_id: str, fields: dict) -> None:
    get_db().table("persons").update(fields).eq("id", person_id).execute()


async def update_enrichment_status(person_id: str, status: str) -> None:
    get_db().table("persons").update({"enrichment_status": status}).eq("id", person_id).execute()


async def increment_retry(person_id: str, error: str) -> None:
    db = get_db()
    res = db.table("persons").select("retry_count").eq("id", person_id).execute()
    current = res.data[0]["retry_count"] if res.data else 0
    new_count = current + 1
    now = datetime.now(timezone.utc).isoformat()
    fields: dict = {
        "retry_count":       new_count,
        "last_error":        error,
        "last_error_at":     now,
        "last_attempted_at": now,
    }
    if new_count >= MAX_RETRIES:
        fields["resolution_status"] = "FAILED"
    db.table("persons").update(fields).eq("id", person_id).execute()


async def insert_source_link(
    person_id: str,
    raw_source_id: str,
    source: str,
    handle: str,
    confidence: float,
    notes: dict,
) -> None:
    get_db().table("source_links").insert({
        "person_id":        person_id,
        "raw_source_id":    raw_source_id,
        "source":           source,
        "source_handle":    handle,
        "confidence":       round(confidence, 3),
        "confidence_notes": notes,
    }).execute()


async def insert_attributes(person_id: str, source: str, attrs: dict) -> None:
    db = get_db()
    rows = [
        {"person_id": person_id, "source": source, "attr_key": k, "attr_value": v}
        for k, v in attrs.items()
        if v is not None
    ]
    if rows:
        db.table("person_attributes").insert(rows).execute()


async def log_api_call(source: str, endpoint: str, status: int, latency_ms: int) -> None:
    get_db().table("api_call_log").insert({
        "source": source, "endpoint": endpoint,
        "status_code": status, "latency_ms": latency_ms,
    }).execute()


async def log_llm_usage(person_id: str, prompt_tokens: int, output_tokens: int, model: str) -> None:
    get_db().table("llm_usage_log").insert({
        "person_id": person_id, "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens, "model": model,
    }).execute()


# ── Read helpers ──────────────────────────────────────────────────────────────

def _notes_to_matched(notes: dict) -> tuple[list[str], list[str]]:
    matched_on: list[str] = []
    explanation: list[str] = []
    for key, val in notes.items():
        matched_on.append(key)
        if isinstance(val, dict) and "explanation" in val:
            explanation.append(val["explanation"])
    return matched_on, explanation


async def get_profile_by_handles(handles: dict[str, str | None]) -> PersonProfile | None:
    db = get_db()
    for source, handle in handles.items():
        if not handle:
            continue
        res = (
            db.table("source_links")
            .select("person_id")
            .eq("source", source)
            .eq("source_handle", handle)
            .execute()
        )
        if res.data:
            person_id = res.data[0]["person_id"]
            return await get_profile_by_id(person_id)
    return None


async def get_profile_by_id(profile_id: str) -> PersonProfile | None:
    db = get_db()

    person_res = db.table("persons").select("*").eq("id", profile_id).execute()
    if not person_res.data:
        return None
    p = person_res.data[0]

    links_res = (
        db.table("source_links").select("*").eq("person_id", profile_id).execute()
    )
    
    # Build a map of source -> confidence for linked sources
    linked_map: dict[str, float] = {}
    sources = []
    for l in (links_res.data or []):
        notes = l["confidence_notes"] or {}
        matched_on, explanation = _notes_to_matched(notes)
        linked_map[l["source"]] = float(l["confidence"])
        sources.append(SourceContribution(
            source=l["source"],
            handle=l["source_handle"],
            confidence=float(l["confidence"]),
            matched_on=matched_on,
            explanation=explanation,
            confidence_notes=notes,
        ))

    attrs_res = (
        db.table("person_attributes").select("*").eq("person_id", profile_id).execute()
    )
    attributes: dict = {}
    for a in (attrs_res.data or []):
        attributes.setdefault(a["source"], {})[a["attr_key"]] = a["attr_value"]

    # Fetch raw source data for all handles in latest_query
    latest_query = p.get("latest_query") or {}
    queried_handles = {
        source: handle
        for source, handle in {
            "github":        latest_query.get("github"),
            "stackexchange": latest_query.get("stackoverflow"),
            "devto":         latest_query.get("devto"),
            "hackernews":    latest_query.get("hackernews"),
        }.items()
        if handle
    }

    raw_fetched = []
    for source, handle in queried_handles.items():
        raw_res = (
            db.table("raw_source_data")
            .select("source, source_handle, fetched_at")
            .eq("source", source)
            .eq("source_handle", handle)
            .order("fetched_at", desc=True)
            .limit(1)
            .execute()
        )
        if raw_res.data:
            r = raw_res.data[0]
            is_linked = source in linked_map
            raw_fetched.append(RawFetchedSource(
                source=r["source"],
                handle=r["source_handle"],
                fetched_at=str(r["fetched_at"]),
                confidence=linked_map.get(source),
                linked=is_linked,
            ))

    return PersonProfile(
        id=p["id"],
        display_name=p.get("display_name"),
        location=p.get("location"),
        bio=p.get("bio"),
        avatar_url=p.get("avatar_url"),
        llm_summary=p.get("llm_summary"),
        resolution_status=p["resolution_status"],
        enrichment_status=p.get("enrichment_status", "PENDING"),
        completeness_score=p.get("completeness_score"),
        provider_statuses=p.get("provider_statuses") or {},
        sources=sources,
        attributes=attributes,
        conflicts=p.get("conflicts") or [],
        raw_fetched=raw_fetched,
        last_resolved_at=str(p["last_resolved_at"]) if p.get("last_resolved_at") else None,
        retry_count=p.get("retry_count", 0),
        last_error=p.get("last_error"),
        latest_query=p.get("latest_query"),
        created_at=str(p["created_at"]),
        updated_at=str(p["updated_at"]),
    )