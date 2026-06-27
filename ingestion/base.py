from __future__ import annotations
import time
import httpx
from abc import ABC, abstractmethod
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from app.services.observer import metrics
from app.services.database import log_api_call


class BaseFetcher(ABC):
    source: str = ""
    base_url: str = ""

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=15.0)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        reraise=True,
    )
    async def _get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        on_response=None,
    ) -> dict:
        start = time.monotonic()
        resp  = await self.client.get(url, params=params, headers=headers)
        if on_response:
            on_response(resp)
        latency = int((time.monotonic() - start) * 1000)

        metrics.record_api_call(self.source)
        await log_api_call(self.source, url, resp.status_code, latency)

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            time.sleep(retry_after)
            resp.raise_for_status()

        resp.raise_for_status()
        return resp.json()

    @abstractmethod
    async def fetch(self, handle: str) -> dict:
        """Return a normalised dict of the profile data."""
        ...

    async def close(self):
        await self.client.aclose()