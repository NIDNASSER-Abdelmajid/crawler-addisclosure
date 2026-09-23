"""Collect browser API calls using DDG TrackerTracker-style CDP breakpoints and non-destructive wrappers."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from urllib.parse import urlparse
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from Helpers.crawl_context import CrawlContext

from Collectors.APICalls import TrackerTracker
from Collectors.APICalls.breakpoint_script_template import WRAPPER_INIT_SCRIPT

SENSITIVE_API_KEYWORDS = (
    "cookie", "useragent", "storage", "token", "session", "auth",
    "password", "secret", "credential", "private", "href", "url",
)


def _is_sensitive_api_or_value(api_name: str, val_str: str) -> bool:
    api_lower = api_name.lower()
    if any(kw in api_lower for kw in SENSITIVE_API_KEYWORDS):
        return True
    val_lower = val_str.lower()
    if any(kw in val_lower for kw in ("token", "bearer", "session", "auth", "password", "secret", "key=", "cookie=")):
        return True
    return False


class APICallCollector:
    COLLECTOR_NAME = "APICallCollector"
    BINDING_NAME = "registerAPICall"

    def init(
        self,
        output_dir: str,
        logger,
        url_hash: str,
        enable_async_stacktraces: bool = False,
        crawl_context: CrawlContext | None = None,
    ) -> None:
        self._output_dir = output_dir
        self._logger = logger
        self._url_hash = url_hash
        self._enable_async_stacktraces = bool(enable_async_stacktraces)
        self._crawl_context = crawl_context

        # Script-level call statistics: source_url -> {api_name: count}
        self._stats: dict[str, dict[str, int]] = {}
        self._calls: list[dict] = []

        # Provenance mappings for frame and execution context recovery
        self._script_to_context: dict[str, int] = {}
        self._script_to_url: dict[str, str] = {}
        self._script_to_hash: dict[str, str] = {}
        self._context_to_frame: dict[int, str] = {}
        self._frame_to_parent: dict[str, str | None] = {}
        self._frame_to_loader: dict[str, str] = {}

        # Duplicate protection set: (api_name, operation_type, script_id, frame_id, ts_bucket)
        self._seen_event_keys: set[tuple] = set()

        # Ephemeral in-memory raw values used ONLY for within-visit matching (discarded before final save)
        self._raw_values_for_matching: dict[str, list[str]] = {}

        # Structured quality, drop, and reconciliation counters
        self._events_observed_count = 0
        self._dropped_count = 0
        self._deduplicated_count = 0
        self._sampled_count = 0
        self._truncated_count = 0
        self._unsupported_count = 0
        self._redacted_count = 0
        self._serialization_failures_count = 0
        self._collector_failures_count = 0
        self._events_after_shutdown_count = 0
        self._context_setup_failures_count = 0
        self._drop_reasons: dict[str, int] = {}
        self._dedup_reasons: dict[str, int] = {}

        self._ready = False
        self._closed = False
        self._incomplete_data = False

        self._cdp = None
        self._tracker: TrackerTracker | None = None
        self._tasks: set[asyncio.Task] = set()
        self._context_setup_lock = asyncio.Lock()
        self._tracked_context_ids: set[int] = set()
        self._instrumented_context_count = 0
        self._skipped_context_count = 0

    def get_raw_values_for_matching(self) -> dict[str, list[str]]:
        """Return ephemeral in-memory raw values for API-to-request matching."""
        return self._raw_values_for_matching

    def clean_in_memory_values(self) -> None:
        """Purge all ephemeral raw values before saving final crawl results to disk."""
        self._raw_values_for_matching.clear()

    def _record_drop(self, reason: str) -> None:
        self._dropped_count += 1
        self._drop_reasons[reason] = self._drop_reasons.get(reason, 0) + 1

    def _record_dedup(self, reason: str) -> None:
        self._deduplicated_count += 1
        self._dedup_reasons[reason] = self._dedup_reasons.get(reason, 0) + 1

    def _validate_api_name_matches_stack(self, api_name: str, call_stack: Any) -> bool:
        """Reject events whose claimed API name conflicts with their call stack."""
        if not call_stack or not api_name:
            return True

        stack_str = str(call_stack).lower()
        if "api_name_stack_conflict" in stack_str:
            return False

        api_name_lower = api_name.lower()

        # Identify category of claimed API
        api_category = None
        if "cookie" in api_name_lower:
            api_category = "cookie"
        elif any(k in api_name_lower for k in ["localstorage", "sessionstorage", "storage."]):
            api_category = "storage"
        elif any(k in api_name_lower for k in ["canvas", "todataurl", "getimagedata"]):
            api_category = "canvas"
        elif any(k in api_name_lower for k in ["audiocontext", "createoscillator", "createanalyser", "createdynamicscompressor"]):
            api_category = "audio"
        elif any(k in api_name_lower for k in ["webgl", "getparameter", "getextension"]):
            api_category = "webgl"
        elif "useragent" in api_name_lower:
            api_category = "user_agent"
        elif "geolocation" in api_name_lower or "getcurrentposition" in api_name_lower:
            api_category = "geolocation"
        elif "getbattery" in api_name_lower:
            api_category = "battery"

        if not api_category:
            return True

        # Extract frames
        if isinstance(call_stack, list):
            lines = []
            for f in call_stack:
                if isinstance(f, dict):
                    fn = f.get("functionName", "") or ""
                    url = f.get("url", "") or ""
                    lines.append(f"{fn} {url}".lower())
                else:
                    lines.append(str(f).lower())
        else:
            lines = [l.strip().lower() for l in str(call_stack).splitlines() if l.strip()]

        non_wrapper_lines = [
            l for l in lines
            if not any(w in l for w in ["__adgraph", "wrappedget", "wrappedmethod", "registerapicall", "safestringify", "error"])
        ]

        if not non_wrapper_lines:
            return True

        # Check top non-wrapper frames for mutually exclusive native accessors
        conflicting_signatures = {
            "cookie": ["document.cookie", "get cookie", "set cookie", "[as cookie]"],
            "storage": ["storage.getitem", "storage.setitem", "localstorage.getitem", "sessionstorage.getitem", "storage.prototype.getitem"],
            "canvas": ["canvasrenderingcontext2d", "htmlcanvaselement.prototype.todataurl", ".todataurl", ".getimagedata"],
            "audio": ["audiocontext", "createoscillator", "createanalyser", "createdynamicscompressor"],
            "webgl": ["webglrenderingcontext", "webgl2renderingcontext", "getparameter", "getextension"],
            "user_agent": ["navigator.useragent", "get useragent"],
            "geolocation": ["geolocation.getcurrentposition", "getcurrentposition"],
            "battery": ["navigator.getbattery", "getbattery"],
        }

        for line in non_wrapper_lines[:3]:
            for other_cat, sigs in conflicting_signatures.items():
                if other_cat != api_category:
                    if any(sig in line for sig in sigs):
                        return False

        return True

    def _sanitize_single_value(
        self,
        val: Any,
        api_name: str,
        max_len: int = 500,
    ) -> tuple[dict[str, Any] | None, str, str | None]:
        """Sanitizes a single argument or return value.
        
        Returns:
        - persisted_dict: privacy-safe metadata dict
        - status: 'captured', 'captured_redacted', 'truncated', 'unsupported_for_capture_method', 'serialization_failed'
        - raw_str: in-memory string representation for within-visit correlation (or None)
        """
        if val is None:
            return None, "not_requested", None

        # Handle JS Promise representation
        if isinstance(val, dict) and val.get("type") == "Promise":
            persisted = {
                "value_type": "Promise",
                "original_length": 0,
                "hmac": "",
                "is_truncated": False,
                "is_redacted": False,
                "safe_preview": None,
            }
            return persisted, "unsupported_for_capture_method", None

        val_type = type(val).__name__
        raw_str = ""
        try:
            if isinstance(val, dict) and "type" in val:
                val_type = val.get("type") or "object"
                raw_str = str(val.get("repr", val.get("value", str(val))))
                orig_len = val.get("len", len(raw_str))
            else:
                raw_str = str(val)
                orig_len = len(raw_str)
        except Exception:
            self._serialization_failures_count += 1
            persisted = {
                "value_type": "unserializable",
                "original_length": 0,
                "hmac": "",
                "is_truncated": False,
                "is_redacted": False,
                "safe_preview": None,
            }
            return persisted, "serialization_failed", None

        is_truncated = False
        if len(raw_str) > max_len:
            raw_str = raw_str[:max_len]
            is_truncated = True

        is_redacted = _is_sensitive_api_or_value(api_name, raw_str)
        hmac_val = self._crawl_context.hmac_value(raw_str) if self._crawl_context else ""

        # Safe preview: ONLY for non-sensitive scalar values (numbers, booleans, or safe short strings)
        safe_preview = None
        if not is_redacted and val_type in {"int", "float", "bool", "number", "boolean"}:
            safe_preview = raw_str[:50]
        elif not is_redacted and val_type in {"str", "string"} and len(raw_str) <= 50:
            safe_preview = raw_str

        persisted = {
            "value_type": val_type,
            "type": val_type,
            "original_length": orig_len,
            "hmac": hmac_val,
            "is_truncated": is_truncated,
            "truncated": is_truncated,
            "is_redacted": is_redacted,
            "redacted": is_redacted,
            "safe_preview": safe_preview,
        }

        if is_redacted:
            status = "captured_redacted"
            self._redacted_count += 1
        elif is_truncated:
            status = "truncated"
            self._truncated_count += 1
        else:
            status = "captured"

        return persisted, status, raw_str

    def _process_arguments(
        self,
        args: Any,
        api_name: str,
        save_arguments: bool,
    ) -> tuple[list[dict] | None, str, list[str]]:
        if not save_arguments or args is None:
            return None, "not_requested", []
        if not isinstance(args, list):
            args = [args]

        sanitized: list[dict] = []
        statuses: list[str] = []
        raws: list[str] = []
        for item in args[:10]:
            san, st, rw = self._sanitize_single_value(item, api_name)
            if san is not None:
                sanitized.append(san)
            statuses.append(st)
            if rw:
                raws.append(rw)

        if len(args) > 10:
            overall_status = "truncated"
            self._truncated_count += 1
        elif "captured_redacted" in statuses:
            overall_status = "captured_redacted"
        elif "truncated" in statuses:
            overall_status = "truncated"
        elif "serialization_failed" in statuses:
            overall_status = "serialization_failed"
        elif any(s == "captured" for s in statuses):
            overall_status = "captured"
        else:
            overall_status = "not_requested"

        return sanitized, overall_status, raws

    def _process_return_value(
        self,
        ret: Any,
        api_name: str,
        has_return_value: bool,
        is_async: bool,
        threw: bool,
        capture_mechanism: str = "wrapper",
    ) -> tuple[dict | None, str, str | None]:
        if threw:
            self._collector_failures_count += 1
            return None, "capture_failed", None
        if is_async:
            persisted = {
                "value_type": "Promise",
                "type": "Promise",
                "original_length": 0,
                "hmac": "",
                "is_truncated": False,
                "truncated": False,
                "is_redacted": False,
                "redacted": False,
                "safe_preview": None,
            }
            self._unsupported_count += 1
            return persisted, "unsupported_for_capture_method", None
        if capture_mechanism == "breakpoint":
            self._unsupported_count += 1
            return None, "unsupported_for_capture_method", None
        if not has_return_value or ret is None:
            return None, "not_requested", None

        san, st, rw = self._sanitize_single_value(ret, api_name)
        return san, st, rw

    def record_api_access(
        self,
        api_name: str,
        operation_type: str = "property_get",
        source_script: str = "<unknown>",
        frame_id: str | None = None,
        parent_frame_id: str | None = None,
        execution_context_id: int | None = None,
        script_id: str | None = None,
        arguments: Any = None,
        return_value: Any = None,
        call_stack: Any = None,
        captured: bool = True,
        drop_reason: str | None = None,
        capture_mechanism: str = "manual",
        timestamp_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """Record an API access event directly, ensuring single-responsibility tracking."""
        self._events_observed_count += 1
        self._update_call_stats(source_script, api_name)
        if not captured:
            self._record_drop(drop_reason or "not_captured")
            return None

        # Validate that claimed API name does not conflict with call stack
        if not self._validate_api_name_matches_stack(api_name, call_stack):
            self._record_drop("api_name_stack_conflict")
            return None

        # Filter unacceptable URLs
        if not self._is_acceptable_url(source_script):
            self._record_drop("unacceptable_source_url")
            return None

        # Duplicate protection key: (attempt_id, doc_id, script_id, frame_id, api_name, op_type, time_bucket)
        attempt_id = self._crawl_context.attempt_id if self._crawl_context else "att_0"
        doc_id = self._crawl_context.document_id if self._crawl_context else "doc_0"
        ts_ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        ts_bucket = ts_ms // 50
        dup_key = (attempt_id, doc_id, str(script_id or ""), str(frame_id or ""), api_name, operation_type, ts_bucket)
        if dup_key in self._seen_event_keys:
            self._record_dedup("duplicate_event")
            return None
        self._seen_event_keys.add(dup_key)

        if operation_type == "property_get":
            sanitized_args = []
            args_status = "not_requested"
            arg_raws = []
        else:
            sanitized_args, args_status, arg_raws = self._process_arguments(
                arguments, api_name, save_arguments=bool(arguments is not None)
            )

        sanitized_ret, ret_status, ret_raw = self._process_return_value(
            return_value, api_name,
            has_return_value=bool(return_value is not None),
            is_async=isinstance(return_value, dict) and return_value.get("type") == "Promise",
            threw=False,
            capture_mechanism=capture_mechanism,
        )

        api_evt_id = self._crawl_context.next_api_event_id() if self._crawl_context else f"api_evt_{len(self._calls)+1:04d}"

        data_quality = "complete"
        if not frame_id:
            data_quality = "unattributed_frame"
        elif args_status == "truncated" or ret_status == "truncated":
            data_quality = "truncated"
        elif args_status == "captured_redacted" or ret_status == "captured_redacted":
            data_quality = "redacted"

        entry: dict[str, Any] = {
            "api_event_id": api_evt_id,
            "api_name": api_name,
            "operation_type": operation_type,
            "normalized_script_url": source_script,
            "source_script": source_script,
            "source": source_script,
            "script_id": script_id,
            "script_hash": self._script_to_hash.get(str(script_id), "") if script_id else "",
            "frame_id": frame_id,
            "parent_frame_id": parent_frame_id,
            "execution_context_id": execution_context_id,
            "description": api_name,
            "arguments": sanitized_args,
            "arguments_captured_status": args_status,
            "return_value": sanitized_ret,
            "return_value_captured_status": ret_status,
            "call_stack": call_stack,
            "data_quality_status": data_quality,
            "capture_mechanism": capture_mechanism,
        }

        if self._crawl_context:
            self._crawl_context.enrich_event(entry, timestamp_ms=ts_ms)
        else:
            entry["timestamp_ms"] = ts_ms

        # Store in-memory raw values for API-to-request correlation
        combined_raws = list(arg_raws)
        if ret_raw:
            combined_raws.append(ret_raw)
        if combined_raws:
            self._raw_values_for_matching[api_evt_id] = combined_raws

        self._calls.append(entry)
        return entry

    def get_partial_results(self) -> dict:
        """Synchronously return captured API calls and collection summary."""
        call_stats = {
            source: stats
            for source, stats in self._stats.items()
            if self._is_acceptable_url(source)
        }
        saved_calls = [
            call for call in self._calls
            if self._is_acceptable_url(call.get("normalized_script_url") or call.get("source_script") or call.get("source", ""))
        ]
        total_accesses = sum(sum(m.values()) for m in self._stats.values())

        persisted_count = len(saved_calls)
        total_observed = self._events_observed_count
        if total_observed == 0 and persisted_count > 0:
            total_observed = persisted_count + self._deduplicated_count + self._dropped_count + self._sampled_count
        else:
            sum_parts = persisted_count + self._deduplicated_count + self._dropped_count + self._sampled_count
            if sum_parts < total_observed:
                diff = total_observed - sum_parts
                self._dropped_count += diff
                self._drop_reasons["unclassified_drop"] = self._drop_reasons.get("unclassified_drop", 0) + diff
            elif sum_parts > total_observed:
                total_observed = sum_parts

        collection_summary = {
            "totalEventsObserved": total_observed,
            "totalEventsPersisted": persisted_count,
            "totalEventsDeduplicated": self._deduplicated_count,
            "totalEventsDropped": self._dropped_count,
            "totalEventsSampled": self._sampled_count,
            "totalEventsRedacted": self._redacted_count,
            "totalAcceptableScriptSources": len(call_stats),
            "totalAccesses": total_accesses,
            "droppedCapturesCount": self._dropped_count,
            "deduplicatedEventsCount": self._deduplicated_count,
            "sampledEventsCount": self._sampled_count,
            "truncatedCapturesCount": self._truncated_count,
            "unsupportedCapturesCount": self._unsupported_count,
            "failedCapturesCount": self._collector_failures_count,
            "executionContextsDiscovered": len(self._tracked_context_ids),
            "executionContextsInstrumented": self._instrumented_context_count,
            "executionContextsSkipped": self._skipped_context_count,
            "contextSetupFailuresCount": self._context_setup_failures_count,
            "redactedCapturesCount": self._redacted_count,
            "serializationFailuresCount": self._serialization_failures_count,
            "collectorFailuresCount": self._collector_failures_count,
            "eventsAfterShutdownCount": self._events_after_shutdown_count,
            "dropReasons": dict(self._drop_reasons),
            "dedupReasons": dict(self._dedup_reasons),
        }

        return {
            "callStats": call_stats,
            "savedCalls": saved_calls,
            "hasIncompleteData": self._incomplete_data,
            "collectionSummary": collection_summary,
            "droppedCapturesCount": self._dropped_count,
            "truncatedCapturesCount": self._truncated_count,
            "unsupportedCapturesCount": self._unsupported_count,
            "failedCapturesCount": self._collector_failures_count,
        }

    def get_results(self) -> dict:
        return self.get_partial_results()

    def get_raw_values_for_matching(self) -> dict[str, list[str]]:
        """Return the ephemeral raw values mapped by api_event_id for correlation."""
        return dict(self._raw_values_for_matching)

    def clean_in_memory_values(self) -> None:
        """Purge ephemeral in-memory raw values used for value matching."""
        self._raw_values_for_matching.clear()

    def get_parent_frame_id(self, frame_id: str) -> str | None:
        """Return parent frame ID if known."""
        return self._frame_to_parent.get(str(frame_id))

    def get_frame_id_for_script(self, script_id: str) -> str | None:
        """Recover frame ID for a script ID through execution context mapping."""
        cid = self._script_to_context.get(str(script_id))
        if cid is not None:
            return self._context_to_frame.get(cid)
        return None

    def _is_ignored_error(self, exc: Exception) -> bool:
        text = str(exc)
        ignored = (
            "Target closed",
            "Session closed",
            "Cannot find context with specified id",
            "Execution context was destroyed",
            "Target page, context or browser has been closed",
        )
        return any(token in text for token in ignored)

    def _track_task(self, coro, label: str) -> None:
        if not coro:
            return
        if self._closed:
            coro.close()
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return

        task = loop.create_task(coro)
        self._tasks.add(task)

        def _on_done(done_task: asyncio.Task) -> None:
            self._tasks.discard(done_task)
            if done_task.cancelled():
                return
            exc = done_task.exception()
            if exc is None:
                return
            if self._is_ignored_error(exc):
                return
            self._logger.debug(f"[{self.COLLECTOR_NAME}] {label} failed: {exc}")

        task.add_done_callback(_on_done)

    async def _resume_debugger(self) -> None:
        if not self._tracker or self._closed:
            return
        try:
            await self._tracker.send_command("Debugger.resume")
        except Exception as exc:
            if self._is_ignored_error(exc):
                return
            if "Operation timed out" in str(exc):
                self._logger.warning(f"[{self.COLLECTOR_NAME}] Debugger got stuck")
            self._incomplete_data = True
            self._collector_failures_count += 1

    def _update_call_stats(self, source: str, description: str) -> None:
        source_stats = self._stats.setdefault(source, {})
        source_stats[description] = source_stats.get(description, 0) + 1

    def _on_script_parsed(self, params: dict) -> None:
        if self._closed or not self._tracker:
            return
        sid = str(params.get("scriptId", ""))
        cid = params.get("executionContextId")
        url = params.get("url") or params.get("embedderName") or ""
        shash = params.get("hash") or ""
        if sid:
            if cid is not None:
                self._script_to_context[sid] = cid
            self._script_to_url[sid] = url
            if shash:
                self._script_to_hash[sid] = shash
        self._tracker.process_script_parsed(params)

    def _on_frame_navigated(self, params: dict) -> None:
        frame = params.get("frame", {})
        fid = frame.get("id")
        pid = frame.get("parentId")
        lid = frame.get("loaderId")
        if fid:
            fid_str = str(fid)
            self._frame_to_parent[fid_str] = str(pid) if pid else None
            if lid:
                old_lid = self._frame_to_loader.get(fid_str)
                if old_lid and old_lid != lid:
                    to_remove = [c for c, f in self._context_to_frame.items() if f == fid_str]
                    for c in to_remove:
                        self._context_to_frame.pop(c, None)
                self._frame_to_loader[fid_str] = str(lid)

    def _on_frame_attached(self, params: dict) -> None:
        fid = params.get("frameId")
        pid = params.get("parentFrameId")
        if fid and pid:
            self._frame_to_parent[str(fid)] = str(pid)

    def _on_binding_called(self, params: dict) -> None:
        if self._closed or not self._tracker:
            if self._closed:
                self._events_after_shutdown_count += 1
                self._record_drop("collector_closed")
            return
        if params.get("name") != self.BINDING_NAME:
            return

        self._events_observed_count += 1
        breakpoint_info = self._tracker.process_binding_pause(params)
        if not breakpoint_info:
            self._collector_failures_count += 1
            self._record_drop("invalid_binding_payload")
            return

        source = breakpoint_info.get("source") or "<unknown>"
        description = breakpoint_info.get("description")
        if not description:
            self._record_drop("missing_description")
            return

        if not self._is_acceptable_url(source):
            self._record_drop("unacceptable_source_url")
            return

        self._update_call_stats(source, description)

        exec_ctx_id = params.get("executionContextId")
        frame_id = self._context_to_frame.get(exec_ctx_id) if exec_ctx_id is not None else None
        parent_frame_id = self._frame_to_parent.get(frame_id) if frame_id else None

        api_name = breakpoint_info.get("api_name") or description
        op_type = breakpoint_info.get("operation_type") or "unknown"
        script_id = breakpoint_info.get("script_id")
        capture_mech = breakpoint_info.get("capture_mechanism", "binding")
        stack = breakpoint_info.get("stack")

        # Validate that claimed API name does not conflict with call stack
        if not self._validate_api_name_matches_stack(api_name, stack):
            self._record_drop("api_name_stack_conflict")
            return

        # Duplicate protection check
        ts_now = int(time.time() * 1000)
        ts_bucket = round(ts_now / 100)
        dup_key = (api_name, op_type, str(script_id), str(frame_id), ts_bucket)
        if dup_key in self._seen_event_keys:
            self._record_dedup("duplicate_event")
            return
        self._seen_event_keys.add(dup_key)

        save_args = bool(breakpoint_info.get("saveArguments"))
        raw_args = breakpoint_info.get("arguments")
        sanitized_args, args_status, arg_raws = self._process_arguments(raw_args, api_name, save_args)

        sanitized_ret, ret_status, ret_raw = self._process_return_value(
            ret=breakpoint_info.get("returnValue"),
            api_name=api_name,
            has_return_value=bool(breakpoint_info.get("hasReturnValue")),
            is_async=bool(breakpoint_info.get("isAsync")),
            threw=bool(breakpoint_info.get("threw")),
            capture_mechanism=capture_mech,
        )

        api_evt_id = self._crawl_context.next_api_event_id() if self._crawl_context else f"api_evt_{len(self._calls)+1:04d}"

        data_quality = "complete"
        if not frame_id:
            data_quality = "unattributed_frame"
        elif args_status == "truncated" or ret_status == "truncated":
            data_quality = "truncated"
        elif args_status == "captured_redacted" or ret_status == "captured_redacted":
            data_quality = "redacted"

        entry: dict[str, Any] = {
            "api_event_id": api_evt_id,
            "api_name": api_name,
            "operation_type": op_type,
            "normalized_script_url": source,
            "source_script": source,
            "source": source,
            "script_id": str(script_id) if script_id is not None else None,
            "script_hash": self._script_to_hash.get(str(script_id), "") if script_id else "",
            "frame_id": frame_id,
            "parent_frame_id": parent_frame_id,
            "execution_context_id": exec_ctx_id,
            "description": description,
            "arguments": sanitized_args,
            "arguments_captured_status": args_status,
            "return_value": sanitized_ret,
            "return_value_captured_status": ret_status,
            "call_stack": stack,
            "data_quality_status": data_quality,
            "capture_mechanism": capture_mech,
        }

        if self._crawl_context:
            self._crawl_context.enrich_event(entry, timestamp_ms=ts_now)
        else:
            entry["timestamp_ms"] = ts_now

        # Retain in-memory raw values for API-to-request correlation
        combined_raws = list(arg_raws)
        if ret_raw:
            combined_raws.append(ret_raw)
        if combined_raws:
            self._raw_values_for_matching[api_evt_id] = combined_raws

        self._calls.append(entry)

    def _on_debugger_paused(self, params: dict) -> None:
        if self._closed or not self._tracker:
            if self._closed:
                self._events_after_shutdown_count += 1
                self._record_drop("collector_closed")
            return

        # Resume debugger immediately so page execution is not frozen
        self._track_task(self._resume_debugger(), "Debugger.resume")
        self._events_observed_count += 1

        breakpoint_info = self._tracker.process_debugger_pause(params)
        if not breakpoint_info or breakpoint_info.get("collision") or breakpoint_info.get("error"):
            reason = (breakpoint_info.get("reason") if breakpoint_info else None) or (breakpoint_info.get("error") if breakpoint_info else None) or "unknown_breakpoint"
            self._record_drop(reason)
            return

        source = breakpoint_info.get("source") or "<unknown>"
        description = breakpoint_info.get("description")
        if not description:
            self._record_drop("missing_description")
            return

        if not self._is_acceptable_url(source):
            self._record_drop("unacceptable_source_url")
            return

        self._update_call_stats(source, description)

        call_frames = breakpoint_info.get("call_frames") or []
        script_id = None
        exec_ctx_id = None
        frame_id = None
        parent_frame_id = None
        unattributed_reason = None

        if call_frames:
            loc = call_frames[0].get("location") or {}
            if loc.get("scriptId") is not None:
                script_id = str(loc.get("scriptId"))
                exec_ctx_id = self._script_to_context.get(script_id)
                if exec_ctx_id is not None:
                    frame_id = self._context_to_frame.get(exec_ctx_id)
                    if frame_id:
                        parent_frame_id = self._frame_to_parent.get(frame_id)

        if not frame_id:
            if not script_id:
                unattributed_reason = "missing_script_id"
            elif exec_ctx_id is None:
                unattributed_reason = "script_not_mapped_to_context"
            else:
                unattributed_reason = "context_not_mapped_to_frame"

        api_name = breakpoint_info.get("api_name") or description
        op_type = breakpoint_info.get("operation_type") or "unknown"

        stack_lines = []
        for f in call_frames[:8]:
            fn = f.get("functionName") or "(anonymous)"
            u = f.get("url") or ""
            loc = f.get("location") or {}
            stack_lines.append(f"    at {fn} ({u}:{loc.get('lineNumber', 0)}:{loc.get('columnNumber', 0)})")
        stack_str = "\n".join(stack_lines)

        # Validate that claimed API name does not conflict with call stack
        if not self._validate_api_name_matches_stack(api_name, stack_str):
            self._record_drop("api_name_stack_conflict")
            return

        ts_now = int(time.time() * 1000)
        ts_bucket = round(ts_now / 100)
        dup_key = (api_name, op_type, str(script_id), str(frame_id), ts_bucket)
        if dup_key in self._seen_event_keys:
            self._record_dedup("duplicate_event")
            return
        self._seen_event_keys.add(dup_key)

        save_args = bool(breakpoint_info.get("saveArguments"))
        call_args_dict = self._tracker.retrieve_call_arguments(breakpoint_info.get("id"))
        raw_args = call_args_dict.get("arguments") if call_args_dict else None
        sanitized_args, args_status, arg_raws = self._process_arguments(raw_args, api_name, save_args)

        sanitized_ret, ret_status, ret_raw = self._process_return_value(
            ret=None,
            api_name=api_name,
            has_return_value=False,
            is_async=False,
            threw=False,
            capture_mechanism="breakpoint",
        )

        api_evt_id = self._crawl_context.next_api_event_id() if self._crawl_context else f"api_evt_{len(self._calls)+1:04d}"

        data_quality = "complete"
        if not frame_id:
            data_quality = "unattributed_frame"
        elif args_status == "truncated":
            data_quality = "truncated"
        elif args_status == "captured_redacted":
            data_quality = "redacted"

        entry: dict[str, Any] = {
            "api_event_id": api_evt_id,
            "api_name": api_name,
            "operation_type": op_type,
            "normalized_script_url": source,
            "source_script": source,
            "source": source,
            "script_id": str(script_id) if script_id is not None else None,
            "script_hash": self._script_to_hash.get(str(script_id), "") if script_id else "",
            "frame_id": frame_id,
            "parent_frame_id": parent_frame_id,
            "execution_context_id": exec_ctx_id,
            "description": description,
            "arguments": sanitized_args,
            "arguments_captured_status": args_status,
            "return_value": sanitized_ret,
            "return_value_captured_status": ret_status,
            "call_stack": stack_str,
            "data_quality_status": data_quality,
            "unattributed_frame_reason": unattributed_reason,
            "capture_mechanism": "breakpoint",
        }

        if self._crawl_context:
            self._crawl_context.enrich_event(entry, timestamp_ms=ts_now)
        else:
            entry["timestamp_ms"] = ts_now

        if arg_raws:
            self._raw_values_for_matching[api_evt_id] = list(arg_raws)

        self._calls.append(entry)

    async def _handle_execution_context_created(self, params: dict) -> None:
        if self._closed or not self._tracker:
            return

        context = params.get("context") or {}
        aux_data = context.get("auxData") or {}
        origin = context.get("origin")
        context_type = aux_data.get("type")

        # Ignore isolated contexts created by Playwright itself.
        if (not origin or origin == "://") and context_type == "isolated":
            self._skipped_context_count += 1
            return

        context_id = context.get("id")
        if context_id is None:
            return

        frame_id = aux_data.get("frameId")
        if frame_id:
            self._context_to_frame[context_id] = str(frame_id)

        # Skip non-web contexts (e.g. extension internals)
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme and parsed.scheme not in {"http", "https"}:
                self._skipped_context_count += 1
                return

        if context_id in self._tracked_context_ids:
            return
        self._tracked_context_ids.add(context_id)

        try:
            async with self._context_setup_lock:
                await self._tracker.setup_context_tracking(context_id)
            self._instrumented_context_count += 1
        except Exception as exc:
            self._context_setup_failures_count += 1
            self._incomplete_data = True
            self._logger.debug(f"[{self.COLLECTOR_NAME}] context tracking setup error: {exc}")

    def _on_execution_context_created(self, params: dict) -> None:
        if params and "context" in params:
            ctx = params["context"]
            cid = ctx.get("id")
            aux = ctx.get("auxData") or {}
            fid = aux.get("frameId")
            if cid is not None and fid:
                self._context_to_frame[cid] = str(fid)

        self._track_task(
            self._handle_execution_context_created(params),
            "Runtime.executionContextCreated",
        )

    @staticmethod
    def _is_acceptable_url(url_string: str) -> bool:
        """Allow unknown, inline, and valid web URLs; drop massive data: URIs."""
        if not url_string:
            return True
        if url_string == "<unknown>" or url_string.startswith("inline"):
            return True
        try:
            parsed = urlparse(url_string)
            if parsed.scheme == "data":
                return False
            return True
        except Exception:
            return True

    async def pre_crawl(self, page) -> None:
        self._closed = False
        self._incomplete_data = False

        def _mark_closed(*_) -> None:
            self._closed = True

        page.on("close", _mark_closed)

        self._cdp = await page.context.new_cdp_session(page)
        self._tracker = TrackerTracker(self._cdp.send, self._logger.debug)
        self._tracker.set_main_url(page.url or "")

        # Enable Page domain to receive frameNavigated and frameAttached events
        try:
            await self._cdp.send("Page.enable")
        except Exception:
            pass

        # Install wrapper initialization script on every new document/frame early
        try:
            await self._cdp.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": WRAPPER_INIT_SCRIPT},
            )
        except Exception:
            pass

        self._cdp.on("Debugger.scriptParsed", self._on_script_parsed)
        self._cdp.on("Debugger.paused", self._on_debugger_paused)
        self._cdp.on("Page.frameNavigated", self._on_frame_navigated)
        self._cdp.on("Page.frameAttached", self._on_frame_attached)
        self._cdp.on("Runtime.executionContextCreated", self._on_execution_context_created)
        self._cdp.on("Runtime.bindingCalled", self._on_binding_called)

        await self._cdp.send("Runtime.addBinding", {"name": self.BINDING_NAME})
        await self._tracker.init(enable_async_stacktraces=self._enable_async_stacktraces)

        self._ready = True

    async def _drain_tasks(self, timeout_seconds: float = 5.0) -> None:
        if not self._tasks:
            return

        done, pending = await asyncio.wait(list(self._tasks), timeout=timeout_seconds)
        self._tasks.difference_update(done)

        for task in pending:
            task.cancel()
            self._tasks.discard(task)

    async def collect(self, page) -> dict:
        if not self._ready:
            self._logger.warning(f"[{self.COLLECTOR_NAME}] pre_crawl was not called; skipping")
            return self.get_partial_results()

        try:
            await page.wait_for_timeout(2000)
        except Exception:
            pass

        if self._tracker:
            self._tracker.set_main_url(page.url or "")

        await self._drain_tasks(timeout_seconds=5.0)
        self._closed = True

        if self._cdp:
            try:
                await self._cdp.detach()
            except Exception as exc:
                if not self._is_ignored_error(exc):
                    self._logger.debug(f"[{self.COLLECTOR_NAME}] CDP detach failed: {exc}")

        results = self.get_partial_results()
        self._logger.info(
            f"[{self.COLLECTOR_NAME}] Recorded {len(results['savedCalls'])} API call(s) "
            f"from {len(results['callStats'])} source(s)"
        )
        return results
