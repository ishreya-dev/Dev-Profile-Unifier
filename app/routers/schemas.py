from __future__ import annotations
from typing import Any
from pydantic import BaseModel, field_validator, Field


class ResolveRequest(BaseModel):
    name: str = Field(..., description="Full display name to search for")
    github: str | None = Field(None, description="GitHub username hint")
    stackoverflow: str | None = Field(None, description="Stack Overflow user-id or display name")
    devto: str | None = Field(None, description="dev.to username")
    hackernews: str | None = Field(None, description="Hacker News username")
    email_hint: str | None = Field(None, description="Partial or full email for cross-matching")

    @field_validator("github", "stackoverflow", "devto", "hackernews", "email_hint", mode="before")
    @classmethod
    def reject_placeholders(cls, v):
        if isinstance(v, str) and v.strip().lower() in {
            "null", "string", "none", "undefined", ""
        }:
            return None
        return v


class SourceContribution(BaseModel):
    source: str
    handle: str
    confidence: float
    matched_on: list[str] = Field(default_factory=list)
    explanation: list[str] = Field(default_factory=list)
    confidence_notes: dict[str, Any] = Field(default_factory=dict)


class RawFetchedSource(BaseModel):
    source: str
    handle: str
    fetched_at: str
    confidence: float | None = None
    linked: bool = False  # True if it made it into source_links


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
    provider_statuses: dict[str, str] = Field(default_factory=dict)
    sources: list[SourceContribution]
    raw_fetched: list[RawFetchedSource] = Field(default_factory=list)
    attributes: dict[str, Any]
    conflicts: list[dict] = Field(default_factory=list)
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