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

# Matches reasoning/meta-commentary leaking into output
_META_COMMENTARY_PATTERNS = re.compile(
    r"""(
        make\s+sure\s+(exactly|it'?s|to)|
        exactly\s+\d+\s+sentence|
        no\s+extra\s+punctuation|
        that'?s\s+one\s+sentence|
        let\s+me\s+(think|check|count|make\s+sure)|
        i\s+(should|need\s+to|will)\s+(write|make\s+sure|keep|count)|
        as\s+(an?\s+)?(ai|language\s+model|assistant)|
        # Reasoning-model patterns: thinking out loud mid-sentence
        this\s+creates\s+a\s+conflict|
        we\s+(must|cannot|need\s+to|have\s+no)|
        the\s+instruction\s*:|
        perhaps\s+we\s+(must|can|should)|
        is\s+that\s+acceptable\??|
        could\s+be\s+considered|
        we\s+risk\s+violating|
        saying\s+they\s+are\s+not\s+specified
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Detects lines that look like reasoning steps rather than prose
# e.g. "So we can produce:", "But that would not be..."
_REASONING_STEP_PATTERNS = re.compile(
    r"""^(
        so\s+we\s+(can|cannot|must|should)|
        but\s+that\s+(would|could|is)|
        however\s+we\s+must|
        if\s+we\s+say|
        maybe\s+we\s+can|
        it\s+is\s+not\s+inventing|
        \(.*\)\s*$
    )""",
    re.IGNORECASE | re.VERBOSE,
)


async def generate_summary(canonical: dict, raw: dict) -> tuple[str, dict]:
    print(f"[enricher] generate_summary called for: {canonical.get('display_name')}")
    prompt = _build_prompt(canonical, raw)
    api_key = os.environ.get("OPENROUTER_API_KEY")
    print(f"[enricher] API key found: {'Yes' if api_key else 'NO KEY'}")

    if not api_key:
        fallback = _build_fallback_summary(canonical, raw)
        metrics.record_llm_usage(0, 0)
        return fallback, {"prompt_tokens": 0, "output_tokens": 0, "model": "fallback-no-key"}

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
                                "You write short developer bios. "
                                "Reply with ONLY the bio itself — "
                                "no explanation, no reasoning, no notes, "
                                "no thinking out loud, no quotation marks. "
                                "Write exactly 2 sentences, then stop immediately.\n\n"
                                "Example (copy this format exactly):\n"
                                "Jane Doe is a backend engineer known for creating "
                                "fastqueue, a popular Go job-queue library. "
                                "She has 12,000 GitHub followers and is active in "
                                "distributed systems and databases."
                            ),
                        },
                        {
                            "role": "user",
                            "content": prompt,
                        },
                    ],
                    # FIX: was 300 — enough for reasoning leak to fill the budget.
                    # 120 tokens is plenty for 2 clean sentences and forces the
                    # model to be concise rather than reason at length.
                    "max_tokens": 120,
                },
            )
            resp.raise_for_status()
            body = resp.json()

    except Exception as exc:
        print(f"[enricher] OpenRouter call failed: {exc}")
        fallback = _build_fallback_summary(canonical, raw)
        metrics.record_llm_usage(0, 0)
        return fallback, {"prompt_tokens": 0, "output_tokens": 0, "model": "fallback-error"}

    candidate = _extract_candidate_text(body)
    print(f"[enricher] raw candidate: {candidate[:120] if candidate else 'EMPTY'}")

    if not candidate:
        choices = body.get("choices") or []
        if not choices:
            print("[enricher] DEBUG: response had no 'choices' at all")
        else:
            for i, choice in enumerate(choices):
                raw_text = (choice.get("message") or {}).get("content")
                print(
                    f"[enricher] DEBUG: choice[{i}] raw content:\n"
                    f"--- START ---\n{raw_text}\n--- END ---"
                )

    usage_raw = body.get("usage", {}) or {}
    prompt_t = usage_raw.get("prompt_tokens", 0)
    output_t = usage_raw.get("completion_tokens", 0)
    model_used = body.get("model", "openrouter/free")

    if not candidate or _looks_like_metadata(candidate):
        print("[enricher] discarding invalid response, using fallback")
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
    if _METADATA_PATTERNS.match(paragraph):
        return True
    if _META_COMMENTARY_PATTERNS.search(paragraph):
        return True
    if _REASONING_STEP_PATTERNS.match(paragraph):
        return True
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

        # FIX: some free models output reasoning as single-newline-separated
        # lines rather than double-newline paragraphs. Split on both and
        # collect all non-reasoning lines, then take the last clean sentence(s).
        lines = [ln.strip() for ln in re.split(r"\n+", text.strip()) if ln.strip()]

        # Filter out lines that are clearly reasoning/meta-commentary
        clean_lines = [
            ln for ln in lines
            if not _looks_like_metadata(ln) and not _REASONING_STEP_PATTERNS.match(ln)
        ]

        if clean_lines:
            # Take the last 2 clean lines (the actual bio sentences)
            candidate = " ".join(clean_lines[-2:])
            if len(candidate.split()) >= 5:  # must be real prose, not a fragment
                return candidate

        # Fallback: try paragraph-level walk (original logic)
        paragraphs = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
        for paragraph in reversed(paragraphs):
            if not _looks_like_metadata(paragraph):
                return paragraph

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
        extras.append(f"Notable repositories include {' and '.join(repo_snippets)}.")
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
    gh = raw.get("github", {}) or {}
    so = raw.get("stackexchange", {}) or {}
    dev = raw.get("devto", {}) or {}
    hn = raw.get("hackernews", {}) or {}

    parts = [f"Name: {canonical.get('display_name', 'Unknown')}"]

    if canonical.get("location"):
        parts.append(f"Location: {canonical['location']}")
    if canonical.get("bio"):
        parts.append(f"Bio: {canonical['bio']}")

    if gh.get("languages"):
        parts.append(f"GitHub languages: {', '.join(gh['languages'].keys())}")
    if gh.get("public_repos"):
        parts.append(
            f"Public repos: {gh['public_repos']}, Followers: {gh.get('followers')}"
        )

    # Top repos by stars — gives the LLM real "notable work" to mention
    top_repos = sorted(
        [r for r in (gh.get("repos") or []) if r.get("stars", 0) > 0 and r.get("description")],
        key=lambda r: -r.get("stars", 0),
    )[:3]
    if top_repos:
        repo_lines = [
            f"  - {r['name']} ({r['stars']:,}★): {r['description']}"
            for r in top_repos
        ]
        parts.append("Top GitHub repos:\n" + "\n".join(repo_lines))

    if so.get("top_tags"):
        parts.append(
            f"Stack Overflow: tags={', '.join(so['top_tags'][:5])}, "
            f"reputation={so.get('reputation')}"
        )
    if dev.get("top_tags"):
        parts.append(
            f"dev.to: tags={', '.join(list(dev['top_tags'].keys())[:5])}, "
            f"articles={dev.get('article_count')}"
        )
    if hn.get("karma"):
        parts.append(f"Hacker News karma: {hn['karma']}")

    profile_text = "\n".join(parts)

    # FIX: removed "Mention their primary languages and notable work" —
    # that instruction caused reasoning loops when data was sparse.
    # The system prompt already defines the format; the user prompt
    # just supplies the data and a single clear instruction.
    return (
        "Write a 2-sentence developer bio using only the facts below. "
        "Do not invent anything not listed.\n\n"
        f"{profile_text}"
    )