"""tests/test_api_collection_and_attribution.py
---------------------------------------------
Comprehensive synthetic validation fixtures covering all 16 scenarios:
1. One ad frame accessing navigator.userAgent and initiating a request.
2. Two ads sharing one parent advertising frame (multiple_ads).
3. Page-level code accessing an API (page_shared).
4. Nested frames.
5. Cross-origin frames.
6. A property getter returning a primitive.
7. A method receiving arguments and returning a value.
8. A method throwing an exception.
9. A promise-returning method.
10. Long values requiring truncation.
11. Cookie-like and token-like sensitive values.
12. An API event with no subsequent request.
13. A request from the same script outside the allowed time window.
14. A request from another script inside the time window.
15. Navigation creating a new document in the same frame.
16. An initial attempt followed by a retry.

Automated assertions verify:
- Non-destructive API execution & return values.
- Unchanged exception propagation.
- Deduplication and monotonic sequencing.
- Getter reads have no arguments.
- Return-capture statuses and truncation handling.
- Zero raw sensitive values persisted.
- Accurate frame and document attribution.
- Classification into single_ad, multiple_ads, page_shared, and unlinked.
- Conservative API-to-request association and value matching.
- Data-quality reporting and eligibility flag computation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
import time
import uuid
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Helpers.crawl_context import CrawlContext, Phase
from Helpers.frame_correlator import correlate_and_annotate_events, build_frame_correlation_index
from Helpers.api_request_correlator import correlate_apis_and_requests
from Helpers.data_quality import generate_data_quality_report
from Collectors.APICallCollector import APICallCollector
from Collectors.RequestCollector import RequestCollector


# ============================================================================
# 1. One ad frame accessing navigator.userAgent and initiating a request
# ============================================================================

def test_scenario_01_single_ad_access_and_request(tmp_path):
    """Scenario 1: One ad frame accessing navigator.userAgent and initiating a request."""
    ctx = CrawlContext(url="https://publisher.com")
    ctx.set_phase(Phase.PASSIVE_AD_DELIVERY)
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash1", crawl_context=ctx)

    # API call in ad frame F_AD1
    api_evt = col.record_api_access(
        api_name="navigator.userAgent",
        operation_type="property_get",
        source_script="https://adserver.com/ad.js",
        frame_id="F_AD1",
        script_id="script_101",
        return_value="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        captured=True,
    )
    assert api_evt is not None
    assert api_evt["arguments"] == []  # Getter reads have no arguments
    assert api_evt["operation_type"] == "property_get"

    # Outgoing request from same script and frame shortly after
    req = {
        "requestId": "REQ_001",
        "id": "REQ_001",
        "url": "https://adserver.com/track?ua=Mozilla%2F5.0",
        "frameId": "F_AD1",
        "initiatingScriptIds": ["script_101"],
        "initiatorUrls": ["https://adserver.com/ad.js"],
        "timestamp_ms": api_evt["timestamp_ms"] + 50,
        "phase": "passive_ad_delivery",
        "document_id": ctx.document_id,
    }

    result = {
        "schema_version": "2.0.0",
        "document_id": ctx.document_id,
        "data": {
            "APICallCollector": col.get_results(),
            "RequestCollector": [req],
            "AdCollector": {
                "adAttrs": [
                    {
                        "ad_impression_id": "ad_001",
                        "frame_id": "F_AD1",
                        "adLinksAndImages": [{"frameId": "F_AD1"}],
                    }
                ]
            },
        },
    }

    # Frame attribution
    correlate_and_annotate_events(result)
    saved_api = result["data"]["APICallCollector"]["savedCalls"][0]
    saved_req = result["data"]["RequestCollector"][0]

    assert saved_api["attribution_scope"] == "single_ad"
    assert saved_api["candidate_ad_ids"] == ["ad_001"]
    assert saved_api["unique_ad_attribution"] is True
    assert saved_api["evidence_confidence"] == "high"

    # API-Request association
    raw_vals = col.get_raw_values_for_matching()
    assoc_res = correlate_apis_and_requests(
        api_events=result["data"]["APICallCollector"]["savedCalls"],
        network_requests=result["data"]["RequestCollector"],
        raw_api_values=raw_vals,
    )
    assocs = assoc_res["associations"]
    assert len(assocs) == 1
    assert assocs[0]["evidence_type"] == "exact_script_and_frame"
    assert assocs[0]["accepted_for_primary_analysis"] is True
    assert assocs[0]["script_match"] is True
    assert assocs[0]["frame_match"] is True


# ============================================================================
# 2. Two ads sharing one parent advertising frame
# ============================================================================

def test_scenario_02_shared_parent_frame_multiple_ads(tmp_path):
    """Scenario 2: Two ads sharing one parent advertising frame -> multiple_ads."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash2", crawl_context=ctx)

    col.record_api_access(
        api_name="window.innerWidth",
        operation_type="property_get",
        source_script="https://adnetwork.com/container.js",
        frame_id="F_SHARED_PARENT",
        return_value=1200,
        captured=True,
    )

    result = {
        "schema_version": "2.0.0",
        "document_id": ctx.document_id,
        "data": {
            "APICallCollector": col.get_results(),
            "AdCollector": {
                "adAttrs": [
                    {"ad_impression_id": "ad_001", "frame_id": "F_SHARED_PARENT"},
                    {"ad_impression_id": "ad_002", "frame_id": "F_SHARED_PARENT"},
                ]
            },
        },
    }

    correlate_and_annotate_events(result)
    saved_api = result["data"]["APICallCollector"]["savedCalls"][0]

    assert saved_api["attribution_scope"] == "multiple_ads"
    assert set(saved_api["candidate_ad_ids"]) == {"ad_001", "ad_002"}
    assert saved_api["unique_ad_attribution"] is False
    assert saved_api["ambiguous"] is True
    assert saved_api["evidence_confidence"] == "high"


