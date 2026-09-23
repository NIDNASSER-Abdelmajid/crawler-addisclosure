"""TrackerTracker port used by APICallCollector.

This module consumes APICalls assets from:
- resources/apicalls/breakpoints.json
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from .breakpoint_script_template import BREAKPOINT_SCRIPT_TEMPLATE, WRAPPER_INIT_SCRIPT

MAX_ASYNC_CALL_STACK_DEPTH = 32
HTTP_URL_REGEX = re.compile(r"^https?://", re.IGNORECASE)

_RESOURCES_DIR = Path(__file__).resolve().parents[2] / "resources"
_BREAKPOINTS_PATH = _RESOURCES_DIR / "apicalls" / "breakpoints.json"

_BREAKPOINTS_CACHE: list[dict[str, Any]] | None = None
_TEMPLATE_CACHE: str | None = None

_IGNORED_BREAKPOINT_ERRORS = (
    "Target closed",
    "Session closed",
    "Breakpoint at specified location already exists.",
    "Cannot find context with specified id",
    "API unavailable in given context.",
    "Target page, context or browser has been closed",
)


def _error_text(exc: Exception) -> str:
    return exc if isinstance(exc, str) else str(exc)


def _load_breakpoints() -> list[dict[str, Any]]:
    global _BREAKPOINTS_CACHE
    if _BREAKPOINTS_CACHE is not None:
        return _BREAKPOINTS_CACHE

    with _BREAKPOINTS_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, list):
        raise ValueError("APICalls breakpoints.json must contain a JSON array")

    _BREAKPOINTS_CACHE = data
    return _BREAKPOINTS_CACHE


def _load_breakpoint_template() -> str:
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is not None:
        return _TEMPLATE_CACHE

    _TEMPLATE_CACHE = BREAKPOINT_SCRIPT_TEMPLATE
    return _TEMPLATE_CACHE


def _escape_js_single_quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class TrackerTracker:
    """Minimal Python port of DDG's APICalls TrackerTracker helper."""

    def __init__(self, send_command: Callable[..., Any], log: Callable[..., None]) -> None:
        self._send = send_command
        self._log = log
        self._enable_async_stacktraces = False

        self._id_to_breakpoint: dict[str, dict[str, Any]] = {}
        self._desc_to_breakpoint: dict[str, dict[str, Any]] = {}
        self._main_url = ""
        self._script_id_to_url: dict[str, str] = {}
        self._pending_calls: dict[str, dict[str, Any]] = {}

        self._all_breakpoints = _load_breakpoints()
        self._breakpoint_template = _load_breakpoint_template()

    async def init(self, enable_async_stacktraces: bool = False) -> None:
        self._enable_async_stacktraces = bool(enable_async_stacktraces)
        await self._send("Debugger.enable")
        await self._send("Runtime.enable")
        await self._send(
            "Runtime.setAsyncCallStackDepth",
            {"maxDepth": MAX_ASYNC_CALL_STACK_DEPTH},
        )

    async def send_command(self, command: str, payload: dict | None = None) -> Any:
        return await self._send(command, payload or {})

    def set_main_url(self, url: str) -> None:
        self._main_url = url or ""

    def _get_breakpoint_by_id(self, breakpoint_id: str) -> dict[str, Any] | None:
        return self._id_to_breakpoint.get(str(breakpoint_id))

    def _get_breakpoint_by_description(self, breakpoint_description: str) -> dict[str, Any] | None:
        return self._desc_to_breakpoint.get(breakpoint_description)

    def _get_breakpoint_script(
        self,
        breakpoint: dict[str, Any],
        description: str,
    ) -> str:
        save_arguments = bool(breakpoint.get("saveArguments"))
        argument_collection = (
            "try { capturedArgs = Array.from(arguments).slice(0, 10).map(a => String(a).slice(0, 500)); } "
            "catch (_) { capturedArgs = ['<unserializable>']; }"
            if save_arguments
            else ""
        )

        script = (
            self._breakpoint_template
            .replace("ARGUMENT_COLLECTION", argument_collection)
            .replace("DESCRIPTION", _escape_js_single_quoted(description))
            .replace("SAVE_ARGUMENTS", "true" if save_arguments else "false")
        )

        condition = breakpoint.get("condition")
        if condition:
            script = f"""
            if (!!({condition})) {{
                {script}
            }}
            """

        script = f"""
        let shouldPause = false;
        {script}
        {'' if self._enable_async_stacktraces else 'shouldPause = false;'}
        shouldPause;
        """
        return script

    async def _add_breakpoint(
        self,
        context_id: int | None,
        expression: str,
        description: str,
        breakpoint: dict[str, Any],
    ) -> None:
        try:
            evaluate_payload: dict[str, Any] = {
                "expression": expression,
                "silent": True,
            }
            if context_id is not None:
                evaluate_payload["contextId"] = context_id

            result = await self._send("Runtime.evaluate", evaluate_payload)
            if result.get("exceptionDetails"):
                raise RuntimeError("API unavailable in given context.")

            object_id = (result.get("result") or {}).get("objectId")
            if not object_id:
                return

            condition_script = self._get_breakpoint_script(breakpoint, description)
            cdp_breakpoint_result = await self._send(
                "Debugger.setBreakpointOnFunctionCall",
                {
                    "objectId": object_id,
                    "condition": condition_script,
                },
            )

            breakpoint_id = str(cdp_breakpoint_result.get("breakpointId", ""))
            if not breakpoint_id:
                return

            enriched = {
                **breakpoint,
                "cdpId": breakpoint_id,
                "description": description,
            }
            self._id_to_breakpoint[breakpoint_id] = enriched
            self._desc_to_breakpoint[description] = enriched
        except Exception as exc:
            error = _error_text(exc)
            if any(ignore in error for ignore in _IGNORED_BREAKPOINT_ERRORS):
                return
            self._log("[APICallCollector] setting breakpoint failed: %s %s", description, error)

    async def setup_context_tracking(self, context_id: int | None = None) -> None:
        try:
            eval_payload: dict[str, Any] = {"expression": WRAPPER_INIT_SCRIPT, "silent": True}
            if context_id is not None:
                eval_payload["contextId"] = context_id
            await self._send("Runtime.evaluate", eval_payload)
        except Exception as exc:
            self._log("[APICallCollector] wrapper init script failed: %s", exc)

        for group in self._all_breakpoints:
            proto = group.get("proto")
            obj = group.get("global") or (f"{proto}.prototype" if proto else None)
            if not obj:
                continue

            for prop in group.get("props", []):
                name = prop.get("name")
                if not name:
                    continue
                accessor = "set" if prop.get("setter") is True else "get"
                expression = f"Reflect.getOwnPropertyDescriptor({obj}, '{name}').{accessor}"
                description = prop.get("description") or f"{obj}.{name}"
                enriched_prop = dict(prop)
                enriched_prop["operation_type"] = "property_set" if prop.get("setter") is True else "property_get"
                
                # Install non-destructive getter wrapper if getter
                if accessor == "get":
                    try:
                        wrap_expr = f"if (typeof window.__adgraph_wrap_getter__ === 'function') window.__adgraph_wrap_getter__('{obj}', '{name}', '{description}');"
                        wrap_payload: dict[str, Any] = {"expression": wrap_expr, "silent": True}
                        if context_id is not None:
                            wrap_payload["contextId"] = context_id
                        await self._send("Runtime.evaluate", wrap_payload)
                    except Exception:
                        pass

                await self._add_breakpoint(context_id, expression, description, enriched_prop)

            for method in group.get("methods", []):
                name = method.get("name")
                if not name:
                    continue
                expression = f"Reflect.getOwnPropertyDescriptor({obj}, '{name}').value"
                description = method.get("description") or f"{obj}.{name}"
                enriched_method = dict(method)
                enriched_method["operation_type"] = "method_call"

                # Install non-destructive method wrapper
                try:
                    wrap_expr = f"if (typeof window.__adgraph_wrap_method__ === 'function') window.__adgraph_wrap_method__('{obj}', '{name}', '{description}');"
                    wrap_payload: dict[str, Any] = {"expression": wrap_expr, "silent": True}
                    if context_id is not None:
                        wrap_payload["contextId"] = context_id
                    await self._send("Runtime.evaluate", wrap_payload)
                except Exception:
                    pass

                await self._add_breakpoint(context_id, expression, description, enriched_method)

    def _get_script_url_from_stack_trace(self, params: dict[str, Any] | None) -> str | None:
        if not isinstance(params, dict):
            return None

        call_frames = params.get("callFrames") or []
        for frame in call_frames:
            script_id = frame.get("scriptId")
            file_url = self._script_id_to_url.get(str(script_id)) if script_id is not None else None
            frame_url = frame.get("url")
            for candidate in (frame_url, file_url):
                if candidate and HTTP_URL_REGEX.match(candidate):
                    return candidate

        parent = params.get("parent")
        if isinstance(parent, dict):
            return self._get_script_url_from_stack_trace(parent)
        return None

    def _normalize_source_url(self, script: str | None) -> str:
        """Normalize source URL without replacing unknown source scripts with publisher URL."""
        if not script or script.strip() == "":
            return "<unknown>"

        try:
            if script.startswith(("http://", "https://")):
                return script
            if self._main_url and self._main_url.startswith(("http://", "https://")):
                return urljoin(self._main_url, script)
            return script
        except Exception:
            return "<unknown>"

    def _get_script_url_from_paused_event(self, params: dict[str, Any]) -> str:
        script = None

        call_frames = params.get("callFrames") or []
        for frame in call_frames:
            frame_url = frame.get("url")

            location = frame.get("location") or {}
            location_script_id = location.get("scriptId")
            location_url = self._script_id_to_url.get(str(location_script_id)) if location_script_id is not None else None

            function_location = frame.get("functionLocation") or {}
            function_script_id = function_location.get("scriptId")
            function_url = self._script_id_to_url.get(str(function_script_id)) if function_script_id is not None else None

            for candidate in (frame_url, function_url, location_url):
                if candidate and HTTP_URL_REGEX.match(candidate):
                    script = candidate
                    break
            if script:
                break

        if not script:
            script = self._get_script_url_from_stack_trace(params.get("asyncStackTrace"))

        return self._normalize_source_url(script)

    def retrieve_call_arguments(self, breakpoint_id: str) -> dict[str, Any] | None:
        return self._pending_calls.pop(str(breakpoint_id), None)

    def process_script_parsed(self, params: dict[str, Any]) -> None:
        script_id = params.get("scriptId")
        if script_id is None:
            return

        script_id_str = str(script_id)
        if script_id_str in self._script_id_to_url:
            self._log("[APICallCollector] duplicate scriptId: %s", script_id_str)

        embedder_name = params.get("embedderName") or params.get("url") or ""
        self._script_id_to_url[script_id_str] = str(embedder_name)

    def get_script_id_for_url(self, url: str | None) -> str | None:
        if not url:
            return None
        for sid, surl in self._script_id_to_url.items():
            if surl and (surl == url or surl in url or url in surl):
                return sid
        return None

    @staticmethod
    def _normalize_call_arguments(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value]
        return [str(value)]

    def process_binding_pause(self, params: dict[str, Any]) -> dict[str, Any] | None:
        payload_raw = params.get("payload")
        try:
            payload = json.loads(payload_raw)
        except Exception:
            self._log("[APICallCollector] invalid breakpoint payload: %s", payload_raw)
            return None

        description = payload.get("description")
        if not description:
            return None

        breakpoint = self._get_breakpoint_by_description(description)
        if not breakpoint:
            breakpoint = {"description": description, "saveArguments": False, "operation_type": "unknown"}

        args = payload.get("args")
        source_url = payload.get("url")
        stack = payload.get("stack")

        # If source_url is missing, recover first HTTP/HTTPS script URL from stack trace lines
        if (not source_url or source_url == "<unknown>") and stack and isinstance(stack, str):
            for line in stack.split("\n"):
                m = re.search(r"(?:https?://[^)\s:]+)", line)
                if m:
                    source_url = m.group(0)
                    break

        norm_source = self._normalize_source_url(source_url)
        script_id = self.get_script_id_for_url(source_url) or self.get_script_id_for_url(norm_source)
        operation_type = payload.get("operation_type") or breakpoint.get("operation_type") or ("property_set" if breakpoint.get("setter") else "method_call")
        api_name = payload.get("api_name") or description

        return {
            "description": description,
            "api_name": api_name,
            "operation_type": operation_type,
            "source": norm_source,
            "script_id": script_id,
            "saveArguments": bool(breakpoint.get("saveArguments") or args is not None),
            "arguments": args,
            "returnValue": payload.get("returnValue"),
            "hasReturnValue": bool(payload.get("hasReturnValue")),
            "isAsync": bool(payload.get("isAsync")),
            "threw": bool(payload.get("threw")),
            "capture_mechanism": payload.get("capture_mechanism", "binding"),
            "stack": stack,
            "breakpoint": breakpoint,
        }

    def process_debugger_pause(self, params: dict[str, Any]) -> dict[str, Any] | None:
        call_frames = params.get("callFrames") or []
        # Detect breakpoint-wrapper collision first:
        for frame in call_frames[:5]:
            fn = frame.get("functionName") or ""
            url = frame.get("url") or ""
            if "__adgraph" in fn or "__adgraph" in url or fn in {"wrappedGet", "wrappedMethod", "safeStringify"}:
                self._log("[APICallCollector] breakpoint-wrapper collision detected in frame %s", fn)
                return {"collision": True, "reason": "breakpoint_wrapper_collision"}

        hit_breakpoints = params.get("hitBreakpoints") or []
        if not hit_breakpoints:
            return {"collision": False, "error": "no_hit_breakpoints"}

        breakpoint_id = str(hit_breakpoints[0])
        breakpoint = self._get_breakpoint_by_id(breakpoint_id)
        if not breakpoint:
            self._log("[APICallCollector] unknown pause breakpoint: %s", hit_breakpoints)
            return {"collision": False, "error": "unknown_breakpoint"}

        source = self._get_script_url_from_paused_event(params)
        script_id = None
        if call_frames:
            loc = call_frames[0].get("location") or {}
            script_id = loc.get("scriptId")

        operation_type = breakpoint.get("operation_type") or ("property_set" if breakpoint.get("setter") else "method_call")

        return {
            "id": breakpoint_id,
            "description": breakpoint.get("description"),
            "api_name": breakpoint.get("description"),
            "operation_type": operation_type,
            "saveArguments": bool(breakpoint.get("saveArguments")),
            "source": source,
            "script_id": script_id,
            "call_frames": call_frames,
        }
