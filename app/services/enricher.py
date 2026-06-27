from __future__ import annotations

import os
import html
from pathlib import Path

import httpx
from dotenv import load_dotenv
from app.services.observer import metrics

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


# async def generate_summary(canonical: dict, raw: dict) -> tuple[str, dict]:
#     """
#     Build a prompt from the unified profile and call OpenRouter.
#     Returns (summary_text, usage_dict).
#     """

#     prompt = _build_prompt(canonical, raw)
#     api_key = os.environ.get("OPENROUTER_API_KEY")

async def generate_summary(canonical: dict, raw: dict) -> tuple[str, dict]:
    
    """
    Build a prompt from the unified profile and call OpenRouter.
    Returns (summary_text, usage_dict).
    """

    print(f"[enricher] generate_summary called for: {canonical.get('display_name')}")
    prompt = _build_prompt(canonical, raw)
    api_key = os.environ.get("OPENROUTER_API_KEY")
    print(f"[enricher] API key found: {'Yes' if api_key else 'NO KEY'}")

    # No API key → fallback
    if not api_key:
        fallback = _build_fallback_summary(canonical, raw)
        metrics.record_llm_usage(0, 0)
        return fallback, {
            "prompt_tokens": 0,
            "output_tokens": 0,
            "model": "fallback-no-key",
        }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "openrouter/free",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 300,
                },
            )
            resp.raise_for_status()
            body = resp.json()

    except Exception as exc:
        print(f"[enricher] OpenRouter call failed: {exc}")
        fallback = _build_fallback_summary(canonical, raw)
        metrics.record_llm_usage(0, 0)
        return fallback, {
            "prompt_tokens": 0,
            "output_tokens": 0,
            "model": "fallback-error",
        }

    # Extract response — handle both content and reasoning fields
    candidate = _extract_candidate_text(body)
    if not candidate:
        candidate = _build_fallback_summary(canonical, raw)

    usage = body.get("usage", {}) or {}
    prompt_t = usage.get("prompt_tokens", 0)
    output_t = usage.get("completion_tokens", 0)
    model_used = body.get("model", "openrouter/free")

    metrics.record_llm_usage(prompt_t, output_t)

    return candidate.strip(), {
        "prompt_tokens": prompt_t,
        "output_tokens": output_t,
        "model": model_used,
    }


def _extract_candidate_text(body: dict) -> str | None:
    choices = body.get("choices") or []
    for choice in choices:
        message = choice.get("message") or {}
        # Prefer content over reasoning — reasoning is internal thinking
        text = message.get("content")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def _build_fallback_summary(canonical: dict, raw: dict, handle: str = "") -> str:
    name = canonical.get("display_name") or "This developer"
    location = canonical.get("location")
    bio = html.unescape(canonical.get("bio") or "")  # fixed: moved inside function

    gh = raw.get("github", {}) or {}
    so = raw.get("stackexchange", {}) or {}
    hn = raw.get("hackernews", {}) or {}

    langs = list((gh.get("languages") or {}).keys())[:4]

    top_repos = sorted(
        [r for r in (gh.get("repos") or [])
         if r.get("stars", 0) > 0
         and r.get("description")
         and r.get("name") != handle],   # handle now exists as param
        key=lambda r: -r.get("stars", 0),
    )[:2]

    repo_snippets = [
        f"{r['name']} ({r['stars']:,}★)"
        for r in top_repos
        if r.get("name")
    ]

    so_tags = (so.get("top_tags") or [])[:3]
    so_rep = so.get("reputation")

    followers = gh.get("followers")
    hn_karma = hn.get("karma")

    parts = [f"{name} is a developer"]

    if langs:
        parts.append(f"working primarily in {', '.join(langs)}")
    if location:
        parts.append(f"based in {location}")
    if bio:
        parts.append(f"— {bio}")

    parts.append(".")

    extras = []

    if repo_snippets:
        extras.append(
            f"Notable repositories include {' and '.join(repo_snippets)}."
        )

    if so_tags and so_rep:
        extras.append(
            f"On Stack Overflow they are active in {', '.join(so_tags)} "
            f"with a reputation of {so_rep:,}."
        )

    if followers:
        extras.append(f"They have {followers:,} GitHub followers.")

    if hn_karma:
        extras.append(f"Hacker News karma: {hn_karma}.")

    return " ".join(parts).strip() + (" " + " ".join(extras) if extras else "")


def _build_prompt(canonical: dict, raw: dict) -> str:
    gh = raw.get("github", {})
    so = raw.get("stackexchange", {})
    dev = raw.get("devto", {})
    hn = raw.get("hackernews", {})

    parts = [
        f"Name: {canonical.get('display_name', 'Unknown')}",
        f"Location: {canonical.get('location', 'N/A')}",
        f"Bio: {canonical.get('bio', 'N/A')}",
    ]

    if gh.get("languages"):
        parts.append(
            f"GitHub languages: {', '.join(gh['languages'].keys())}"
        )

    if gh.get("public_repos"):
        parts.append(
            f"Public repos: {gh['public_repos']}, "
            f"Followers: {gh.get('followers')}"
        )

    if so.get("top_tags"):
        parts.append(
            f"Stack Overflow top tags: {', '.join(so['top_tags'][:5])}, "
            f"Reputation: {so.get('reputation')}"
        )

    if dev.get("top_tags"):
        parts.append(
            f"dev.to article tags: {', '.join(list(dev['top_tags'].keys())[:5])}, "
            f"Articles: {dev.get('article_count')}"
        )

    if hn.get("karma"):
        parts.append(f"Hacker News karma: {hn['karma']}")

    profile_text = "\n".join(parts)

    return (
    "You are a technical recruiter writing a concise developer profile. "
    "Based on the following data aggregated from public developer platforms, "
    "write exactly one paragraph (2–3 sentences, strictly under 200 words). "
    "Be specific — mention languages, technologies, and topics. "
    "Do not invent information not present in the data. "
    "Return only the paragraph, no thinking, no preamble.\n\n"
    f"{profile_text}"
    )