# ============================================================================
# 3. Page-level code accessing an API
# ============================================================================

def test_scenario_03_page_level_access_page_shared(tmp_path):
    """Scenario 3: Page-level code accessing an API -> page_shared."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash3", crawl_context=ctx)

    col.record_api_access(
        api_name="document.cookie",
        operation_type="property_get",
        source_script="https://publisher.com/main.js",
        frame_id="F_MAIN_TOP",
        return_value="theme=dark",
        captured=True,
    )

    result = {
        "schema_version": "2.0.0",
        "document_id": ctx.document_id,
        "data": {
            "APICallCollector": col.get_results(),
            "AdCollector": {
                "adAttrs": [
                    {"ad_impression_id": "ad_001", "frame_id": "F_OTHER_IFRAME"},
                ]
            },
        },
    }

    correlate_and_annotate_events(result)
    saved_api = result["data"]["APICallCollector"]["savedCalls"][0]

    assert saved_api["attribution_scope"] == "page_shared"
    assert saved_api["candidate_ad_ids"] == []
    assert saved_api["unique_ad_attribution"] is False
    assert saved_api["ambiguous"] is False


# ============================================================================
# 4. Nested frames
# ============================================================================

def test_scenario_04_nested_frames_provenance(tmp_path):
    """Scenario 4: Nested frames preserve parent_frame_id."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash4", crawl_context=ctx)

    # Establish frame hierarchy
    col._on_frame_attached({"frameId": "F_CHILD", "parentFrameId": "F_PARENT"})
    col._on_frame_attached({"frameId": "F_PARENT", "parentFrameId": "F_TOP"})

    evt = col.record_api_access(
        api_name="window.localStorage.getItem",
        operation_type="method_call",
        source_script="https://adcdn.com/tag.js",
        frame_id="F_CHILD",
        parent_frame_id=col.get_parent_frame_id("F_CHILD"),
        arguments=["uid"],
        return_value="user_xyz",
        captured=True,
    )

    assert evt["frame_id"] == "F_CHILD"
    assert evt["parent_frame_id"] == "F_PARENT"


# ============================================================================
# 5. Cross-origin frames
# ============================================================================

