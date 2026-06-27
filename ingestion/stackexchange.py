from __future__ import annotations
import os
from ingestion.base import BaseFetcher

_BASE = "https://api.stackexchange.com/2.3"
_SITE = "stackoverflow"


class StackExchangeFetcher(BaseFetcher):
    source   = "stackexchange"
    base_url = _BASE

    def __init__(self):
        super().__init__()
        self._key = os.environ.get("STACK_EXCHANGE_KEY", "")

    def _p(self, extra: dict | None = None) -> dict:
        params = {"site": _SITE, "key": self._key}
        if extra:
            params.update(extra)
        return params

    async def fetch(self, handle: str) -> dict:
        # handle can be a numeric user ID or a display-name search
        user_id = await self._resolve_user_id(handle)
        if not user_id:
            return {"handle": handle, "found": False}

        profile_raw = await self._get(
            f"{_BASE}/users/{user_id}", params=self._p()
        )
        profile = profile_raw["items"][0] if profile_raw.get("items") else {}

        tags_raw = await self._get(
            f"{_BASE}/users/{user_id}/top-tags",
            params=self._p({"pagesize": 10}),
        )
        top_tags = [t["tag_name"] for t in tags_raw.get("items", [])]

        answers_raw = await self._get(
            f"{_BASE}/users/{user_id}/answers",
            params=self._p({"sort": "votes", "order": "desc", "pagesize": 5,
                            "filter": "withbody"}),
        )

        return {
            "handle":         handle,
            "user_id":        user_id,
            "display_name":   profile.get("display_name"),
            "reputation":     profile.get("reputation"),
            "location":       profile.get("location"),
            "website_url":    profile.get("website_url"),
            "link":           profile.get("link"),
            "top_tags":       top_tags,
            "answer_count":   profile.get("answer_count"),
            "question_count": profile.get("question_count"),
            "top_answers": [
                {
                    "question_id": a["question_id"],
                    "score":       a["score"],
                    "tags":        a.get("tags", []),
                }
                for a in answers_raw.get("items", [])
            ],
        }

    async def _resolve_user_id(self, handle: str) -> int | None:
        if handle.isdigit():
            return int(handle)
        search = await self._get(
            f"{_BASE}/users",
            params=self._p({"inname": handle, "pagesize": 3, "sort": "reputation", "order": "desc"}),
        )
        items = search.get("items", [])
        return items[0]["user_id"] if items else None