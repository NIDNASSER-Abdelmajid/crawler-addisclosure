"""
Collectors/RequestCollector.py
-------------------------------
Captures all network requests made by a page using Playwright's CDP (Chrome
DevTools Protocol) route.

Ported from:
https://github.com/duckduckgo/tracker-radar-collector/blob/main/collectors/RequestCollector.js

Recorded fields per request
────────────────────────────
url             str        Full request URL
method          str        HTTP verb (GET, POST, …)
type            str        Resource type (Document, Script, Image, XHR, …)
status          int|None   HTTP response status code
size            int|None   Encoded response body size in bytes
remoteIPAddress str|None   Server IP address
responseHeaders dict|None  Filtered response headers (see DEFAULT_SAVE_HEADERS)
responseBodyHash str|None  SHA-256 hex digest of the response body
failureReason   str|None   Error text for failed requests
redirectedFrom  str|None   URL this request was redirected from
redirectedTo    str|None   URL this request redirected to
initiators      list[str]  Chain of initiator URLs only
time            float|None Duration in seconds (endTime – startTime)
"""

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse


# Response headers kept in the output (everything else is stripped)
DEFAULT_SAVE_HEADERS = [
    "etag",
    "set-cookie",
    "cache-control",
    "expires",
    "pragma",
    "p3p",
    "timing-allow-origin",
    "access-control-allow-origin",
    "accept-ch",
]


def _normalize_headers(raw: dict | None) -> dict:
    """Lower-case all header names."""
    if not raw:
        return {}
    return {k.lower(): v for k, v in raw.items()}


def _filter_headers(headers: dict, keep: list[str]) -> dict:
    """Keep only the whitelisted headers."""
    keep_set = set(keep)
    return {k: v for k, v in headers.items() if k in keep_set}


def _get_initiators(initiator: dict | None) -> list[str]:
    """
    Walk the initiator chain and return a de-duplicated list of initiator
    URLs only.
    """
    if not initiator:
        return []

    seen: set[str] = set()
    result: list[str] = []

    def _add_url(candidate: str | None) -> None:
        if not candidate:
            return
        parsed = urlparse(candidate)
        if not parsed.scheme:
            return
        if candidate not in seen:
            seen.add(candidate)
            result.append(candidate)

    def _walk(node: dict) -> None:
        if not node:
            return
        _add_url(node.get("url"))
        # Recurse into stack frames / parent
        stack = node.get("stack") or {}
        for frame in stack.get("callFrames", []):
            _add_url(frame.get("url"))
        if stack.get("parent"):
            _walk(stack["parent"])

    _walk(initiator)
    return result