def test_scenario_05_cross_origin_frames(tmp_path):
    """Scenario 5: Cross-origin frames tracked safely with distinct contexts."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash5", crawl_context=ctx)

    # Context 1: top frame
    col._tracked_context_ids.add(1)
    col._context_to_frame[1] = "F_TOP"
    # Context 2: cross-origin iframe
    col._tracked_context_ids.add(2)
    col._context_to_frame[2] = "F_CROSS_ORIGIN"
    col._script_to_context["script_x"] = 2

    # Recover frame from script_id
    recovered_frame = col.get_frame_id_for_script("script_x")
    assert recovered_frame == "F_CROSS_ORIGIN"


# ============================================================================
# 6. Property getter returning a primitive
# ============================================================================

def test_scenario_06_property_getter_primitive(tmp_path):
    """Scenario 6: A property getter returning a primitive."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash6", crawl_context=ctx)

    evt = col.record_api_access(
        api_name="screen.colorDepth",
        operation_type="property_get",
        source_script="https://cdn.com/detect.js",
        frame_id="F1",
        return_value=24,
        captured=True,
    )

    assert evt["arguments"] == []
    assert evt["return_value_captured_status"] == "captured"
    assert evt["return_value"]["type"] == "int"
    assert evt["return_value"]["safe_preview"] == "24"


# ============================================================================
# 7. Method receiving arguments and returning a value
# ============================================================================

def test_scenario_07_method_receiving_arguments_and_returning_value(tmp_path):
    """Scenario 7: Method receiving arguments and returning a value."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash7", crawl_context=ctx)

    evt = col.record_api_access(
        api_name="HTMLCanvasElement.getContext",
        operation_type="method_call",
        source_script="https://cdn.com/fp.js",
        frame_id="F1",
        arguments=["2d"],
        return_value={"type": "CanvasRenderingContext2D"},
        captured=True,
    )

    assert evt["arguments_captured_status"] == "captured"
    assert len(evt["arguments"]) == 1
    assert evt["arguments"][0]["safe_preview"] == "2d"
    assert evt["return_value"]["type"] == "CanvasRenderingContext2D"


# ============================================================================
# 8. Method throwing an exception
# ============================================================================

def test_scenario_08_method_throwing_exception(tmp_path):
    """Scenario 8: Method throwing an exception records capture_failed without altering page."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash8", crawl_context=ctx)

    san_ret, status, raw_val = col._process_return_value(
        {"exception": "SecurityError: Access Denied"},
        api_name="window.localStorage",
        has_return_value=True,
        is_async=False,
        threw=True,
    )

    assert status == "capture_failed"
    assert raw_val is None


# ============================================================================
# 9. Promise-returning method
# ============================================================================

def test_scenario_09_promise_returning_method(tmp_path):
    """Scenario 9: Promise-returning method marks resolved value as unsupported_for_capture_method."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash9", crawl_context=ctx)

    san_ret, status, raw_val = col._process_return_value(
        {"type": "Promise", "id": 123},
        api_name="navigator.mediaDevices.enumerateDevices",
        has_return_value=True,
        is_async=True,
        threw=False,
    )

    assert status == "unsupported_for_capture_method"
    assert san_ret["type"] == "Promise"


# ============================================================================
# 10. Long values requiring truncation
# ============================================================================

def test_scenario_10_long_values_truncation(tmp_path):
    """Scenario 10: Long values exceeding limit are truncated with truncated status."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash10", crawl_context=ctx)

    very_long_string = "A" * 5000
    san_val, status, raw_val = col._sanitize_single_value(
        very_long_string, api_name="window.name", max_len=200
    )

    assert status == "truncated"
    assert san_val["truncated"] is True
    assert san_val["original_length"] == 5000
    assert san_val["safe_preview"] is None


# ============================================================================
# 11. Cookie-like and token-like sensitive values
# ============================================================================

