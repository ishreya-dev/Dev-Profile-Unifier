import pytest
import httpx
import respx
from ingestion.github import GitHubFetcher


@pytest.mark.asyncio
@respx.mock
async def test_github_fetch_slim():
    handle = "testuser"
    respx.get(f"https://api.github.com/users/{handle}").mock(
        return_value=httpx.Response(200, json={
            "name": "Test User", "bio": "dev", "location": "NYC",
            "public_repos": 5, "followers": 10, "avatar_url": "https://example.com/avatar.png",
            "email": None, "blog": "", "company": None,
        })
    )
    respx.get(f"https://api.github.com/users/{handle}/repos").mock(
        return_value=httpx.Response(200, json=[
            {"name": "repo1", "language": "Python", "stargazers_count": 3,
             "forks_count": 1, "pushed_at": "2024-01-01", "description": "test"}
        ])
    )
    respx.get(f"https://api.github.com/users/{handle}/events/public").mock(
        return_value=httpx.Response(200, json=[])
    )

    fetcher = GitHubFetcher()
    result  = await fetcher.fetch(handle)
    await fetcher.close()

    assert result["name"] == "Test User"
    assert result["languages"] == {"Python": 1}
    assert result["public_repos"] == 5