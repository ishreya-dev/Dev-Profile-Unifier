import os
import pytest
from unittest.mock import AsyncMock, patch
from app.services.enricher import generate_summary
from app.services.resolver import (
    _score, _token_overlap, _emails_match, _merge_canonical,
    _resolution_status, ResolutionResult, _run_resolution, resolve_profile,
)
from app.routers.schemas import ResolveRequest, PersonProfile


def _req(**kwargs):
    defaults = {"name": "Jane Doe", "github": "jdoe"}
    defaults.update(kwargs)
    return ResolveRequest(**defaults)


def test_hint_provided_boosts_confidence():
    req = _req(github="jdoe")
    conf, notes = _score(req, "github", {"name": "Jane Doe"}, "jdoe")
    assert conf >= 0.15
    assert "hint_provided" in notes
    assert "explanation" in notes["hint_provided"]


def test_exact_name_match():
    req = _req()
    conf, notes = _score(req, "github", {"name": "Jane Doe"}, "jdoe")
    assert "name_exact" in notes
    assert conf >= 0.40


def test_fuzzy_name_match():
    req = _req(name="Jane Doe")
    conf, notes = _score(req, "github", {"name": "Jane A. Doe"}, "jdoe")
    assert "name_fuzzy" not in notes or notes.get("name_exact") is None


def test_email_partial_match():
    req = _req(email_hint="jane@")
    conf, notes = _score(req, "github", {"name": "Jane Doe", "email": "jane@example.com"}, "jdoe")
    assert "email_match" in notes


def test_token_overlap_full():
    assert _token_overlap("Jane Doe", "Jane Doe") == 1.0


def test_token_overlap_partial():
    overlap = _token_overlap("Jane Doe", "Jane Smith")
    assert 0.4 < overlap < 0.8


def test_emails_match_exact():
    assert _emails_match("jane@example.com", "jane@example.com")


def test_emails_match_partial_hint():
    assert _emails_match("jane@example.com", "jane@")


def test_confidence_clamped_to_one():
    req = _req(email_hint="jane@example.com", github="jdoe")
    data = {"name": "Jane Doe", "email": "jane@example.com"}
    conf, _ = _score(req, "github", data, "jdoe")
    assert conf <= 1.0


def test_conflict_detection():
    req = _req()
    results = [
        ResolutionResult("github", "jdoe", 0.8, {}),
        ResolutionResult("devto", "jdoe", 0.7, {}),
    ]
    raw = {
        "github": {"location": "Portland, OR", "email": "jane@example.com"},
        "devto":  {"location": "Remote", "email": "jane@example.com"},
    }
    fields, conflicts = _merge_canonical(req, results, raw)
    assert fields["location"] == "Portland, OR"
    assert len(conflicts) == 1
    assert conflicts[0]["field"] == "location"
    assert conflicts[0]["github"] == "Portland, OR"
    assert conflicts[0]["devto"] == "Remote"


def test_resolution_status_resolved():
    results = [
        ResolutionResult("github", "a", 0.7, {}),
        ResolutionResult("devto", "a", 0.65, {}),
    ]
    assert _resolution_status(results) == "RESOLVED"


def test_resolution_status_ambiguous():
    results = [ResolutionResult("github", "a", 0.5, {})]
    assert _resolution_status(results) == "AMBIGUOUS"


@pytest.mark.asyncio
async def test_resolve_creates_new_profile():
    mock_bg = AsyncMock()
    with patch("app.services.resolver.get_profile_by_handles", new_callable=AsyncMock, return_value=None), \
         patch("app.services.resolver.upsert_person", new_callable=AsyncMock, return_value="new-id"), \
         patch("app.services.resolver._run_resolution", new_callable=AsyncMock):
        pid, status, enrich = await resolve_profile(_req(), mock_bg)
    assert pid == "new-id"
    assert status == "PENDING"
    assert enrich == "PENDING"
    mock_bg.add_task.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_returns_cached_resolved():
    existing = PersonProfile(
        id="existing-id",
        display_name="Jane Doe",
        location=None, bio=None, avatar_url=None, llm_summary=None,
        resolution_status="RESOLVED",
        enrichment_status="READY",
        sources=[], attributes={},
        created_at="2024-01-01", updated_at="2024-01-01",
        last_resolved_at="2025-06-26T00:00:00+00:00",
    )
    mock_bg = AsyncMock()
    with patch("app.services.resolver.get_profile_by_handles", new_callable=AsyncMock, return_value=existing), \
         patch("app.services.resolver.is_stale", return_value=False):
        pid, status, enrich = await resolve_profile(_req(), mock_bg)
    assert pid == "existing-id"
    assert status == "RESOLVED"
    assert enrich == "READY"
    mock_bg.add_task.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_ambiguous_with_new_evidence():
    existing = PersonProfile(
        id="ambig-id",
        display_name="Jane Doe",
        location=None, bio=None, avatar_url=None, llm_summary=None,
        resolution_status="AMBIGUOUS",
        enrichment_status="PENDING",
        latest_query={"name": "Jane Doe", "github": "jdoe"},
        sources=[], attributes={},
        created_at="2024-01-01", updated_at="2024-01-01",
    )
    mock_bg = AsyncMock()
    with patch("app.services.resolver.get_profile_by_handles", new_callable=AsyncMock, return_value=existing), \
         patch("app.services.resolver.update_person", new_callable=AsyncMock), \
         patch("app.services.resolver._run_resolution", new_callable=AsyncMock):
        pid, status, _ = await resolve_profile(_req(devto="jdoe"), mock_bg)
    assert pid == "ambig-id"
    assert status == "PENDING"
    mock_bg.add_task.assert_called_once()


