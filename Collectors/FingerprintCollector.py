"""Collect fingerprinting-related API calls made by page scripts."""

from __future__ import annotations

import time
from urllib.parse import urlparse
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from Helpers.crawl_context import CrawlContext

from Helpers.fingerprint_detection import fingerprint_detection_script


class FingerprintCollector:
    COLLECTOR_NAME = "FingerprintCollector"
    BINDING_NAME = "calledAPIEvent"

    def init(self, output_dir: str, logger, url_hash: str, crawl_context: CrawlContext | None = None) -> None:
        self._output_dir = output_dir
        self._logger = logger
        self._url_hash = url_hash
        self._crawl_context = crawl_context
        self._stats: dict[str, dict[str, int]] = {}
        self._calls: list[dict] = []
        self._total_observed_calls = 0
        self._truncated_calls_count = 0
        self._frame_url_to_id: dict[str, str] = {}
        self._ready = False
        self._closed = False

    async def pre_crawl(self, page) -> None:
        self._closed = False

        def _mark_closed(*_) -> None:
            self._closed = True

        page.on("close", _mark_closed)

        def _called_api_event(api_call: dict) -> None:
            if self._closed:
                return
            if not (api_call and api_call.get("description")):
                self._logger.debug(f"[{self.COLLECTOR_NAME}] Missing call description: {api_call}")
                return

            source = api_call.get("source") or "inline_or_unknown"
            description = api_call["description"]

            self.record_fingerprint_call(api_call)

        await page.expose_function(self.BINDING_NAME, _called_api_event)
        await page.add_init_script(fingerprint_detection_script(self.BINDING_NAME))
        self._ready = True

    def record_fingerprint_call(self, api_call: dict[str, Any]) -> dict[str, Any]:
        """Record a fingerprint API call event."""
        source = api_call.get("source") or "<unknown>"
        description = api_call.get("description")
        if not description:
            return {}

        source_stats = self._stats.setdefault(source, {})
        total_observed_count = api_call.get("total_observed_count")
        if total_observed_count is not None and isinstance(total_observed_count, int):
            self._total_observed_calls = max(self._total_observed_calls, total_observed_count)
            source_stats[description] = max(source_stats.get(description, 0), total_observed_count)
        else:
            self._total_observed_calls += 1
            source_stats[description] = source_stats.get(description, 0) + 1

        capture_status = api_call.get("capture_status", "captured")
        if capture_status == "truncated":
            if total_observed_count is not None and isinstance(total_observed_count, int):
                self._truncated_calls_count = max(self._truncated_calls_count, max(1, total_observed_count - 100))
            else:
                self._truncated_calls_count += 1

        ts_ms = api_call.get("timestamp_ms") or int(time.time() * 1000)
        frame_url = api_call.get("frameUrl")
        frame_id = api_call.get("frame_id") or (self._frame_url_to_id.get(frame_url) if frame_url else None)

        overlaps = False
        desc_lower = description.lower()
        if any(term in desc_lower for term in ("localstorage", "sessionstorage", "indexeddb", "cookie")):
            overlaps = True

        entry: dict[str, Any] = {
            "api_name": description,
            "description": description,
            "access_type": api_call.get("accessType", "call"),
            "source_script": source,
            "source": source,
            "frame_url": frame_url,
            "frame_id": frame_id,
            "execution_context_id": api_call.get("execution_context_id"),
            "call_stack": api_call.get("stack"),
            "arguments": api_call.get("args"),
            "return_value": api_call.get("retVal"),
            "capture_status": capture_status,
            "arguments_captured_status": "captured" if api_call.get("args") is not None else ("truncated" if capture_status == "truncated" else "not_requested"),
            "return_value_captured_status": "captured" if api_call.get("retVal") is not None else ("truncated" if capture_status == "truncated" else "not_requested"),
            "total_observed_count": api_call.get("total_observed_count", 1),
            "overlaps_with_api_collector": overlaps,
        }

        if self._crawl_context:
            self._crawl_context.enrich_event(entry, timestamp_ms=ts_ms)
        else:
            entry["timestamp_ms"] = ts_ms

        # Deduplicate multiple truncated calls per API to avoid memory bloat
        trunc_key = f"{source}_{description}__truncated_recorded"
        if capture_status == "truncated":
            if getattr(self, "_recorded_truncations", None) is None:
                self._recorded_truncations = set()
            if trunc_key in self._recorded_truncations:
                return entry
            self._recorded_truncations.add(trunc_key)

        self._calls.append(entry)
        return entry

    # Alias for testing
    _handle_console_message = record_fingerprint_call


    def _is_acceptable_url(self, url_string: str) -> bool:
        """Allow inline, unknown, and valid web URLs; drop massive data: URIs."""
        if not url_string:
            return True
        if url_string in {"inline_or_unknown", "<unknown>"} or url_string.startswith("inline"):
            return True
        try:
            parsed = urlparse(url_string)
            if parsed.scheme == "data":
                return False
            return True
        except Exception:
            return True

    def get_partial_results(self) -> dict:
        """Synchronously return captured fingerprinting events."""
        call_stats = {
            source: stats
            for source, stats in self._stats.items()
            if self._is_acceptable_url(source)
        }
        saved_calls = [
            call for call in self._calls
            if self._is_acceptable_url(call.get("source_script") or call.get("source", ""))
        ]
        return {
            "callStats": call_stats,
            "savedCalls": saved_calls,
            "totalObservedCalls": self._total_observed_calls,
            "totalObservedCount": self._total_observed_calls,
            "truncatedCallsCount": self._truncated_calls_count,
            "truncatedCount": self._truncated_calls_count,
            "hasTruncatedCalls": self._truncated_calls_count > 0,
        }

    def get_results(self) -> dict:
        return self.get_partial_results()


    async def collect(self, page) -> dict:
        if not self._ready:
            self._logger.warning(f"[{self.COLLECTOR_NAME}] pre_crawl was not called; skipping")
            return self.get_partial_results()

        try:
            # Map page frames if available
            for fr in getattr(page, "frames", []):
                fu = getattr(fr, "url", "")
                # Playwright doesn't expose cdp frameId on Frame directly, but URL can map
                if fu:
                    self._frame_url_to_id[fu] = getattr(fr, "name", "")
            await page.wait_for_timeout(2000)
        except Exception:
            pass

        self._closed = True
        results = self.get_partial_results()
        self._logger.info(
            f"[{self.COLLECTOR_NAME}] Recorded {len(results['savedCalls'])} fingerprinting event(s) "
            f"(observed: {results['totalObservedCalls']}, truncated: {results['truncatedCallsCount']}) "
            f"from {len(results['callStats'])} source(s)"
        )
        return results