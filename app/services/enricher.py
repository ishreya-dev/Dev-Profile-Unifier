from __future__ import annotations

import os
import re
import html
from pathlib import Path

import httpx
from dotenv import load_dotenv
from app.services.observer import metrics

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Matches common safety/moderation/system-tag lines that some models append
# or substitute instead of the actual answer (e.g. "User Safety: safe").
_METADATA_PATTERNS = re.compile(
    r"""^\s*(
        user\s*safety|
        safety\s*(rating|check|classification)|
        content\s*(policy|warning)|
        \[?(disclaimer|note|system)\]?\s*:|
        moderation
    )\s*:?""",
    re.IGNORECASE | re.VERBOSE,
)

# Matches the model narrating/second-guessing its own formatting compliance
# instead of (or in addition to) actually answering — e.g. "Make sure exactly
# 2 sentences. No extra punctuation that creates more sentences? Eg. ...".
# This is reasoning-about-the-instructions leaking into the output, distinct
# from a safety tag and distinct from a clean reasoning-then-blank-line-then-
# answer shape (which the paragraph-walk already handles).
_META_COMMENTARY_PATTERNS = re.compile(
    r"""(
        make\s+sure\s+(exactly|it'?s|to)|
        exactly\s+\d+\s+sentence|
        no\s+extra\s+punctuation|
        that'?s\s+one\s+sentence|
        let\s+me\s+(think|check|count|make\s+sure)|
        i\s+(should|need\s+to|will)\s+(write|make\s+sure|keep|count)|
        as\s+(an?\s+)?(ai|language\s+model|assistant)
    )""",
    re.IGNORECASE | re.VERBOSE,
)


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
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You write short developer bios. Reply with ONLY the bio "
                                "itself — no explanation, no notes, no thinking about "
                                "format, no quotation marks around it. "
                                "Exactly 2 sentences, then stop.\n\n"
                                "Example output (this exact format, nothing added before "
                                "or after):\n"
                                "Jane Doe is a backend engineer working primarily in Go "
                                "and Rust, known for the popular library fastqueue. "
                                "She has over 12,000 GitHub followers."
                            )
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ],
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

    # Extract response — take last paragraph that looks like real content,
    # skipping any reasoning leak or safety/metadata tag the model appended.
    candidate = _extract_candidate_text(body)
    print(f"[enricher] summary result: {candidate[:80] if candidate else 'EMPTY'}")

    if not candidate:
        # Diagnostic: was there genuinely no content, or did every paragraph
        # get filtered out as metadata? This tells us which case we're in
        # without needing to reproduce it live.
        choices = body.get("choices") or []
        if not choices:
            print("[enricher] DEBUG: response had no 'choices' at all")
        else:
            for i, choice in enumerate(choices):
                raw_text = (choice.get("message") or {}).get("content")
                if not isinstance(raw_text, str) or not raw_text.strip():
                    print(f"[enricher] DEBUG: choice[{i}] content was empty/missing: {raw_text!r}")
                    continue
                paragraphs = [p.strip() for p in raw_text.strip().split("\n\n") if p.strip()]
                print(
                    f"[enricher] DEBUG: choice[{i}] had {len(paragraphs)} paragraph(s), "
                    f"all flagged as metadata. Raw content follows:\n"
                    f"--- RAW CONTENT START ---\n{raw_text}\n--- RAW CONTENT END ---"
                )
                for j, p in enumerate(paragraphs):
                    print(f"[enricher] DEBUG: choice[{i}] paragraph[{j}] = {p!r}")

    usage_raw = body.get("usage", {}) or {}
    prompt_t = usage_raw.get("prompt_tokens", 0)
    output_t = usage_raw.get("completion_tokens", 0)
    model_used = body.get("model", "openrouter/free")

    if not candidate or _looks_like_metadata(candidate):
        print(f"[enricher] discarding invalid/metadata-only response, using fallback")
        candidate = _build_fallback_summary(canonical, raw)
        metrics.record_llm_usage(0, 0)
        return candidate.strip(), {
            "prompt_tokens": 0,
            "output_tokens": 0,
            "model": "fallback-invalid-response",
        }

    metrics.record_llm_usage(prompt_t, output_t)

    return candidate.strip(), {
        "prompt_tokens": prompt_t,
        "output_tokens": output_t,
        "model": model_used,
    }


def _looks_like_metadata(paragraph: str) -> bool:
    """
    Heuristic: does this paragraph look like a safety/moderation tag, or
    the model narrating its own formatting process, rather than an actual
    content paragraph?
    """
    if _METADATA_PATTERNS.match(paragraph):
        return True
    if _META_COMMENTARY_PATTERNS.search(paragraph):
        return True
    # Real summaries are sentences; metadata tags tend to be short label:value pairs.
    if len(paragraph.split()) <= 6 and ":" in paragraph:
        return True
    return False


def _extract_candidate_text(body: dict) -> str | None:
    choices = body.get("choices") or []
    for choice in choices:
        message = choice.get("message") or {}
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            continue

        # Strip reasoning — reasoning models think out loud then write
        # the actual answer as the last paragraph.
        paragraphs = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
        if not paragraphs:
            continue

        # Walk backwards from the end, skipping metadata/safety-tag
        # paragraphs, to find the last paragraph that looks like real prose.
        for paragraph in reversed(paragraphs):
            if not _looks_like_metadata(paragraph):
                return paragraph

        # Every paragraph looked like metadata — nothing usable came back.
        return None
    return None


def _build_fallback_summary(canonical: dict, raw: dict, handle: str = "") -> str:
    name = canonical.get("display_name") or "This developer"
    location = canonical.get("location")
    bio = html.unescape(canonical.get("bio") or "")

    gh = raw.get("github", {}) or {}
    so = raw.get("stackexchange", {}) or {}
    hn = raw.get("hackernews", {}) or {}

    langs = list((gh.get("languages") or {}).keys())[:4]

    top_repos = sorted(
        [r for r in (gh.get("repos") or [])
         if r.get("stars", 0) > 0
         and r.get("description")
         and r.get("name") != handle],
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
        "Write a 2-sentence developer profile based only on the data below. "
        "Mention their primary languages and notable work. "
        "Do not invent information not present in the data.\n\n"
        f"{profile_text}"
    )