class RequestCollector:
    COLLECTOR_NAME = "RequestCollector"

    def __init__(
        self,
        save_response_hash: bool = True,
        save_headers: list[str] | None = None,
    ) -> None:
        self._save_response_hash = save_response_hash
        self._save_headers: list[str] = (
            [h.lower() for h in save_headers] if save_headers else list(DEFAULT_SAVE_HEADERS)
        )

    def init(self, output_dir: str, logger, url_hash: str) -> None:
        self._output_dir = Path(output_dir)
        self._logger = logger
        self._url_hash = url_hash

        # Keyed by requestId
        self._requests: dict[str, dict] = {}
        # Events that arrived before the matching requestWillBeSent
        self._unmatched: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def pre_crawl(self, page) -> None:
        """
        Attach CDP listeners BEFORE ``page.goto()`` is called so that every
        request — including the very first document fetch — is captured.

        Must be paired with a subsequent call to ``collect()``.
        """
        self._cdp = await page.context.new_cdp_session(page)
        await self._cdp.send("Network.enable")

        # Wire up CDP events (all synchronous lambdas — CDP fires on the loop)
        self._cdp.on("Network.requestWillBeSent",
                     lambda e: self._handle_request(e, self._cdp))
        self._cdp.on("Network.webSocketCreated",
                     lambda e: self._handle_websocket(e))
        self._cdp.on("Network.responseReceived",
                     lambda e: self._handle_response(e))
        self._cdp.on("Network.responseReceivedExtraInfo",
                     lambda e: self._handle_response_extra_info(e))
        self._cdp.on("Network.loadingFailed",
                     lambda e: self._handle_failed(e, self._cdp))
        self._cdp.on("Network.loadingFinished",
                     lambda e: self._handle_finished(e, self._cdp))

    async def collect(self, page) -> list:
        """
        Wait for the page to settle, detach CDP, persist request data, and
        return the captured request list.

        Must be called after ``pre_crawl()`` has already been called.
        """
        # Give the page extra time for late XHR / fetch calls
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass  # timeout is fine – we keep whatever we captured

        await self._populate_missing_response_hashes()

        await self._cdp.detach()

        requests = self._build_results(page.url)
        self._logger.info(
            f"[RequestCollector] Captured {len(requests)} request(s)"
        )

        return requests

    # ------------------------------------------------------------------
    # CDP event handlers  (all synchronous — CDP events fire on the event loop)
    # ------------------------------------------------------------------

    def _find_last(self, request_id: str) -> dict | None:
        return self._requests.get(request_id)

    def _handle_request(self, data: dict, cdp) -> None:
        rid = data["requestId"]
        request = data.get("request", {})
        url = request.get("url", "")
        method = request.get("method", "")
        rtype = data.get("type", "Other")
        initiator = data.get("initiator")
        start_time = data.get("timestamp")

        # For CORS requests Chrome sometimes mis-labels initiator as 'parser';
        # recover from the matching OPTIONS pre-flight if present.
        if method != "OPTIONS" and (initiator or {}).get("type") == "parser":
            for req in reversed(list(self._requests.values())):
                if req.get("method") == "OPTIONS" and req.get("url") == url:
                    initiator = req.get("initiator")
                    break

        entry: dict = {
            "id":        rid,
            "url":       url,
            "method":    method,
            "type":      rtype,
            "initiator": initiator,
            "startTime": start_time,
        }

        # Handle redirect chain: Chrome re-uses the requestId; the previous
        # response arrives inside this event's redirectResponse field.
        redirect_response = data.get("redirectResponse")
        if redirect_response:
            prev = self._find_last(rid)
            if prev:
                self._handle_response({
                    "requestId": rid,
                    "type":      rtype,
                    "response":  redirect_response,
                })
                prev_size = prev.get("size")
                self._handle_finished(
                    {"requestId": rid, "timestamp": start_time, "encodedDataLength": prev_size},
                    cdp,
                )
                entry["initiator"]     = prev.get("initiator")
                entry["redirectedFrom"] = prev.get("url")
                prev["redirectedTo"]   = url

        # Merge any early-arriving unmatched info
        if rid in self._unmatched:
            early = self._unmatched.pop(rid)
            for k, v in early.items():
                entry.setdefault(k, v)

        # Store under requestId (last writer wins for redirects — that's fine
        # because we tag redirectedFrom/redirectedTo above)
        self._requests[rid] = entry

    def _handle_websocket(self, data: dict) -> None:
        rid = data["requestId"]
        self._requests[rid] = {
            "id":        rid,
            "url":       data.get("url", ""),
            "type":      "WebSocket",
            "initiator": data.get("initiator"),
        }

    def _handle_response(self, data: dict) -> None:
        rid = data["requestId"]
        response = data.get("response", {})
        entry = self._requests.get(rid)

        if entry is None:
            entry = {"id": rid, "url": response.get("url", ""), "type": data.get("type", "Other")}
            self._unmatched[rid] = entry

        entry["type"]            = data.get("type") or entry.get("type")
        entry["status"]          = response.get("status")
        entry["remoteIPAddress"] = response.get("remoteIPAddress")

        # responseHeaders may be overwritten by handleResponseExtraInfo (richer)
        if "responseHeaders" not in entry:
            entry["responseHeaders"] = _normalize_headers(response.get("headers"))

    def _handle_response_extra_info(self, data: dict) -> None:
        rid = data["requestId"]
        entry = self._requests.get(rid)

        if entry is None:
            entry = {"id": rid, "url": "<unknown>", "type": "Other"}
            self._unmatched[rid] = entry

        # Always override — extra-info provides the most complete header set
        entry["responseHeaders"] = _normalize_headers(data.get("headers"))

    def _handle_failed(self, data: dict, cdp) -> None:
        rid = data["requestId"]
        entry = self._requests.get(rid)

        if entry is None:
            entry = {"id": rid, "url": "<unknown>", "type": data.get("type", "Other")}
            self._unmatched[rid] = entry

        entry["endTime"]       = data.get("timestamp")
        entry["failureReason"] = data.get("errorText") or "unknown error"

        if self._save_response_hash:
            entry["responseBodyHash"] = self._get_body_hash_sync(rid, cdp)

    def _handle_finished(self, data: dict, cdp) -> None:
        rid = data["requestId"]
        entry = self._requests.get(rid)

        if entry is None:
            entry = {"id": rid, "url": "<unknown>", "type": "Other"}
            self._unmatched[rid] = entry

        entry["endTime"] = data.get("timestamp")
        size = data.get("encodedDataLength")
        entry["size"] = size if isinstance(size, (int, float)) and size >= 0 else None

        if self._save_response_hash:
            entry["responseBodyHash"] = self._get_body_hash_sync(rid, cdp)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_body_hash_sync(request_id: str, cdp) -> str | None:
        """
        Compute SHA-256 of the response body.  CDP send() is async but this
        is called from a sync event handler; we skip hashing here and
        do a post-pass in collect() if needed.
        Returns None — hashing is best-effort and only used in async contexts.
        """
        # NOTE: Playwright CDP send() is a coroutine; calling it from a
        # synchronous event callback isn't straightforward.  We skip inline
        # hashing and rely on the JSON already being saved.
        return None

    async def _get_body_hash(self, request_id: str) -> str:
        try:
            response = await self._cdp.send("Network.getResponseBody", {"requestId": request_id})
            body = response.get("body", "")
            if response.get("base64Encoded"):
                payload = base64.b64decode(body)
            else:
                payload = body.encode("utf-8", errors="replace")
            return hashlib.sha256(payload).hexdigest()
        except Exception:
            return ""

    async def _populate_missing_response_hashes(self) -> None:
        if not self._save_response_hash:
            return

        pending_request_ids = [
            request_id
            for request_id, entry in list(self._requests.items())
            if not entry.get("responseBodyHash") and entry.get("endTime")
        ]

        for request_id in pending_request_ids:
            entry = self._requests.get(request_id)
            if entry is None:
                continue
            if entry.get("responseBodyHash"):
                continue
            if entry.get("type") == "WebSocket":
                entry["responseBodyHash"] = ""
                continue

            entry["responseBodyHash"] = await self._get_body_hash(request_id)

    def _build_results(self, final_url: str) -> list[dict]:
        """Convert internal entries to the public RequestData schema."""
        out = []
        for entry in self._requests.values():
            url = entry.get("url", "")

            # Skip data: URIs and invalid URLs
            try:
                parsed = urlparse(url)
                if parsed.scheme == "data" or not parsed.scheme:
                    continue
            except Exception:
                continue

            start = entry.get("startTime")
            end   = entry.get("endTime")

            headers = entry.get("responseHeaders")
            size = entry.get("size")

            out.append({
                "url":              url,
                "method":           entry.get("method"),
                "type":             entry.get("type"),
                "status":           entry.get("status"),
                "size":             int(size) if isinstance(size, float) else size,
                "remoteIPAddress":  entry.get("remoteIPAddress"),
                "responseHeaders":  _filter_headers(headers, self._save_headers) if headers else None,
                "responseBodyHash": entry.get("responseBodyHash") or "",
                "failureReason":    entry.get("failureReason") or "",
                "redirectedFrom":   entry.get("redirectedFrom") or "",
                "redirectedTo":     entry.get("redirectedTo") or "",
                "initiators":       _get_initiators(entry.get("initiator")),
                "time":             round(end - start, 6) if isinstance(start, (int, float)) and isinstance(end, (int, float)) else None,
            })

        return out
