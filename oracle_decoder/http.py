"""Small HTTP client: retries with backoff, a polite minimum interval, JSON
responses. Used for the publisher registries and Sourcify."""

from __future__ import annotations

import time

import httpx


class Http:
    def __init__(self, min_interval_s: float = 0.25, timeout_s: float = 30.0, retries: int = 4) -> None:
        self._client = httpx.Client(timeout=timeout_s, headers={"User-Agent": "oracle-decoder"})
        self._min_interval = min_interval_s
        self._retries = retries
        self._last = 0.0

    def get_json(self, url: str, params: dict | None = None, accept: frozenset[int] = frozenset()) -> dict | list:
        """GET and decode JSON. Retries on transport errors and 5xx/429.
        Statuses in `accept` (e.g. 404) return the decoded body instead of
        raising, so a definitive "not found" is distinguishable from an
        outage."""
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self._client.get(url, params=params)
                self._last = time.monotonic()
                if resp.status_code in accept:
                    return resp.json()
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(f"{resp.status_code}", request=resp.request, response=resp)
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last_error = e
                time.sleep(min(30.0, 1.5 * 2**attempt))
        raise RuntimeError(f"GET {url} failed after retries") from last_error

    def close(self) -> None:
        self._client.close()