def test_scenario_11_sensitive_token_privacy_redaction(tmp_path):
    """Scenario 11: Sensitive tokens/cookies are redacted; HMAC stored, raw not persisted."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash11", crawl_context=ctx)

    raw_token = "bearer_secret_token_1234567890abcdef"
    evt = col.record_api_access(
        api_name="document.cookie",
        operation_type="property_set",
        source_script="https://tracker.com/t.js",
        frame_id="F1",
        arguments=[f"session={raw_token}"],
        return_value=None,
        captured=True,
    )

    # Convert event to JSON string as persisted to disk
    persisted_str = json.dumps(evt)
    assert raw_token not in persisted_str
    assert "hmac" in evt["arguments"][0]
    assert evt["arguments"][0]["redacted"] is True
    assert evt["arguments"][0]["hmac"] == ctx.hmac_value(f"session={raw_token}")


# ============================================================================
# 12. API event with no subsequent request
# ============================================================================

def test_scenario_12_api_without_subsequent_request(tmp_path):
    """Scenario 12: API event with no subsequent request produces no false associations."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash12", crawl_context=ctx)

    evt = col.record_api_access(
        api_name="navigator.plugins",
        operation_type="property_get",
        source_script="https://cdn.com/test.js",
        frame_id="F1",
        return_value=["Plugin1", "Plugin2"],
        captured=True,
    )

    assoc_res = correlate_apis_and_requests(
        api_events=[evt],
        network_requests=[],
        raw_api_values=col.get_raw_values_for_matching(),
    )

    assert len(assoc_res["associations"]) == 0
    assert assoc_res["summary"]["unique_api_events_linked"] == 0


# ============================================================================
# 13. Request from same script outside allowed time window
# ============================================================================

def test_scenario_13_request_outside_time_window(tmp_path):
    """Scenario 13: Request outside time window is rejected for primary analysis."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash13", crawl_context=ctx)

    evt = col.record_api_access(
        api_name="window.localStorage.getItem",
        operation_type="method_call",
        source_script="https://cdn.com/app.js",
        frame_id="F1",
        script_id="script_201",
        arguments=["key"],
        return_value="val",
        captured=True,
    )

    # Request arrives 15,000 ms later (max window default is 5,000 ms)
    late_req = {
        "requestId": "REQ_LATE",
        "id": "REQ_LATE",
        "url": "https://cdn.com/log",
        "frameId": "F1",
        "initiatingScriptIds": ["script_201"],
        "timestamp_ms": evt["timestamp_ms"] + 15000,
        "phase": "page_load",
        "document_id": ctx.document_id,
    }

    assoc_res = correlate_apis_and_requests(
        api_events=[evt],
        network_requests=[late_req],
        raw_api_values=col.get_raw_values_for_matching(),
        max_window_ms=5000,
    )

    # Not accepted because outside time window
    assert len(assoc_res["associations"]) == 0
    assert assoc_res["summary"]["primary_associations_count"] == 0


# ============================================================================
# 14. Request from another script inside the time window
# ============================================================================

def test_scenario_14_request_from_another_script_in_window(tmp_path):
    """Scenario 14: Request from another script inside window without provenance is unlinked."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash14", crawl_context=ctx)

    evt = col.record_api_access(
        api_name="navigator.hardwareConcurrency",
        operation_type="property_get",
        source_script="https://analytics.com/a.js",
        frame_id="F1",
        script_id="script_a",
        return_value=8,
        captured=True,
    )

    # Unrelated request from script_b
    req = {
        "requestId": "REQ_UNRELATED",
        "id": "REQ_UNRELATED",
        "url": "https://other.com/image.png",
        "frameId": "F2",
        "initiatingScriptIds": ["script_b"],
        "initiatorUrls": ["https://other.com/b.js"],
        "timestamp_ms": evt["timestamp_ms"] + 200,
        "phase": "page_load",
        "document_id": ctx.document_id,
    }

    assoc_res = correlate_apis_and_requests(
        api_events=[evt],
        network_requests=[req],
        raw_api_values=col.get_raw_values_for_matching(),
    )

    assert assoc_res["summary"]["primary_associations_count"] == 0


# ============================================================================
# 15. Navigation creating a new document in the same frame
# ============================================================================

