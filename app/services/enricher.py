from __future__ import annotations
import os
from pathlib import Path
import httpx
from dotenv import load_dotenv
from app.services.observer import metrics

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent"
)


async def generate_summary(canonical: dict, raw: dict) -> tuple[str, dict]:
    """
    Build a prompt from the unified profile and call Gemini.
    Returns (summary_text, usage_dict).
    """
    prompt = _build_prompt(canonical, raw)
    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:
        fallback = _build_fallback_summary(canonical, raw)
        return fallback, {
            "prompt_tokens": 0,
            "output_tokens": 0,
            "model": "gemini-2.0-flash",
        }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _GEMINI_URL,
                params={"key": api_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "maxOutputTokens": 300,
                        "temperature":     0.4,
                    },
                },
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception:
        fallback = _build_fallback_summary(canonical, raw)
        return fallback, {
            "prompt_tokens": 0,
            "output_tokens": 0,
            "model": "gemini-2.0-flash",
        }

    candidate = _extract_candidate_text(body)
    if not candidate:
        candidate = _build_fallback_summary(canonical, raw)

    usage     = body.get("usageMetadata", {})
    prompt_t  = usage.get("promptTokenCount", 0)
    output_t  = usage.get("candidatesTokenCount", 0)

    metrics.record_llm_usage(prompt_t, output_t)

    return candidate.strip(), {
        "prompt_tokens":  prompt_t,
        "output_tokens":  output_t,
        "model": "gemini-2.0-flash",
    }


def _extract_candidate_text(body: dict) -> str | None:
    candidates = body.get("candidates") or []
    for candidate in candidates:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def _build_fallback_summary(canonical: dict, raw: dict) -> str:
    name = canonical.get("display_name") or "This developer"
    location = canonical.get("location")
    bio = canonical.get("bio")

    skills: list[str] = []
    for source in ("github", "stackexchange", "devto"):
        data = raw.get(source, {}) or {}
        if source == "github" and data.get("languages"):
            skills.extend(list(data["languages"].keys())[:3])
        elif source == "stackexchange" and data.get("top_tags"):
            skills.extend(list(data["top_tags"][:3]))
        elif source == "devto" and data.get("top_tags"):
            skills.extend(list(data["top_tags"].keys())[:3])

    summary_parts = [f"{name} is a developer"]
    if skills:
        summary_parts.append(f"with experience in {', '.join(skills[:4])}")
    if location:
        summary_parts.append(f"based in {location}")
    if bio:
        summary_parts.append(f"and known for {bio}")

    return " ".join(summary_parts).strip() + "."


def _build_prompt(canonical: dict, raw: dict) -> str:
    gh  = raw.get("github", {})
    so  = raw.get("stackexchange", {})
    dev = raw.get("devto", {})
    hn  = raw.get("hackernews", {})

    parts = [
        f"Name: {canonical.get('display_name', 'Unknown')}",
        f"Location: {canonical.get('location', 'N/A')}",
        f"Bio: {canonical.get('bio', 'N/A')}",
    ]
    if gh.get("languages"):
        parts.append(f"GitHub languages: {', '.join(gh['languages'].keys())}")
    if gh.get("public_repos"):
        parts.append(f"Public repos: {gh['public_repos']}, Followers: {gh.get('followers')}")
    if so.get("top_tags"):
        parts.append(f"Stack Overflow top tags: {', '.join(so['top_tags'][:5])}, Reputation: {so.get('reputation')}")
    if dev.get("top_tags"):
        parts.append(f"dev.to article tags: {', '.join(list(dev['top_tags'].keys())[:5])}, Articles: {dev.get('article_count')}")
    if hn.get("karma"):
        parts.append(f"Hacker News karma: {hn['karma']}")

    profile_text = "\n".join(parts)

    return (
        "You are a technical recruiter writing a concise developer profile. "
        "Based on the following data aggregated from public developer platforms, "
        "write exactly one paragraph (3–5 sentences) summarising this person's "
        "skills, focus areas, and recent activity. "
        "Be specific — mention languages, technologies, and topics. "
        "Do not invent information not present in the data.\n\n"
        f"{profile_text}"
    )