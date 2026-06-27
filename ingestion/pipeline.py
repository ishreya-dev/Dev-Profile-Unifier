from __future__ import annotations
import asyncio
from ingestion.providers import PROVIDER_REGISTRY
from app.services.database import insert_raw_source


async def run_pipeline(handles: dict[str, str | None]) -> dict[str, dict]:
    """
    handles: {"github": "torvalds", "stackexchange": "12345", ...}
    Returns: {"github": {...raw_data...}, ...}  (only sources with a handle)
    """
    tasks = {}
    fetchers = {}

    for source, handle in handles.items():
        if not handle or source not in PROVIDER_REGISTRY:
            continue
        fetcher = PROVIDER_REGISTRY[source]["fetcher"]()
        fetchers[source] = fetcher
        tasks[source] = fetcher.fetch(handle)

    results_list = await asyncio.gather(*tasks.values(), return_exceptions=True)
    raw: dict[str, dict] = {}

    for source, result in zip(tasks.keys(), results_list):
        if isinstance(result, Exception):
            raw[source] = {"error": str(result), "handle": handles[source]}
        else:
            raw[source] = result
            raw_id = await insert_raw_source(
                source=source,
                handle=handles[source],
                payload=result,
                meta={"status": "ok"},
            )
            result["_raw_source_id"] = raw_id

    for f in fetchers.values():
        await f.close()

    return raw
