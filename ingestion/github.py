from __future__ import annotations
import os
from datetime import datetime, timezone
from ingestion.base import BaseFetcher
from app.services.observer import metrics


class GitHubFetcher(BaseFetcher):
    source   = "github"
    base_url = "https://api.github.com"

    def __init__(self):
        super().__init__()
        token = os.environ.get("GITHUB_TOKEN", "")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept":        "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _capture_rate_limit(self, resp) -> None:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        limit     = resp.headers.get("X-RateLimit-Limit")
        reset_ts  = resp.headers.get("X-RateLimit-Reset")
        if remaining:
            reset_utc = datetime.fromtimestamp(int(reset_ts), tz=timezone.utc).isoformat()
            metrics.update_github_rate_limit(int(remaining), int(limit), reset_utc)

    async def fetch(self, handle: str) -> dict:
        profile = await self._get(
            f"{self.base_url}/users/{handle}",
            headers=self._headers,
            on_response=self._capture_rate_limit,
        )

        repos_raw = await self._get(
            f"{self.base_url}/users/{handle}/repos",
            params={"per_page": 30, "sort": "pushed"},
            headers=self._headers,
            on_response=self._capture_rate_limit,
        )

        events_raw = await self._get(
            f"{self.base_url}/users/{handle}/events/public",
            params={"per_page": 30},
            headers=self._headers,
            on_response=self._capture_rate_limit,
        )

        languages = _extract_languages(repos_raw)
        recent_activity = _extract_activity(events_raw)

        return {
            "handle":          handle,
            "name":            profile.get("name"),
            "email":           profile.get("email"),
            "bio":             profile.get("bio"),
            "location":        profile.get("location"),
            "avatar_url":      profile.get("avatar_url"),
            "blog":            profile.get("blog"),
            "company":         profile.get("company"),
            "public_repos":    profile.get("public_repos"),
            "followers":       profile.get("followers"),
            "languages":       languages,
            "recent_activity": recent_activity,
            "repos":           [_slim_repo(r) for r in repos_raw[:10]],
        }


def _extract_languages(repos: list[dict]) -> dict[str, int]:
    lang_count: dict[str, int] = {}
    for r in repos:
        lang = r.get("language")
        if lang:
            lang_count[lang] = lang_count.get(lang, 0) + 1
    return dict(sorted(lang_count.items(), key=lambda x: -x[1])[:10])


def _extract_activity(events: list[dict]) -> list[dict]:
    keep = ("PushEvent", "PullRequestEvent", "IssuesEvent", "CreateEvent")
    return [
        {
            "type":    e["type"],
            "repo":    e["repo"]["name"],
            "created": e["created_at"],
        }
        for e in events if e.get("type") in keep
    ][:15]


def _slim_repo(r: dict) -> dict:
    return {
        "name":        r["name"],
        "description": r.get("description"),
        "language":    r.get("language"),
        "stars":       r.get("stargazers_count"),
        "forks":       r.get("forks_count"),
        "pushed_at":   r.get("pushed_at"),
    }