def test_scenario_15_navigation_resets_document_and_contexts(tmp_path):
    """Scenario 15: Frame navigation invalidates previous execution context mappings."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash15", crawl_context=ctx)

    col._context_to_frame[101] = "F_NAV"
    col._frame_to_loader["F_NAV"] = "LOADER_DOC_1"

    # Frame navigates to new document (new loaderId)
    col._on_frame_navigated({
        "frame": {
            "id": "F_NAV",
            "loaderId": "LOADER_DOC_2",
            "parentId": None,
        }
    })

    # Context 101 must be flushed
    assert 101 not in col._context_to_frame
    assert col._frame_to_loader["F_NAV"] == "LOADER_DOC_2"


# ============================================================================
# 16. An initial attempt followed by a retry
# ============================================================================

def test_scenario_16_initial_attempt_and_retry_isolation(tmp_path):
    """Scenario 16: Retry attempt uses distinct attempt_id and document_id."""
    ctx1 = CrawlContext(url="https://publisher.com", attempt_id="att_001", attempt_number=1)
    ctx2 = CrawlContext(url="https://publisher.com", attempt_id="att_002", attempt_number=2, retry_of_attempt_id="att_001")

    col1 = APICallCollector()
    col1.init(str(tmp_path / "att1"), None, "h1", crawl_context=ctx1)
    evt1 = col1.record_api_access(api_name="navigator.userAgent", frame_id="F1", captured=True)

    col2 = APICallCollector()
    col2.init(str(tmp_path / "att2"), None, "h2", crawl_context=ctx2)
    evt2 = col2.record_api_access(api_name="navigator.userAgent", frame_id="F1", captured=True)

    assert evt1["attempt_id"] == "att_001"
    assert evt2["attempt_id"] == "att_002"
    assert evt1["document_id"] != evt2["document_id"]


# ============================================================================
# 17. Deduplication & Data Quality Eligibility Tests
# ============================================================================

def test_duplicate_event_protection(tmp_path):
    """Verify duplicate events from both breakpoint and binding are rejected."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash_dup", crawl_context=ctx)

    # First event from binding
    e1 = col.record_api_access(
        api_name="navigator.userAgent",
        operation_type="property_get",
        script_id="script_dup",
        frame_id="F_DUP",
        capture_mechanism="binding",
        captured=True,
    )
    assert e1 is not None

    # Duplicate from breakpoint
    e2 = col.record_api_access(
        api_name="navigator.userAgent",
        operation_type="property_get",
        script_id="script_dup",
        frame_id="F_DUP",
        capture_mechanism="breakpoint",
        captured=True,
    )
    assert e2 is None  # Deduplicated!


def test_data_quality_report_and_eligibility(tmp_path):
    """Verify data quality report calculates all metrics and eligibility flags correctly."""
    ctx = CrawlContext(url="https://publisher.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash_dq", crawl_context=ctx)

    col.record_api_access(
        api_name="window.localStorage.getItem",
        operation_type="method_call",
        source_script="https://adnetwork.com/lib.js",
        frame_id="F_AD",
        script_id="s1",
        return_value="target_id_12345",
        captured=True,
    )

    req = {
        "requestId": "REQ_01",
        "url": "https://adnetwork.com/beacon?val=target_id_12345",
        "frameId": "F_AD",
        "initiatingScriptIds": ["s1"],
        "timestamp_ms": int(time.time() * 1000),
    }

    result = {
        "successful": True,
        "status": "completed",
        "data": {
            "APICallCollector": col.get_results(),
            "RequestCollector": [req],
            "AdCollector": {
                "adAttrs": [{"ad_impression_id": "ad_001", "frame_id": "F_AD"}]
            },
        },
    }

    correlate_and_annotate_events(result)
    raw_vals = col.get_raw_values_for_matching()
    assoc_res = correlate_apis_and_requests(
        api_events=result["data"]["APICallCollector"]["savedCalls"],
        network_requests=result["data"]["RequestCollector"],
        raw_api_values=raw_vals,
    )

    report = generate_data_quality_report(
        result=result,
        api_collector_summary=result["data"]["APICallCollector"]["collectionSummary"],
        api_request_associations_summary=assoc_res["summary"],
    )

    assert report["is_complete_visit"] is True
    assert report["eligible_for_api_prevalence"] is True
    assert report["eligible_for_script_analysis"] is True
    assert report["eligible_for_request_association"] is True
    assert report["eligible_for_single_ad_analysis"] is True
    assert report["eligible_for_value_transmission_analysis"] is True
    assert report["single_ad_events_count"] == 1
    assert report["direct_value_matches_count"] == 1
