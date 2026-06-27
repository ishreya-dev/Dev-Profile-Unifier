from __future__ import annotations
from ingestion.base import BaseFetcher

_ALGOLIA = "https://hn.algolia.com/api/v1"
_HN_API  = "https://hacker-news.firebaseio.com/v0"


class HackerNewsFetcher(BaseFetcher):
    source   = "hackernews"
    base_url = _ALGOLIA

    async def fetch(self, handle: str) -> dict:
        # 1. Profile from official HN API
        try:
            profile = await self._get(f"{_HN_API}/user/{handle}.json")
        except Exception:
            profile = {}

        # 2. Recent submissions via Algolia
        submissions = await self._get(
            f"{_ALGOLIA}/search",
            params={
                "tags":    f"author_{handle},story",
                "hitsPerPage": 10,
            },
        )

        # 3. Recent comments via Algolia
        comments = await self._get(
            f"{_ALGOLIA}/search",
            params={
                "tags":    f"author_{handle},comment",
                "hitsPerPage": 10,
            },
        )

        # Derive domains the user links to
        domains: dict[str, int] = {}
        for hit in submissions.get("hits", []):
            url = hit.get("url", "")
            if url:
                from urllib.parse import urlparse
                d = urlparse(url).netloc.lstrip("www.")
                if d:
                    domains[d] = domains.get(d, 0) + 1

        return {
            "handle":          handle,
            "karma":           profile.get("karma"),
            "about":           profile.get("about"),
            "created":         profile.get("created"),
            "submission_count": submissions.get("nbHits", 0),
            "comment_count":    comments.get("nbHits",    0),
            "top_domains": dict(sorted(domains.items(), key=lambda x: -x[1])[:5]),
            "recent_stories": [
                {
                    "title":       h.get("title"),
                    "url":         h.get("url"),
                    "points":      h.get("points"),
                    "created_at":  h.get("created_at"),
                }
                for h in submissions.get("hits", [])[:5]
            ],
        }