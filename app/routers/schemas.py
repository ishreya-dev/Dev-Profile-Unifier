from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field


class ResolveRequest(BaseModel):
    name: str = Field(..., description="Full display name to search for")
    github: str | None = Field(None, description="GitHub username hint")
    stackoverflow: str | None = Field(None, description="Stack Overflow user-id or display name")
    devto: str | None = Field(None, description="dev.to username")
    hackernews: str | None = Field(None, description="Hacker News username")
    email_hint: str | None = Field(None, description="Partial or full email for cross-matching")


class SourceContribution(BaseModel):
    source: str
    handle: str
    confidence: float
    matched_on: list[str] = []
    explanation: list[str] = []
    confidence_notes: dict[str, Any] = {}


class PersonProfile(BaseModel):
    id: str
    display_name: str | None
    location: str | None
    bio: str | None
    avatar_url: str | None
    llm_summary: str | None
    resolution_status: str
    enrichment_status: str
    completeness_score: float | None = None
    provider_statuses: dict[str, str] = {}
    sources: list[SourceContribution]
    attributes: dict[str, Any]
    conflicts: list[dict] = []
    last_resolved_at: str | None = None
    retry_count: int = 0
    last_error: str | None = None
    latest_query: dict[str, Any] | None = None
    created_at: str
    updated_at: str


class ResolveResponse(BaseModel):
    profile_id: str
    resolution_status: str
    enrichment_status: str
    message: str
