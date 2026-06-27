from __future__ import annotations
from ingestion.base import BaseFetcher

_BASE = "https://dev.to/api"


class DevToFetcher(BaseFetcher):
    source   = "devto"
    base_url = _BASE

    async def fetch(self, handle: str) -> dict:
        profile_raw = await self._get(f"{_BASE}/profiles/{handle}")
        articles_raw = await self._get(
            f"{_BASE}/articles",
            params={"username": handle, "per_page": 10},
        )

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