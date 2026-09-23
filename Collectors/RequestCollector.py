"""Collectors/RequestCollector.py
-------------------------------
Captures all network requests made by a page using Playwright's CDP route,
preserving redirect hops, provenance identifiers (requestId, frameId, loaderId,
initiator stack/script IDs), and privacy-safe keyed-HMAC metadata for bodies
and cookies.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from Helpers.sanitization import sanitize_headers

if TYPE_CHECKING:
    from Helpers.crawl_context import CrawlContext


# Response headers kept in the output. Note: set-cookie is removed for privacy compliance.
DEFAULT_SAVE_HEADERS = [
    "etag",
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
    return {str(k).lower(): v for k, v in raw.items()}


def _filter_headers(headers: dict, keep: list[str]) -> dict:
    """Keep only whitelisted headers, strictly omitting raw set-cookie values."""
    keep_set = set(keep)
    # Explicitly ensure set-cookie is never retained raw
    keep_set.discard("set-cookie")
    keep_set.discard("cookie")
    return {k: v for k, v in headers.items() if k in keep_set}


def _get_initiator_info(initiator: dict | None) -> tuple[str, list[str], dict | None, list[str]]:
    """Walk initiator and return (initiator_type, initiator_urls, initiator_stack, initiating_script_ids)."""
    if not initiator:
        return "other", [], None, []

    itype = str(initiator.get("type") or "other")
    seen_urls: set[str] = set()
    urls: list[str] = []
    script_ids: set[str] = set()

    def _add_url(candidate: str | None) -> None:
        if not candidate:
            return
        parsed = urlparse(candidate)
        if not parsed.scheme:
            return
        if candidate not in seen_urls:
            seen_urls.add(candidate)
            urls.append(candidate)

    def _walk(node: dict) -> None:
        if not node:
            return
        _add_url(node.get("url"))
        stack = node.get("stack") or {}
        for frame in stack.get("callFrames", []):
            _add_url(frame.get("url"))
            sid = frame.get("scriptId")
            if sid is not None:
                script_ids.add(str(sid))
        if stack.get("parent"):
            _walk(stack["parent"])

    _walk(initiator)
    return itype, urls, initiator.get("stack"), sorted(script_ids)


def _process_post_data(
    post_data: str | None,
    content_type: str | None,
    crawl_context: CrawlContext | None,
) -> dict[str, Any] | None:
    """Extract privacy-safe parameter names and keyed-HMAC values without leaking raw tokens."""
    if not post_data:
        return None

    byte_len = len(post_data.encode("utf-8", errors="replace"))
    param_names: list[str] = []
    param_hmacs: dict[str, Any] = {}

    ct = (content_type or "").lower()
    if "application/x-www-form-urlencoded" in ct or ("=" in post_data and "&" in post_data):
        try:
            parsed = parse_qs(post_data, keep_blank_values=True)
            param_names = sorted(parsed.keys())
            if crawl_context:
                for k, vals in parsed.items():
                    if len(vals) == 1:
                        param_hmacs[k] = crawl_context.hmac_value(vals[0])
                    else:
                        param_hmacs[k] = [crawl_context.hmac_value(v) for v in vals]
        except Exception:
            pass
    elif "application/json" in ct or post_data.strip().startswith("{"):
        try:
            parsed_json = json.loads(post_data)
            if isinstance(parsed_json, dict):
                param_names = sorted(parsed_json.keys())
                if crawl_context:
                    for k, v in parsed_json.items():
                        param_hmacs[k] = crawl_context.hmac_value(str(v))
        except Exception:
            pass

    return {
        "contentType": content_type or "unknown",
        "byteLength": byte_len,
        "parameterNames": param_names,
        "parameterHmacs": param_hmacs,
    }


def _get_registered_domain(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        parts = host.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return host
    except Exception:
        return ""


def _sanitize_url_tokens(url: str) -> str:
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        qs = parse_qs(parsed.query, keep_blank_values=True)
        sanitized_qs = []
        for k, vals in qs.items():
            k_lower = k.lower()
            if any(s in k_lower for s in ("token", "auth", "session", "key", "secret", "cookie")):
                sanitized_qs.append(f"{k}=[REDACTED]")
            else:
                for v in vals:
                    sanitized_qs.append(f"{k}={v}")
        new_query = "&".join(sanitized_qs)
        return parsed._replace(query=new_query).geturl()
    except Exception:
        return url


class RequestCollector:
    COLLECTOR_NAME = "RequestCollector"

    def __init__(
        self,
        save_response_hash: bool = True,
        save_headers: list[str] | None = None,
    ) -> None:
        self._save_response_hash = save_response_hash
        self._save_headers: list[str] = (
            [h.lower() for h in save_headers if h.lower() != "set-cookie"]
            if save_headers
            else list(DEFAULT_SAVE_HEADERS)
        )

    def init(
        self,
        output_dir: str,
        logger,
        url_hash: str,
        crawl_context: CrawlContext | None = None,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._logger = logger
        self._url_hash = url_hash
        self._crawl_context = crawl_context

        # Preserves all requests, including every redirect hop
        self._request_list: list[dict] = []
        self._requests: dict[str, dict] = {}
        # Maps active requestId -> current in-flight request entry
        self._active_requests: dict[str, dict] = {}
        # Unmatched events arriving before requestWillBeSent
        self._unmatched: dict[str, dict] = {}

    async def pre_crawl(self, page) -> None:
        self._cdp = await page.context.new_cdp_session(page)
        await self._cdp.send("Network.enable")

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
        await self._populate_missing_response_hashes()
        try:
            await self._cdp.detach()
        except Exception:
            pass

        requests = self._build_results(page.url)
        self._logger.info(
            f"[RequestCollector] Captured {len(requests)} request(s) (including all redirect hops)"
        )
        return requests

    def _handle_request(self, data: dict, cdp) -> None:
        rid = str(data["requestId"])
        request = data.get("request", {})
        url = request.get("url", "")
        method = request.get("method", "")
        rtype = data.get("type", "Other")
        initiator = data.get("initiator")
        start_time = data.get("timestamp")
        wall_time = data.get("wallTime")
        frame_id = data.get("frameId")
        loader_id = data.get("loaderId")

        # For CORS requests, recover initiator if needed
        if method != "OPTIONS" and (initiator or {}).get("type") == "parser":
            for prev_req in reversed(self._request_list):
                if prev_req.get("method") == "OPTIONS" and prev_req.get("url") == url:
                    initiator = prev_req.get("initiator")
                    break

        itype, init_urls, init_stack, script_ids = _get_initiator_info(initiator)

        req_headers = _normalize_headers(request.get("headers"))
        ct = req_headers.get("content-type")
        post_data = request.get("postData")
        body_meta = _process_post_data(post_data, ct, self._crawl_context)

        hop_idx = 0
        redirected_from = ""

        # Handle redirect chain: Chrome re-uses the requestId
        redirect_response = data.get("redirectResponse")
        if redirect_response:
            prev_hop = self._active_requests.get(rid)
            if prev_hop:
                # Finalize previous redirect hop without overwriting it
                prev_hop["redirectedTo"] = url
                prev_hop["status"] = redirect_response.get("status")
                prev_hop["remoteIPAddress"] = redirect_response.get("remoteIPAddress")
                prev_hop["responseHeaders"] = _normalize_headers(redirect_response.get("headers"))
                prev_hop["endTime"] = start_time
                hop_idx = prev_hop.get("redirectHopIndex", 0) + 1
                redirected_from = prev_hop.get("url", "")

        # Sanitize request headers: never keep raw Cookie, Set-Cookie, or Authorization
        sanitized_req_headers = {
            k: v for k, v in req_headers.items()
            if k.lower() not in {"cookie", "authorization", "set-cookie"}
        }

        entry: dict[str, Any] = {
            "requestId":           rid,
            "id":                  rid,
            "frameId":             frame_id,
            "loaderId":            loader_id,
            "url":                 url,
            "method":              method,
            "type":                rtype,
            "headers":             sanitized_req_headers,
            "requestHeaders":      sanitized_req_headers,
            "initiator":           initiator,
            "initiatorType":       itype,
            "initiatorStack":      init_stack,
            "initiatingScriptIds": script_ids,
            "initiatorUrls":       init_urls,
            "startTime":           start_time,
            "wallTime":            wall_time,
            "redirectHopIndex":    hop_idx,
            "redirectedFrom":      redirected_from,
            "redirectedTo":        "",
            "requestBody":         body_meta,
            "bodyMetadata":        body_meta,
        }


        # Merge any early-arriving info
        if rid in self._unmatched:
            early = self._unmatched.pop(rid)
            for k, v in early.items():
                entry.setdefault(k, v)

        # Append to master request list and set active pointer
        self._request_list.append(entry)
        self._active_requests[rid] = entry

    def _handle_websocket(self, data: dict) -> None:
        rid = str(data["requestId"])
        entry: dict[str, Any] = {
            "requestId":           rid,
            "id":                  rid,
            "url":                 data.get("url", ""),
            "type":                "WebSocket",
            "initiator":           data.get("initiator"),
            "initiatorType":       "websocket",
            "startTime":           time.time(),
            "redirectHopIndex":    0,
            "redirectedFrom":      "",
            "redirectedTo":        "",
        }
        self._request_list.append(entry)
        self._active_requests[rid] = entry

    def _handle_response(self, data: dict) -> None:
        rid = str(data["requestId"])
        response = data.get("response", {})
        entry = self._active_requests.get(rid)

        if entry is None:
            entry = {"requestId": rid, "id": rid, "url": response.get("url", ""), "type": data.get("type", "Other")}
            self._unmatched[rid] = entry

        entry["type"]            = data.get("type") or entry.get("type")
        entry["status"]          = response.get("status")
        entry["remoteIPAddress"] = response.get("remoteIPAddress")

        if "responseHeaders" not in entry:
            entry["responseHeaders"] = _normalize_headers(response.get("headers"))

    def _handle_response_extra_info(self, data: dict) -> None:
        rid = str(data["requestId"])
        entry = self._active_requests.get(rid)

        if entry is None:
            entry = {"requestId": rid, "id": rid, "url": "<unknown>", "type": "Other"}
            self._unmatched[rid] = entry

        entry["responseHeaders"] = _normalize_headers(data.get("headers"))

    def _handle_failed(self, data: dict, cdp) -> None:
        rid = str(data["requestId"])
        entry = self._active_requests.get(rid)

        if entry is None:
            entry = {"requestId": rid, "id": rid, "url": "<unknown>", "type": data.get("type", "Other")}
            self._unmatched[rid] = entry

        entry["endTime"]       = data.get("timestamp")
        entry["failureReason"] = data.get("errorText") or "unknown error"

    def _handle_finished(self, data: dict, cdp) -> None:
        rid = str(data["requestId"])
        entry = self._active_requests.get(rid)

        if entry is None:
            entry = {"requestId": rid, "id": rid, "url": "<unknown>", "type": "Other"}
            self._unmatched[rid] = entry

        entry["endTime"] = data.get("timestamp")
        size = data.get("encodedDataLength")
        entry["size"] = size if isinstance(size, (int, float)) and size >= 0 else None

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

        for entry in self._request_list:
            if not entry.get("responseBodyHash") and entry.get("endTime"):
                if entry.get("type") == "WebSocket":
                    entry["responseBodyHash"] = ""
                    continue
                entry["responseBodyHash"] = await self._get_body_hash(entry.get("requestId", ""))

    def _build_results(self, final_url: str) -> list[dict]:
        """Convert all preserved request hops to the public RequestData schema."""
        out = []
        entries = self._request_list if self._request_list else list(getattr(self, "_requests", {}).values())
        for entry in entries:
            url = entry.get("url", "")
            try:
                parsed = urlparse(url)
                if parsed.scheme == "data" or not parsed.scheme:
                    continue
            except Exception:
                continue

            start = entry.get("startTime")
            end = entry.get("endTime")
            headers = entry.get("responseHeaders")
            filtered_headers = _filter_headers(headers, self._save_headers) if headers else None

            size = entry.get("size")
            init_urls = entry.get("initiatorUrls") or _get_initiator_info(entry.get("initiator"))[1]
            body_meta = entry.get("bodyMetadata") or entry.get("requestBody")
            has_body = bool(body_meta or entry.get("requestBody"))
            headers_captured = bool(entry.get("headers") or entry.get("requestHeaders"))
            failure_reason = entry.get("failureReason") or ""
            data_quality = "failed" if failure_reason else ("complete" if headers_captured else "degraded")

            item = {
                "requestId":                    entry.get("requestId", ""),
                "id":                           entry.get("requestId", ""),
                "visit_id":                     self._crawl_context.website_id if self._crawl_context else "",
                "attempt_id":                   self._crawl_context.attempt_id if self._crawl_context else "",
                "document_id":                  self._crawl_context.document_id if self._crawl_context else "",
                "frameId":                      entry.get("frameId"),
                "parent_frame_id":              entry.get("parent_frame_id"),
                "loaderId":                     entry.get("loaderId"),
                "url":                          url,
                "sanitized_destination_url":    _sanitize_url_tokens(url),
                "registered_destination_domain": _get_registered_domain(url),
                "method":                       entry.get("method"),
                "type":                         entry.get("type"),
                "resource_type":                entry.get("type"),
                "status":                       entry.get("status"),
                "size":                         int(size) if isinstance(size, float) else size,
                "remoteIPAddress":              entry.get("remoteIPAddress"),
                "headers":                      sanitize_headers(entry.get("headers", {})),
                "requestHeaders":               sanitize_headers(entry.get("requestHeaders", {})),
                "responseHeaders":              filtered_headers,
                "headers_captured":             headers_captured,
                "has_request_body":             has_body,
                "responseBodyHash":             entry.get("responseBodyHash") or "",
                "failureReason":                failure_reason,
                "data_quality_status":          data_quality,
                "redirectHopIndex":             entry.get("redirectHopIndex", 0),
                "redirect_position":            entry.get("redirectHopIndex", 0),
                "redirectedFrom":               entry.get("redirectedFrom") or "",
                "redirectedTo":                 entry.get("redirectedTo") or "",
                "initiatorType":                entry.get("initiatorType", "other"),
                "initiators":                   init_urls,
                "initiatorUrls":                init_urls,
                "normalized_initiator_stack_urls": init_urls,
                "initiatingScriptIds":          entry.get("initiatingScriptIds", []),
                "initiatorStack":               entry.get("initiatorStack"),
                "requestBody":                  body_meta,
                "bodyMetadata":                 body_meta,
                "sanitized_body_metadata":      body_meta,
                "time":                         round(end - start, 6) if isinstance(start, (int, float)) and isinstance(end, (int, float)) else None,
            }

            if self._crawl_context:
                ts_ms = int(entry["wallTime"] * 1000) if entry.get("wallTime") else int(time.time() * 1000)
                self._crawl_context.enrich_event(item, timestamp_ms=ts_ms)
            else:
                item["timestamp_ms"] = int(time.time() * 1000)

            out.append(item)

        return out

    def get_partial_results(self, final_url: str = "") -> list[dict]:
        return self._build_results(final_url)

    def get_results(self, final_url: str = "") -> list[dict]:
        return self._build_results(final_url)

    def handle_request_will_be_sent(self, data: dict) -> None:
        self._handle_request(data, None)