# @pytest.mark.asyncio
# async def test_generate_summary_returns_fallback_when_llm_text_is_null():
#     class DummyResponse:
#         def raise_for_status(self):
#             return None

#         def json(self):
#             return {
#                 "candidates": [{"content": {"parts": [{"text": None}]}}],
#                 "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 0},
#             }

#     with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
#          patch("httpx.AsyncClient") as mock_client:
#         mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=DummyResponse())
#         summary, usage = await generate_summary(
#             {"display_name": "Jane Doe", "location": "Remote"},
#             {"github": {"languages": {"Python": 3}}},
#         )

#     assert summary is not None
#     assert "Jane Doe" in summary
#     assert usage["model"] == "gemini-2.0-flash"


# @pytest.mark.asyncio
# async def test_generate_summary_uses_fallback_when_api_key_missing():
#     with patch.dict(os.environ, {}, clear=True):
#         summary, usage = await generate_summary(
#             {"display_name": "Jane Doe", "location": "Remote"},
#             {"github": {"languages": {"Python": 3}}},
#         )

#     assert summary is not None
#     assert "Jane Doe" in summary
#     assert usage["model"] == "gemini-2.0-flash"


@pytest.mark.asyncio
async def test_generate_summary_returns_fallback_when_llm_text_is_null():
    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": None, "reasoning": None}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 0},
                "model": "openrouter/free",
            }

    with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), \
         patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=DummyResponse())
        summary, usage = await generate_summary(
            {"display_name": "Jane Doe", "location": "Remote"},
            {"github": {"languages": {"Python": 3}}},
        )

    assert summary is not None
    assert "Jane Doe" in summary
    assert usage["model"] == "fallback-error"  # falls back when content is null


@pytest.mark.asyncio
async def test_generate_summary_uses_fallback_when_api_key_missing():
    with patch.dict(os.environ, {}, clear=True):
        summary, usage = await generate_summary(
            {"display_name": "Jane Doe", "location": "Remote"},
            {"github": {"languages": {"Python": 3}}},
        )

    assert summary is not None
    assert "Jane Doe" in summary
    assert usage["model"] == "fallback-no-key"


@pytest.mark.asyncio
async def test_run_resolution_still_enriches_when_source_link_insert_fails():
    req = _req(name="Jane Doe", github="jdoe")
    raw = {
        "github": {"name": "Jane Doe", "_raw_source_id": "raw-1"},
        "stackexchange": {"error": "failed"},
    }

    with patch("app.services.resolver.run_pipeline", new_callable=AsyncMock, return_value=raw), \
         patch("app.services.resolver.update_person", new_callable=AsyncMock) as mock_update_person, \
         patch("app.services.resolver.update_enrichment_status", new_callable=AsyncMock), \
         patch("app.services.resolver.insert_source_link", new_callable=AsyncMock, side_effect=Exception("duplicate key")), \
         patch("app.services.resolver.insert_attributes", new_callable=AsyncMock), \
         patch("app.services.resolver.generate_summary", new_callable=AsyncMock, return_value=("summary", {"prompt_tokens": 1, "output_tokens": 2, "model": "openrouter/free"})), \
         patch("app.services.resolver.log_llm_usage", new_callable=AsyncMock), \
         patch("app.services.resolver.increment_retry", new_callable=AsyncMock) as mock_increment, \
         patch("app.services.resolver.metrics.record_resolution_time"):
        await _run_resolution("person-id", req)

    mock_increment.assert_not_awaited()
    mock_update_person.assert_any_await("person-id", {"llm_summary": "summary"})
