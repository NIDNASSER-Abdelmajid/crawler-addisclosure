"""Helpers/schema_validator.py — Schema validator for v2.0.0 crawler results.

Validates that a ``result.json`` structure conforms to the trace-oriented
v2.0.0 contract:
- Required top-level keys are present
- ``schema_version`` equals ``"2.0.0"``
- ``document_id`` is a valid UUID
- Monotonic ``event_seq`` numbers are unique across all collectors
- Per-event fields (timestamp_ms, first_party) conform to expected types
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import uuid


REQUIRED_TOP_LEVEL_KEYS = {
    "schema_version",
    "document_id",
    "initialUrl",
    "finalUrl",
    "successful",
    "testStarted",
    "data",
}


class SchemaValidationError(ValueError):
    """Raised when a result.json payload violates schema constraints."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        message = f"Schema validation failed ({len(errors)} error(s)):\n  - " + "\n  - ".join(errors)
        super().__init__(message)


def validate_result(
    result_or_path: dict[str, Any] | str | Path,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """Validate a result dictionary or JSON file against the v2.0.0 schema.

    Parameters
    ----------
    result_or_path : dict or str or Path
        The dictionary or path to ``result.json``.
    raise_on_error : bool
        If True, raises ``SchemaValidationError`` when invalid.

    Returns
    -------
    dict
        {"valid": bool, "errors": list[str], "event_count": int}
    """
    errors: list[str] = []

    if isinstance(result_or_path, (str, Path)):
        p = Path(result_or_path)
        if not p.is_file():
            errors.append(f"Result file does not exist: {p}")
            if raise_on_error:
                raise SchemaValidationError(errors)
            return {"valid": False, "errors": errors, "event_count": 0}
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"Failed to parse JSON file {p}: {exc}")
            if raise_on_error:
                raise SchemaValidationError(errors)
            return {"valid": False, "errors": errors, "event_count": 0}
    elif isinstance(result_or_path, dict):
        payload = result_or_path
    else:
        errors.append(f"Expected dict or file path, got {type(result_or_path)}")
        if raise_on_error:
            raise SchemaValidationError(errors)
        return {"valid": False, "errors": errors, "event_count": 0}

    # 1. Required top-level keys
    for key in REQUIRED_TOP_LEVEL_KEYS:
        if key not in payload:
            errors.append(f"Missing required top-level key: '{key}'")

    # 2. Schema version
    version = payload.get("schema_version")
    if version != "2.0.0":
        errors.append(f"Invalid schema_version: expected '2.0.0', got {repr(version)}")

    # 3. Document ID UUID format
    doc_id = payload.get("document_id")
    if not doc_id:
        errors.append("Missing or empty 'document_id'")
    else:
        try:
            uuid.UUID(str(doc_id))
        except (ValueError, TypeError, AttributeError):
            errors.append(f"Invalid document_id: '{doc_id}' is not a valid UUID")

    # 4. Data container
    data = payload.get("data")
    if not isinstance(data, dict):
        errors.append(f"Expected 'data' to be a dict, got {type(data)}")
        data = {}

    # 5. Monotonic event_seq uniqueness across all collectors
    seen_event_seqs: dict[int, str] = {}
    total_events = 0

    def _record_seq(seq: Any, source_label: str) -> None:
        nonlocal total_events
        if seq is None:
            return
        if not isinstance(seq, int) or seq <= 0:
            errors.append(f"Invalid event_seq in {source_label}: {seq} (must be positive int)")
            return
        total_events += 1
        if seq in seen_event_seqs:
            errors.append(
                f"Duplicate event_seq {seq} found in {source_label} "
                f"(already seen in {seen_event_seqs[seq]})"
            )
        else:
            seen_event_seqs[seq] = source_label

    # Check RequestCollector
    reqs = data.get("RequestCollector", [])
    if isinstance(reqs, list):
        for idx, r in enumerate(reqs):
            if isinstance(r, dict):
                _record_seq(r.get("event_seq"), f"RequestCollector[{idx}]")

    # Check APICallCollector
    api_col = data.get("APICallCollector", {})
    if isinstance(api_col, dict):
        for idx, call in enumerate(api_col.get("savedCalls", [])):
            if isinstance(call, dict):
                _record_seq(call.get("event_seq"), f"APICallCollector.savedCalls[{idx}]")
                ts = call.get("timestamp_ms")
                if ts is not None and (not isinstance(ts, (int, float)) or ts <= 0):
                    errors.append(f"Invalid timestamp_ms in APICallCollector.savedCalls[{idx}]: {ts}")

    # Check FingerprintCollector
    fp_col = data.get("FingerprintCollector", {})
    if isinstance(fp_col, dict):
        for idx, call in enumerate(fp_col.get("savedCalls", [])):
            if isinstance(call, dict):
                _record_seq(call.get("event_seq"), f"FingerprintCollector.savedCalls[{idx}]")
                ts = call.get("timestamp_ms")
                if ts is not None and (not isinstance(ts, (int, float)) or ts <= 0):
                    errors.append(f"Invalid timestamp_ms in FingerprintCollector.savedCalls[{idx}]: {ts}")

    # Check CookieCollector
    cookies = data.get("CookieCollector", [])
    if isinstance(cookies, list):
        for idx, c in enumerate(cookies):
            if isinstance(c, dict):
                _record_seq(c.get("event_seq"), f"CookieCollector[{idx}]")
                fp_flag = c.get("first_party")
                if fp_flag is not None and not isinstance(fp_flag, bool):
                    errors.append(f"CookieCollector[{idx}].first_party must be bool, got {type(fp_flag)}")

    # Check TargetCollector
    targets = data.get("TargetCollector", [])
    if isinstance(targets, list):
        for idx, t in enumerate(targets):
            if isinstance(t, dict):
                _record_seq(t.get("event_seq"), f"TargetCollector[{idx}]")

    # Check ScreenshotCollector
    shots = data.get("ScreenshotCollector", [])
    if isinstance(shots, list):
        for idx, s in enumerate(shots):
            if isinstance(s, dict):
                _record_seq(s.get("event_seq"), f"ScreenshotCollector[{idx}]")

    is_valid = len(errors) == 0
    if not is_valid and raise_on_error:
        raise SchemaValidationError(errors)

    return {
        "valid": is_valid,
        "errors": errors,
        "event_count": total_events,
    }
