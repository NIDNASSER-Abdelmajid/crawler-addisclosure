"""Collect fingerprinting-related API calls made by page scripts."""

from __future__ import annotations

from urllib.parse import urlparse

from Helpers.fingerprint_detection import fingerprint_detection_script


class FingerprintCollector:
    COLLECTOR_NAME = "FingerprintCollector"
    BINDING_NAME = "calledAPIEvent"

    def init(self, output_dir: str, logger, url_hash: str) -> None:
        self._output_dir = output_dir
        self._logger = logger
        self._url_hash = url_hash
        self._stats: dict[str, dict[str, int]] = {}
        self._calls: list[dict] = []
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
            if not (api_call and api_call.get("source") and api_call.get("description")):
                self._logger.debug(f"[{self.COLLECTOR_NAME}] Missing call details: {api_call}")
                return

            source = api_call["source"]
            description = api_call["description"]
            source_stats = self._stats.setdefault(source, {})
            source_stats[description] = source_stats.get(description, 0) + 1

            self._calls.append(
                {
                    "source": source,
                    "description": description,
                    "arguments": api_call.get("args"),
                    "returnValue": api_call.get("retVal"),
                    "accessType": api_call.get("accessType"),
                }
            )

        await page.expose_function(self.BINDING_NAME, _called_api_event)
        await page.add_init_script(fingerprint_detection_script(self.BINDING_NAME))
        self._ready = True

    def _is_acceptable_url(self, url_string: str) -> bool:
        try:
            parsed = urlparse(url_string)
        except Exception:
            return False

        if parsed.scheme == "data":
            return False

        return bool(parsed.scheme and parsed.netloc)

    async def collect(self, page) -> dict:
        if not self._ready:
            self._logger.warning(f"[{self.COLLECTOR_NAME}] pre_crawl was not called; skipping")
            return {"callStats": {}, "savedCalls": []}

        try:
            await page.wait_for_timeout(2000)
        except Exception:
            pass

        self._closed = True

        call_stats = {
            source: stats
            for source, stats in self._stats.items()
            if self._is_acceptable_url(source)
        }
        saved_calls = [call for call in self._calls if self._is_acceptable_url(call["source"])]

        self._logger.info(
            f"[{self.COLLECTOR_NAME}] Recorded {len(saved_calls)} fingerprinting event(s) "
            f"from {len(call_stats)} source(s)"
        )
        return {"callStats": call_stats, "savedCalls": saved_calls}