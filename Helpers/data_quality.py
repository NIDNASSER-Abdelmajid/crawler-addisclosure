"""Helpers/data_quality.py — Data Quality Reporting and Analysis Eligibility Assessment.

Builds structured end-of-visit quality report with eligibility flags for each downstream analysis:
- eligible_for_api_prevalence
- eligible_for_script_analysis
- eligible_for_request_association
- eligible_for_single_ad_analysis
- eligible_for_value_transmission_analysis
"""

from __future__ import annotations

from typing import Any


def generate_data_quality_report(
    result: dict[str, Any],
    api_collector_summary: dict[str, Any] | None = None,
    api_request_associations_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce comprehensive structured data quality report and eligibility flags."""
    data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}

    api_col = data.get("APICallCollector", {})
    saved_calls = api_col.get("savedCalls", []) if isinstance(api_col, dict) else []
    call_stats = api_col.get("callStats", {}) if isinstance(api_col, dict) else {}
    has_incomplete_api = bool(api_col.get("hasIncompleteData", False)) if isinstance(api_col, dict) else False

    requests = data.get("RequestCollector", [])
    if not isinstance(requests, list):
        requests = []

    ad_col = data.get("AdCollector", {})
    ads = ad_col.get("adAttrs", []) if isinstance(ad_col, dict) else []

    # Count contexts and scripts
    col_summary = api_collector_summary or (api_col.get("collectionSummary", {}) if isinstance(api_col, dict) else {})
    contexts_discovered = col_summary.get("executionContextsDiscovered", 0)
    contexts_instrumented = col_summary.get("executionContextsInstrumented", 0)
    contexts_skipped = col_summary.get("executionContextsSkipped", 0)
    contexts_failed = col_summary.get("contextSetupFailuresCount", 0)

    scripts_observed = col_summary.get("totalAcceptableScriptSources", len(call_stats))
    scripts_attributed = sum(
        1 for s in call_stats
        if s and s != "<unknown>" and not s.startswith("inline")
    )

    api_events_observed = col_summary.get("totalEventsObserved", len(saved_calls))
    api_events_persisted = len(saved_calls)
    api_events_without_frame = sum(1 for c in saved_calls if not c.get("frame_id"))

    # Requests
    requests_observed = len(requests)
    requests_with_initiator = sum(
        1 for r in requests
        if r.get("initiatingScriptIds") or r.get("initiatorUrls") or (r.get("initiatorStack") and r["initiatorStack"].get("callFrames"))
    )

    # Ad attribution distribution across all API events
    single_ad_events = sum(1 for c in saved_calls if c.get("attribution_scope") == "single_ad")
    multiple_ads_events = sum(1 for c in saved_calls if c.get("attribution_scope") == "multiple_ads")
    page_shared_events = sum(1 for c in saved_calls if c.get("attribution_scope") == "page_shared")
    unlinked_events = sum(1 for c in saved_calls if c.get("attribution_scope") in {"unlinked", None})

    # Association metrics
    assoc_summary = api_request_associations_summary or {}
    direct_value_matches = assoc_summary.get("direct_value_matches_count", 0)
    same_script_associations = assoc_summary.get("same_script_subsequent_requests_count", 0)

    truncated_values_count = col_summary.get("truncatedCapturesCount", 0)
    redacted_values_count = col_summary.get("redactedCapturesCount", 0)

    # Determine eligibility flags
    successful_crawl = bool(result.get("successful", False))
    status = result.get("status", "")

    # 1. API Prevalence: valid if page loaded and API collection was active without fatal failure
    contexts_active = contexts_instrumented > 0 or api_events_persisted > 0
    eligible_for_api_prevalence = (
        (successful_crawl or status in {"completed", "completed_with_partial_data"})
        and not has_incomplete_api
        and contexts_active
    )

    # 2. Script Analysis: valid if scripts were observed and attributed
    eligible_for_script_analysis = eligible_for_api_prevalence and (scripts_attributed > 0)

    # 3. Request Association: valid if requests were collected and have initiator information
    eligible_for_request_association = (
        eligible_for_api_prevalence
        and requests_observed > 0
        and requests_with_initiator > 0
    )

    # 4. Single Ad Analysis: valid if at least one ad exists and single_ad events were isolated
    eligible_for_single_ad_analysis = (
        eligible_for_api_prevalence
        and len(ads) > 0
        and single_ad_events > 0
    )

    # 5. Value Transmission Analysis: valid if direct value matches or confirmed associations exist
    eligible_for_value_transmission_analysis = (
        eligible_for_request_association
        and (direct_value_matches > 0 or assoc_summary.get("primary_associations_count", 0) > 0)
    )

    is_complete_visit = bool(
        (successful_crawl or status == "completed")
        and not has_incomplete_api
        and contexts_failed == 0
    )

    return {
        "is_complete_visit": is_complete_visit,
        "execution_contexts_discovered": contexts_discovered,
        "contexts_instrumented": contexts_instrumented,
        "contexts_skipped": contexts_skipped,
        "contexts_failed": contexts_failed,
        "scripts_observed": scripts_observed,
        "scripts_attributed": scripts_attributed,
        "api_events_observed": api_events_observed,
        "api_events_persisted": api_events_persisted,
        "api_events_without_frame": api_events_without_frame,
        "requests_observed": requests_observed,
        "requests_with_initiator_evidence": requests_with_initiator,
        "single_ad_events": single_ad_events,
        "single_ad_events_count": single_ad_events,
        "multiple_ads_events": multiple_ads_events,
        "page_shared_events": page_shared_events,
        "unlinked_events": unlinked_events,
        "direct_value_matches": direct_value_matches,
        "direct_value_matches_count": direct_value_matches,
        "same_script_request_associations": same_script_associations,
        "truncated_values_count": truncated_values_count,
        "redacted_values_count": redacted_values_count,
        "eligible_for_api_prevalence": eligible_for_api_prevalence,
        "eligible_for_script_analysis": eligible_for_script_analysis,
        "eligible_for_request_association": eligible_for_request_association,
        "eligible_for_single_ad_analysis": eligible_for_single_ad_analysis,
        "eligible_for_value_transmission_analysis": eligible_for_value_transmission_analysis,
    }
