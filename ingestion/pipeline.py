from __future__ import annotations
import asyncio
from ingestion.providers import PROVIDER_REGISTRY
from app.services.database import insert_raw_source
from app.services.observer import metrics


async def run_pipeline(handles: dict[str, str | None]) -> dict[str, dict]:
    """
    handles: {"github": "torvalds", "stackexchange": "12345", ...}
    Returns: {"github": {...raw_data...}, ...}  (only sources with a handle)

    All provider fetches run concurrently via asyncio.gather().
    Raw source inserts also run concurrently after fetches complete.
    """
    tasks: dict[str, object] = {}
    fetchers: dict[str, object] = {}

    for source, handle in handles.items():
        if not handle or source not in PROVIDER_REGISTRY:
            continue
        fetcher = PROVIDER_REGISTRY[source]["fetcher"]()
        fetchers[source] = fetcher
        tasks[source] = fetcher.fetch(handle)

    # --- Concurrent fetch of all providers ---
    results_list = await asyncio.gather(*tasks.values(), return_exceptions=True)

    raw: dict[str, dict] = {}
    successful_inserts: list[tuple[str, str, dict]] = []  # (source, handle, result)

    for source, result in zip(tasks.keys(), results_list):
        metrics.record_api_call(source)  # count every attempt (success or failure)
        if isinstance(result, Exception):
            raw[source] = {"error": str(result), "handle": handles[source]}
        else:
            raw[source] = result
            successful_inserts.append((source, handles[source], result))

    # --- Concurrent DB inserts for successful fetches ---
    if successful_inserts:
        insert_tasks = [
            insert_raw_source(
                source=source,
                handle=handle,
                payload=result,
                meta={"status": "ok"},
            )
            for source, handle, result in successful_inserts
        ]
        raw_ids = await asyncio.gather(*insert_tasks, return_exceptions=True)

        for (source, _, result), raw_id in zip(successful_inserts, raw_ids):
            if not isinstance(raw_id, Exception):
                result["_raw_source_id"] = raw_id

    for f in fetchers.values():
        await f.close()

    return raw