from __future__ import annotations
import time
from collections import defaultdict
from threading import Lock

_COST_PER_1M_INPUT  = 0.075
_COST_PER_1M_OUTPUT = 0.30


class Metrics:
    def __init__(self):
        self._lock     = Lock()
        self._start_ts = time.time()
        self.call_counts: dict[str, int] = defaultdict(int)
        self.github_rate_limit: dict    = {}
        self.llm_stats = {
            "calls": 0, "total_tokens": 0,
            "prompt_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0,
        }
        self._resolution_times: list[int] = []

    def start_timer(self):
        self._start_ts = time.time()

    def uptime_seconds(self) -> float:
        return round(time.time() - self._start_ts, 1)

    def record_api_call(self, source: str) -> None:
        with self._lock:
            self.call_counts[source] += 1

    def update_github_rate_limit(self, remaining: int, limit: int, reset_utc: str) -> None:
        with self._lock:
            self.github_rate_limit = {
                "remaining": remaining,
                "limit":     limit,
                "reset_utc": reset_utc,
            }

    def record_llm_usage(self, prompt_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.llm_stats["calls"]         += 1
            self.llm_stats["prompt_tokens"] += prompt_tokens
            self.llm_stats["output_tokens"] += output_tokens
            self.llm_stats["total_tokens"]  += prompt_tokens + output_tokens
            cost = (prompt_tokens  / 1_000_000 * _COST_PER_1M_INPUT +
                    output_tokens  / 1_000_000 * _COST_PER_1M_OUTPUT)
            self.llm_stats["est_cost_usd"]  += cost

    def record_resolution_time(self, elapsed_ms: int) -> None:
        with self._lock:
            self._resolution_times.append(elapsed_ms)


metrics = Metrics()
