"""Real-time network traffic monitor for page visits.

Tracks:
- Total request count (deduplicated by requestId to avoid double-counting).
- Total downloaded bytes via CDP Network.loadingFinished encodedDataLength
  (this is the compressed wire size; actual decompressed content may differ).

Includes: redirects, iframes, scripts, images, fonts, ads, API calls,
and other subresources.  All network requests from the page are counted
regardless of resource type.

Measurement notes:
- Cached resources that produce Network.requestWillBeSent events are
  counted toward the request total.
- Aborted requests are counted toward the request total.
- Redirected responses: the redirect itself is one request; the follow-up
  is a second request with the same requestId (Chrome re-uses IDs).  We
  deduplicate by requestId so a redirect chain counts as one request entry,
  but each loadingFinished event adds bytes separately.
"""

from __future__ import annotations

import asyncio
from typing import Callable


class TrafficMonitor:
    """Tracks request counts and downloaded bytes for a single page visit.

    Attach to CDP events before navigation.  Call ``check_thresholds()``
    periodically or after each event to detect limit breaches.
    """

    def __init__(
        self,
        max_requests: int | None = None,
        max_bytes: int | None = None,
        on_threshold_exceeded: Callable[[str, int, int | None], None] | None = None,
    ) -> None:
        self._max_requests = max_requests
        self._max_bytes = max_bytes
        self._on_threshold_exceeded = on_threshold_exceeded

        self._request_ids: set[str] = set()
        self._total_bytes: int = 0
        self._exceeded = False
        self._exceeded_reason: str = ""
        self._lock = asyncio.Lock()

    @property
    def request_count(self) -> int:
        return len(self._request_ids)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def exceeded(self) -> bool:
        return self._exceeded

    @property
    def exceeded_reason(self) -> str:
        return self._exceeded_reason

    def handle_request(self, data: dict) -> None:
        """Called on Network.requestWillBeSent CDP events."""
        rid = data.get("requestId", "")
        if rid:
            self._request_ids.add(rid)
        self._check_thresholds_sync()

    def handle_finished(self, data: dict) -> None:
        """Called on Network.loadingFinished CDP events."""
        size = data.get("encodedDataLength")
        if isinstance(size, (int, float)) and size > 0:
            self._total_bytes += int(size)
        self._check_thresholds_sync()

    def _check_thresholds_sync(self) -> None:
        """Check if any threshold has been exceeded."""
        if self._exceeded:
            return

        if self._max_requests is not None and self.request_count > self._max_requests:
            self._exceeded = True
            self._exceeded_reason = (
                f"Request count {self.request_count} exceeds limit {self._max_requests}"
            )
            if self._on_threshold_exceeded:
                self._on_threshold_exceeded("requests", self.request_count, self._max_requests)

        if self._max_bytes is not None and self._total_bytes > self._max_bytes:
            self._exceeded = True
            self._exceeded_reason = (
                f"Downloaded bytes {self._total_bytes} exceeds limit {self._max_bytes}"
            )
            if self._on_threshold_exceeded:
                self._on_threshold_exceeded("bytes", self._total_bytes, self._max_bytes)

    def snapshot(self) -> dict:
        """Return current metrics for audit logging."""
        return {
            "request_count": self.request_count,
            "total_bytes": self._total_bytes,
            "exceeded": self._exceeded,
            "exceeded_reason": self._exceeded_reason,
        }
