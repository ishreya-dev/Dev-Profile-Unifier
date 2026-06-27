from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from ingestion.github import GitHubFetcher
from ingestion.stackexchange import StackExchangeFetcher
from ingestion.devto import DevToFetcher
from ingestion.hackernews import HackerNewsFetcher
from app.routers.schemas import ResolveRequest

PROVIDER_REGISTRY: dict[str, dict] = {
    "github": {
        "fetcher": GitHubFetcher,
        "priority": 100,
        "ttl_hours": 24,
        "supports_username": True,
        "supports_name_search": False,
    },
    "stackexchange": {
        "fetcher": StackExchangeFetcher,
        "priority": 80,
        "ttl_hours": 48,
        "supports_username": True,
        "supports_name_search": True,
    },
    "devto": {
        "fetcher": DevToFetcher,
        "priority": 60,
        "ttl_hours": 24,
        "supports_username": True,
        "supports_name_search": False,
    },
    "hackernews": {
        "fetcher": HackerNewsFetcher,
        "priority": 40,
        "ttl_hours": 72,
        "supports_username": True,
        "supports_name_search": False,
    },
}

SCORING_SIGNALS: dict[str, dict] = {
    "name":       {"weight": 0.25, "type": "weak"},
    "email_hint": {"weight": 0.30, "type": "weak"},
    "location":   {"weight": 0.10, "type": "weak"},
}


def is_stale(profile) -> bool:
    last_resolved = getattr(profile, "last_resolved_at", None)
    if not last_resolved:
        return False
    if isinstance(last_resolved, str):
        last_resolved = datetime.fromisoformat(last_resolved.replace("Z", "+00:00"))
    ttl = timedelta(hours=int(os.getenv("PROFILE_TTL_HOURS", 24)))
    return (datetime.now(timezone.utc) - last_resolved) > ttl


def has_new_evidence(latest_query: dict, new_req: ResolveRequest) -> bool:
    for provider in PROVIDER_REGISTRY:
        field = "stackoverflow" if provider == "stackexchange" else provider
        if getattr(new_req, field, None) and not latest_query.get(field):
            return True
    return False
