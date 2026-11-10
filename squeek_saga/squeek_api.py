"""Display-only client for the public Squeekchain API (https://api.squeekchain.com/v1).

Nothing read here ever decides the story: chapters, votes and raid damage come
from SqueekCore on chain. This only feeds "what is the wheel doing right now"
widgets, and treats ``"status": "unavailable"`` as unknown, never as zero.

Polite by construction: bodyless GETs, the endpoint's ETag sent back in
If-None-Match, a minimum interval per endpoint, and Retry-After honoured.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import SQUEEK_API_BASE


@dataclass(frozen=True)
class ApiReading:
    known: bool  # False: unknown (unavailable, error, or never fetched)
    data: dict[str, Any] | None
    reason: str = ""
    fetched_at: float = 0.0


@dataclass
class _Entry:
    etag: str | None = None
    reading: ApiReading = ApiReading(False, None, "not fetched yet")
    next_allowed: float = 0.0


@dataclass
class SqueekApi:
    client: httpx.AsyncClient
    base: str = SQUEEK_API_BASE
    _cache: dict[str, _Entry] = field(default_factory=dict)

    async def get(self, path: str, *, min_interval: float = 5.0) -> ApiReading:
        entry = self._cache.setdefault(path, _Entry())
        current = time.monotonic()
        if current < entry.next_allowed:
            return entry.reading
        entry.next_allowed = current + min_interval
        headers = {"Accept": "application/json", "User-Agent": "squeek-saga (community bot)"}
        if entry.etag:
            headers["If-None-Match"] = entry.etag
        try:
            response = await self.client.get(f"{self.base}{path}", headers=headers, timeout=10.0)
        except httpx.HTTPError as exc:
            entry.reading = ApiReading(False, None, f"network error: {type(exc).__name__}", time.time())
            return entry.reading

        if response.status_code == 304:
            return entry.reading
        if response.status_code in (429, 503):
            retry_after = response.headers.get("Retry-After", "5")
            wait = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 5.0
            entry.next_allowed = current + max(wait, min_interval)
            entry.reading = ApiReading(False, None, f"api busy (HTTP {response.status_code})", time.time())
            return entry.reading
        if response.status_code != 200:
            entry.reading = ApiReading(False, None, f"HTTP {response.status_code}", time.time())
            return entry.reading
        try:
            body = response.json()
        except ValueError:
            # A response that does not parse is an error, not empty data.
            entry.reading = ApiReading(False, None, "unparseable response", time.time())
            return entry.reading
        if not isinstance(body, dict):
            entry.reading = ApiReading(False, None, "unexpected response shape", time.time())
            return entry.reading
        entry.etag = response.headers.get("ETag")
        if body.get("status") == "unavailable":
            entry.reading = ApiReading(False, None, str(body.get("reason", "unavailable")), time.time())
        else:
            entry.reading = ApiReading(True, body, "", time.time())
        return entry.reading

    async def wheel_now(self) -> dict[str, Any]:
        """A small, display-only summary of /v1/status. Unknown fields stay None."""
        reading = await self.get("/status", min_interval=5.0)
        summary: dict[str, Any] = {
            "known": False,
            "reason": reading.reason,
            "freshness": None,
            "coreStatus": None,
            "rpmMilli": None,
            "speedMmPerSecond": None,
            "openRoundWorkUnits": None,
            "observedAt": None,
        }
        if not reading.known or reading.data is None:
            return summary
        measurement = reading.data.get("measurement")
        if not isinstance(measurement, dict):
            summary["reason"] = "no measurement yet"
            return summary
        summary.update(
            known=True,
            reason="",
            freshness=measurement.get("freshness"),
            coreStatus=measurement.get("coreStatus"),
            rpmMilli=measurement.get("rpmMilli") if isinstance(measurement.get("rpmMilli"), int) else None,
            speedMmPerSecond=(
                measurement.get("speedMmPerSecond")
                if isinstance(measurement.get("speedMmPerSecond"), int) else None
            ),
            observedAt=measurement.get("observedAt"),
        )
        builder = reading.data.get("builder")
        if isinstance(builder, dict) and builder.get("status") == "available":
            queued = builder.get("queued")
            if isinstance(queued, dict) and isinstance(queued.get("openRoundWorkUnits"), str):
                # Decimal string: parse as an integer, never as a float.
                try:
                    summary["openRoundWorkUnits"] = str(int(queued["openRoundWorkUnits"]))
                except ValueError:
                    pass
        return summary
