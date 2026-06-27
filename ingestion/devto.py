from __future__ import annotations
import time
from ingestion.base import BaseFetcher

_BASE = "https://dev.to/api"

# Simple in-process circuit breaker for dev.to
# Trips after 3 consecutive failures; resets after 5 minutes.
_FAILURE_COUNT   = 0
_TRIPPED_AT: float | None = None
_MAX_FAILURES    = 3
_RESET_SECONDS   = 300  # 5 minutes


def _is_tripped() -> bool:
    global _FAILURE_COUNT, _TRIPPED_AT
    if _TRIPPED_AT is None:
        return False
    if time.monotonic() - _TRIPPED_AT > _RESET_SECONDS:
        # Auto-reset after cooldown
        _FAILURE_COUNT = 0
        _TRIPPED_AT    = None
        return False
    return _FAILURE_COUNT >= _MAX_FAILURES


def _record_failure() -> None:
    global _FAILURE_COUNT, _TRIPPED_AT
    _FAILURE_COUNT += 1
    if _FAILURE_COUNT >= _MAX_FAILURES and _TRIPPED_AT is None:
        _TRIPPED_AT = time.monotonic()


def _record_success() -> None:
    global _FAILURE_COUNT, _TRIPPED_AT
    _FAILURE_COUNT = 0
    _TRIPPED_AT    = None


class DevToFetcher(BaseFetcher):
    source   = "devto"
    base_url = _BASE

    async def fetch(self, handle: str) -> dict:
        if _is_tripped():
            raise RuntimeError(
                f"dev.to circuit breaker open after {_MAX_FAILURES} consecutive failures. "
                f"Will retry in {_RESET_SECONDS // 60} minutes."
            )

        try:
            profile_raw = await self._get(f"{_BASE}/users/by_username", params={"url": handle})
            articles_raw = await self._get(
                f"{_BASE}/articles",
                params={"username": handle, "per_page": 10},
            )
        except Exception as exc:
            _record_failure()
            raise exc

        _record_success()

        all_tags: dict[str, int] = {}
        for a in articles_raw:
            for tag in a.get("tag_list", []):
                all_tags[tag] = all_tags.get(tag, 0) + 1

        return {
            "handle":         handle,
            "name":           profile_raw.get("name"),
            "summary":        profile_raw.get("summary"),
            "location":       profile_raw.get("location"),
            "github_url":     profile_raw.get("github_url"),
            "twitter_url":    profile_raw.get("twitter_username"),
            "website_url":    profile_raw.get("website_url"),
            "article_count":  len(articles_raw),
            "top_tags":       dict(sorted(all_tags.items(), key=lambda x: -x[1])[:10]),
            "articles": [
                {
                    "title":          a["title"],
                    "published_at":   a.get("published_at"),
                    "reactions":      a.get("public_reactions_count"),
                    "reading_time":   a.get("reading_time_minutes"),
                    "tags":           a.get("tag_list", []),
                }
                for a in articles_raw[:5]
            ],
        }