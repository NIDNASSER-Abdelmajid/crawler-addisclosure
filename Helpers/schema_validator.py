"""Helpers/schema_validator.py — Schema validator for v2.0.0 crawler results.

Validates that a ``result.json`` structure conforms to the trace-oriented
v2.0.0 contract:
- Required top-level keys are present
- ``schema_version`` equals ``"2.0.0"``
- ``document_id`` is a valid UUID
- ``successful`` is a boolean (True/False)
- ``status`` is an explicit status string
- Monotonic ``event_seq`` numbers are unique across all collectors
- Reconciliation of ad candidates and impressions
- No broken references across ads, disclosure attempts, and events
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
    "status",
    "testStarted",
    "data",
}

VALID_STATUSES = {
    "completed",
    "completed_with_partial_data",
    "timed_out",
    "failed",
    "rejected",
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
    """Validate a result dictionary or JSON file against the v2.0.0 schema."""
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

    # 4. Successful boolean check
    successful = payload.get("successful")
    if not isinstance(successful, bool):
        errors.append(f"Expected 'successful' to be boolean True/False, got {repr(successful)} ({type(successful).__name__})")

    # 5. Status string check
    status = payload.get("status")
    if not isinstance(status, str) or status not in VALID_STATUSES:
        errors.append(f"Invalid or missing 'status': expected one of {VALID_STATUSES}, got {repr(status)}")

    # 6. Data container
    data = payload.get("data")
    if not isinstance(data, dict):
        errors.append(f"Expected 'data' to be a dict, got {type(data)}")
        data = {}

    # 7. Monotonic event_seq uniqueness across all collectors
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

    # 8. Ad Candidate Reconciliation & Reference Integrity
    ad_data = data.get("AdCollector", {})

    retained_ad_ids: set[str] = set()
    candidate_ids: set[str] = set()

    if isinstance(ad_data, dict):
        scrape_results = ad_data.get("scrapeResults", {})
        n_detected = scrape_results.get("nDetectedAds", 0)
        n_scraped = scrape_results.get("nAdsScraped", 0)
        n_small = scrape_results.get("nSmallAds", 0)
        n_empty = scrape_results.get("nEmptyAds", 0)
        n_removed = scrape_results.get("nRemovedAds", 0)
        n_skipped = scrape_results.get("nSkippedAds", 0)
        n_timed_out = scrape_results.get("nTimedOutAds", 0)

        # Count reconciliation: every detected ad candidate outcome must reconcile
        if n_detected > 0:
            sum_parts = n_scraped + n_small + n_empty + n_removed + n_skipped + n_timed_out
            if n_detected != sum_parts:
                errors.append(
                    f"Inconsistent ad counts: nDetectedAds ({n_detected}) != "
                    f"sum of outcomes ({sum_parts}: scraped={n_scraped}, small={n_small}, "
                    f"empty={n_empty}, removed={n_removed}, skipped={n_skipped}, timed_out={n_timed_out})"
                )

        # Check candidate records
        candidate_records = ad_data.get("candidateAds", [])
        for cand in candidate_records:
            cid = cand.get("ad_candidate_id")
            if cid:
                candidate_ids.add(cid)

        # Check adAttrs
        ad_attrs = ad_data.get("adAttrs", [])
        if len(ad_attrs) != n_scraped:
            errors.append(f"Inconsistent adAttrs length ({len(ad_attrs)}) != nAdsScraped ({n_scraped})")

        for idx, ad in enumerate(ad_attrs):
            imp_id = ad.get("ad_impression_id")
            cand_id = ad.get("ad_candidate_id")
            if not imp_id:
                errors.append(f"AdCollector.adAttrs[{idx}] missing ad_impression_id")
            else:
                retained_ad_ids.add(imp_id)

            if cand_id and candidate_ids and cand_id not in candidate_ids:
                errors.append(
                    f"Broken reference: adAttrs[{idx}] candidate_id '{cand_id}' not found in candidateAds"
                )

    # 9. Disclosure Attempt Reference Integrity
    disc_data = data.get("AdDisclosureCollector", {})
    known_disc_attempt_ids: set[str] = set()
    if isinstance(disc_data, dict):
        for att in disc_data.get("attempts", []):
            att_id = att.get("disclosure_attempt_id")
            if att_id:
                known_disc_attempt_ids.add(att_id)
            ad_imp = att.get("ad_impression_id")
            if ad_imp and retained_ad_ids and ad_imp not in retained_ad_ids:
                errors.append(
                    f"Broken reference: disclosure attempt '{att_id}' references unknown ad_impression_id '{ad_imp}'"
                )

    is_valid = len(errors) == 0
    if not is_valid and raise_on_error:
        raise SchemaValidationError(errors)

    return {
        "valid": is_valid,
        "errors": errors,
        "event_count": total_events,
    }
