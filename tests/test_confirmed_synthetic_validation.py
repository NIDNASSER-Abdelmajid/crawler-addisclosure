"""tests/test_confirmed_synthetic_validation.py
---------------------------------------------
Comprehensive synthetic validation suite for:
1. Breakpoint-wrapper collision prevention & stack conflict rejection.
2. Event-time phase derivation from historical phase transitions.
3. Main-frame and publisher activity assigned to 'page_shared' by default.
4. Transmission matching strictly restricted to script-controlled components.
5. Exclusion of automatic browser headers from transmission evidence.
6. Strict event counter reconciliation: observed == persisted + deduplicated + dropped + sampled.
7. Disclosure NLP taxonomy and visit-level technical alignment.
8. Sanitization of raw URLs, headers, tokens, and DOM fields.
9. Separate storage of accepted associations and rejected candidate pairs.
10. Full synthetic scenario with known APIs, values, frames, requests, and ad ownership.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Collectors.APICallCollector import APICallCollector
from Collectors.APICalls.tracker_tracker import TrackerTracker
from Helpers.api_request_correlator import (
    AUTOMATIC_BROWSER_HEADERS,
    correlate_apis_and_requests,
    extract_script_controlled_request_text,
    test_value_in_text,
)
from Helpers.crawl_context import CrawlContext, Phase
from Helpers.disclosure_nlp import (
    ALL_TECHNICAL_CATEGORIES,
    classify_statement_nlp,
    compute_visit_disclosure_alignment,
    get_api_category,
    process_visit_disclosures,
)
from Helpers.frame_correlator import correlate_and_annotate_events
from Helpers.sanitization import (
    sanitize_dom_text,
    sanitize_headers,
    sanitize_token_value,
    sanitize_url,
)


# ============================================================================
# 1. Breakpoint-Wrapper Collision Prevention & Stack Conflict Rejection
# ============================================================================

def test_breakpoint_wrapper_collision_prevention():
    """Verify that breakpoint pause inside adgraph wrapper code is detected and suppressed."""
    tracker = TrackerTracker(lambda cmd, p=None: {}, lambda *args: None)
    
    # Simulate a CDP Debugger.paused event whose call frame is our wrapper
    wrapper_pause_params = {
        "hitBreakpoints": ["b1"],
        "callFrames": [
            {"functionName": "wrappedGet", "url": "https://example.com/adgraph_wrapper.js"},
            {"functionName": "safeStringify", "url": "https://example.com/adgraph_wrapper.js"},
            {"functionName": "pageFunction", "url": "https://example.com/page.js"},
        ],
    }
    res = tracker.process_debugger_pause(wrapper_pause_params)
    assert res is not None
    assert res.get("collision") is True
    assert res.get("reason") == "breakpoint_wrapper_collision"

    # Now verify APICallCollector records the drop when collision occurs
    collector = APICallCollector()
    collector.init(output_dir="/tmp", logger=logging.getLogger("test"), url_hash="test")
    collector._tracker = tracker
    collector._resume_debugger = lambda: None

    collector._on_debugger_paused(wrapper_pause_params)
    summary = collector.get_partial_results()["collectionSummary"]
    assert summary["totalEventsObserved"] == 1
    assert summary["totalEventsDropped"] == 1
    assert summary["dropReasons"].get("breakpoint_wrapper_collision") == 1
    assert summary["totalEventsPersisted"] == 0


def test_reject_api_name_stack_conflict():
    """Verify events whose claimed API name conflicts with their call stack are rejected."""
    collector = APICallCollector()
    collector.init(output_dir="/tmp", logger=logging.getLogger("test"), url_hash="test")

    # Incompatible: claimed "document.cookie", but stack top is Canvas getImageData
    stack_conflict = (
        "Error\n"
        "    at CanvasRenderingContext2D.getImageData (https://ad.com/lib.js:10:5)\n"
        "    at trackUser (https://ad.com/tracker.js:20:12)"
    )
    res = collector.record_api_access(
        api_name="document.cookie",
        operation_type="property_get",
        source_script="https://ad.com/tracker.js",
        call_stack=stack_conflict,
    )
    assert res is None, "Conflicting event must be dropped"

    summary = collector.get_partial_results()["collectionSummary"]
    assert summary["dropReasons"].get("api_name_stack_conflict") == 1
    assert summary["totalEventsDropped"] == 1

    # Incompatible: claimed "HTMLCanvasElement.prototype.toDataURL", but stack top is Storage.getItem
    stack_conflict_2 = (
        "Error\n"
        "    at Storage.getItem (<anonymous>)\n"
        "    at getFingerprint (https://ad.com/fp.js:5:1)"
    )
    res2 = collector.record_api_access(
        api_name="HTMLCanvasElement.prototype.toDataURL",
        operation_type="method_call",
        source_script="https://ad.com/fp.js",
        call_stack=stack_conflict_2,
    )
    assert res2 is None
    summary2 = collector.get_partial_results()["collectionSummary"]
    assert summary2["dropReasons"].get("api_name_stack_conflict") == 2

    # Valid: user code calling document.cookie without conflicting native frames
    valid_stack = (
        "Error\n"
        "    at trackUser (https://ad.com/tracker.js:20:12)\n"
        "    at https://ad.com/tracker.js:45:3"
    )
    res3 = collector.record_api_access(
        api_name="document.cookie",
        operation_type="property_get",
        source_script="https://ad.com/tracker.js",
        call_stack=valid_stack,
    )
    assert res3 is not None, "Valid call stack must be accepted"
    summary3 = collector.get_partial_results()["collectionSummary"]
    assert summary3["totalEventsPersisted"] == 1


# ============================================================================
# 2. Event-Time Phase Assignment
# ============================================================================

def test_event_time_phase_assignment():
    """Verify that event phase is assigned from event-time state, not collector final state."""
    ctx = CrawlContext(crawl_id="c1", website_id="w1", attempt_id="att1")
    t0 = int(time.time() * 1000)

    # 1. Start in page_load
    assert ctx.get_phase() == Phase.PAGE_LOAD

    # 2. Transition to passive_ad_delivery at t0 + 1000
    t1 = t0 + 1000
    ctx.phase_transitions.append({
        "from_phase": Phase.PAGE_LOAD,
        "to_phase": Phase.PASSIVE_AD_DELIVERY,
        "timestamp_ms": t1,
    })
    ctx.phase_tracker.set(Phase.PASSIVE_AD_DELIVERY)

    # 3. Transition to disclosure_interaction at t0 + 3000
    t2 = t0 + 3000
    ctx.phase_transitions.append({
        "from_phase": Phase.PASSIVE_AD_DELIVERY,
        "to_phase": Phase.DISCLOSURE_INTERACTION,
        "timestamp_ms": t2,
    })
    ctx.phase_tracker.set(Phase.DISCLOSURE_INTERACTION)

    # The current collector state is now DISCLOSURE_INTERACTION
    assert ctx.get_phase() == Phase.DISCLOSURE_INTERACTION

    # An event with timestamp at t0 + 500 must receive PAGE_LOAD
    ev_early = ctx.enrich_event({"name": "api_call_early"}, timestamp_ms=t0 + 500)
    assert ev_early["phase"] == Phase.PAGE_LOAD

    # An event with timestamp at t0 + 1500 must receive PASSIVE_AD_DELIVERY
    ev_mid = ctx.enrich_event({"name": "api_call_mid"}, timestamp_ms=t0 + 1500)
    assert ev_mid["phase"] == Phase.PASSIVE_AD_DELIVERY

    # An event with timestamp at t0 + 3500 must receive DISCLOSURE_INTERACTION
    ev_late = ctx.enrich_event({"name": "api_call_late"}, timestamp_ms=t0 + 3500)
    assert ev_late["phase"] == Phase.DISCLOSURE_INTERACTION


# ============================================================================
# 3. Main-Frame and Publisher Activity as Page-Shared by Default
# ============================================================================

def test_main_frame_and_publisher_activity_as_page_shared():
    """Verify that main frame and publisher activity default to page_shared."""
    result = {
        "url": "https://news.example.com/article",
        "publisher_domain": "example.com",
        "data": {
            "AdCollector": {
                "main_frame_id": "frame_main_root",
                "adAttrs": [
                    {
                        "ad_impression_id": "ad_001",
                        "frame_id": "frame_isolated_ad",
                        "adLinksAndImages": [{"frameId": "frame_isolated_ad"}],
                    }
                ],
            },
            "RequestCollector": [
                # Request 1: Main frame publisher request
                {
                    "requestId": "req_pub_main",
                    "frameId": "frame_main_root",
                    "url": "https://news.example.com/api/articles",
                    "is_main_frame": True,
                },
                # Request 2: Ad isolated iframe request
                {
                    "requestId": "req_ad_subframe",
                    "frameId": "frame_isolated_ad",
                    "url": "https://doubleclick.net/ad",
                    "is_main_frame": False,
                },
            ],
            "APICallCollector": {
                "savedCalls": [
                    # API 1: Executed on main frame by publisher script
                    {
                        "api_event_id": "api_main_pub",
                        "frame_id": "frame_main_root",
                        "parent_frame_id": None,
                        "source_script": "https://news.example.com/app.js",
                    },
                    # API 2: Executed inside isolated ad frame
                    {
                        "api_event_id": "api_ad_sub",
                        "frame_id": "frame_isolated_ad",
                        "parent_frame_id": "frame_main_root",
                        "source_script": "https://doubleclick.net/ad.js",
                    },
                ]
            },
        },
    }

    correlate_and_annotate_events(result)

    reqs = result["data"]["RequestCollector"]
    calls = result["data"]["APICallCollector"]["savedCalls"]

    # Request 1 (Main frame, publisher): must be page_shared
    assert reqs[0]["attribution_scope"] == "page_shared"
    assert reqs[0]["unique_ad_attribution"] is False

    # Request 2 (Isolated ad subframe): must be single_ad
    assert reqs[1]["attribution_scope"] == "single_ad"
    assert reqs[1]["unique_ad_attribution"] is True
    assert reqs[1]["related_ad_ids"] == ["ad_001"]

    # API 1 (Main frame, publisher): must be page_shared
    assert calls[0]["attribution_scope"] == "page_shared"
    assert calls[0]["unique_ad_attribution"] is False

    # API 2 (Isolated ad subframe): must be single_ad
    assert calls[1]["attribution_scope"] == "single_ad"
    assert calls[1]["unique_ad_attribution"] is True
    assert calls[1]["related_ad_ids"] == ["ad_001"]


# ============================================================================
# 4 & 5. Restrict Transmission Matching & Exclude Automatic Browser Headers
# ============================================================================

def test_transmission_matching_excludes_automatic_headers():
    """Verify that transmission evidence excludes automatic headers like User-Agent."""
    user_agent_val = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"

    # Request with automatic browser headers containing User-Agent, but query and body DO NOT have it
    req_with_auto_headers = {
        "requestId": "req_001",
        "timestamp_ms": 2000,
        "phase": "page_load",
        "frameId": "frame_1",
        "url": "https://tracker.com/pixel?ts=12345",
        "headers": {
            "User-Agent": user_agent_val,
            "Accept": "text/html,application/xhtml+xml",
            "Sec-Ch-Ua": '"Not_A Brand";v="8"',
        },
        "initiatingScriptIds": ["script_1"],
    }

    script_text = extract_script_controlled_request_text(req_with_auto_headers)
    # The automatic User-Agent header must NOT be present in script-controlled text
    assert user_agent_val not in script_text
    assert "Accept" not in script_text

    # API call capturing navigator.userAgent
    api_events = [
        {
            "api_event_id": "api_ua",
            "timestamp_ms": 1000,
            "phase": "page_load",
            "script_id": "script_1",
            "frame_id": "frame_1",
            "api_name": "navigator.userAgent",
        }
    ]
    raw_vals = {"api_ua": [user_agent_val]}

    res = correlate_apis_and_requests(
        api_events=api_events,
        network_requests=[req_with_auto_headers],
        raw_api_values=raw_vals,
    )

    assoc = res["accepted_associations"][0]
    # Because User-Agent was only in automatic headers, value_match_type must NOT be exact_value_match
    assert assoc["value_match_type"] == "same_script_subsequent_request"
    assert res["summary"]["direct_value_matches_count"] == 0

    # Now simulate a script deliberately sending userAgent in a query parameter: ?ua=Mozilla...
    req_with_script_transmission = {
        "requestId": "req_002",
        "timestamp_ms": 2100,
        "phase": "page_load",
        "frameId": "frame_1",
        "url": f"https://tracker.com/pixel?ua={user_agent_val}",
        "headers": {
            "User-Agent": user_agent_val,
        },
        "initiatingScriptIds": ["script_1"],
    }

    res2 = correlate_apis_and_requests(
        api_events=api_events,
        network_requests=[req_with_script_transmission],
        raw_api_values=raw_vals,
    )

    assoc2 = res2["accepted_associations"][0]
    # Script-controlled component transmitted the value!
    assert assoc2["value_match_type"] == "exact_value_match"
    assert res2["summary"]["direct_value_matches_count"] == 1


# ============================================================================
# 6. Event Count Reconciliation
# ============================================================================

def test_event_count_reconciliation():
    """Verify that observed == persisted + deduplicated + dropped + sampled strictly holds."""
    collector = APICallCollector()
    collector.init(output_dir="/tmp", logger=logging.getLogger("test"), url_hash="test")

    # 1. Record 2 valid, persisted calls
    collector.record_api_access(api_name="document.cookie", operation_type="property_get", source_script="https://a.com/1.js", script_id="1")
    collector.record_api_access(api_name="window.localStorage", operation_type="property_get", source_script="https://a.com/2.js", script_id="2")

    # 2. Record 1 duplicate call (same key within bucket)
    collector.record_api_access(api_name="document.cookie", operation_type="property_get", source_script="https://a.com/1.js", script_id="1")

    # 3. Record 1 stack-conflict drop
    collector.record_api_access(
        api_name="document.cookie",
        operation_type="property_get",
        source_script="https://a.com/3.js",
        call_stack="Error\n    at CanvasRenderingContext2D.getImageData (https://a.com/3.js:5:1)",
    )

    # 4. Record 1 uncapturable drop
    collector.record_api_access(api_name="window.name", captured=False, drop_reason="capture_disabled")

    summary = collector.get_partial_results()["collectionSummary"]

    obs = summary["totalEventsObserved"]
    pers = summary["totalEventsPersisted"]
    dedup = summary["totalEventsDeduplicated"]
    drop = summary["totalEventsDropped"]
    samp = summary["totalEventsSampled"]

    assert obs == 5
    assert pers == 2
    assert dedup == 1
    assert drop == 2
    assert samp == 0

    # Mathematical identity must hold:
    assert obs == pers + dedup + drop + samp, "Observed count must strictly equal sum of parts"


# ============================================================================
# 7. Disclosure NLP Taxonomy and Alignment Results
# ============================================================================

def test_disclosure_nlp_taxonomy_and_alignment():
    """Verify NLP disclosure taxonomy extraction and visit-level alignment metrics."""
    raw_disclosure = """
    Why you're seeing this ad:
    - This ad is based on the website content you are viewing.
    - An advertiser determined this ad was relevant based on your general location (city: Boston).
    - Information about your device and browser activity was used to show this ad.
    """

    # Test statement classification
    c1 = classify_statement_nlp("this ad is based on the website content you are viewing")
    assert c1["theme"] == "Contextual Website Content"
    assert "Contextual page content" in c1["strict_categories"]

    c2 = classify_statement_nlp("an advertiser determined this ad was relevant based on your general location")
    assert c2["theme"] == "Time of Day & General Location"
    assert "Location" in c2["strict_categories"]
    assert c2["attribution"] == "Advertiser"

    c3 = classify_statement_nlp("information about your device and browser activity was used")
    assert "Browser identity" in c3["strict_categories"]
    # Lenient interpretation expands to hardware/screen
    assert "Hardware indicators" in c3["lenient_categories"]
    assert "Screen and viewport" in c3["lenient_categories"]

    # Test visit-level alignment calculation
    mock_observed_apis = [
        {"api_name": "Navigator.prototype.userAgent", "api_category": "Browser identity"},
        {"api_name": "Screen.prototype.width", "api_category": "Screen and viewport"},
        {"api_name": "AudioContext.prototype.createOscillator", "api_category": "Audio"},
    ]

    mock_statements = [
        {
            "statement_id": "stmt_01",
            "acknowledged_technical_data_categories_strict": ["Browser identity"],
            "acknowledged_technical_data_categories_lenient": ["Browser identity", "Screen and viewport"],
        }
    ]

    alignment = compute_visit_disclosure_alignment(
        observed_api_events=mock_observed_apis,
        disclosure_statements=mock_statements,
        website="https://boston.com",
    )

    # Observed categories = {Browser identity, Screen and viewport, Audio} (3)
    # Strict disclosed = {Browser identity} (1) -> Strict intersection = {Browser identity} (1) -> coverage = 1/3 ~ 33.3%
    assert alignment["observed_categories_count"] == 3
    assert alignment["strict_intersection_count"] == 1
    assert alignment["strict_coverage_rate"] == round(1 / 3, 4)

    # Lenient disclosed = {Browser identity, Screen and viewport} (2) -> Lenient intersection = 2 -> coverage = 2/3 ~ 66.7%
    assert alignment["lenient_intersection_count"] == 2
    assert alignment["lenient_coverage_rate"] == round(2 / 3, 4)

    # Disclosed categories breakdown
    assert alignment["acknowledged_and_observed"] == ["Browser identity"]
    assert sorted(alignment["observed_without_disclosure"]) == ["Audio", "Screen and viewport"]


# ============================================================================
# 8. Privacy Sanitization (URLs, Headers, Tokens, DOM)
# ============================================================================

def test_privacy_sanitization():
    """Verify that secrets, tokens, passwords, and DOM injection vectors are sanitized."""
    # 1. URL sanitization
    raw_url = "https://user:secretpass@tracker.example.com/pixel?user_id=123&token=abc123xyz456&session=sess789"
    clean_url = sanitize_url(raw_url)
    assert "secretpass" not in clean_url
    assert "token=[REDACTED]" in clean_url
    assert "session=[REDACTED]" in clean_url
    assert "user_id=123" in clean_url

    # 2. Header sanitization
    raw_headers = {
        "User-Agent": "Mozilla/5.0",
        "Authorization": "Bearer super_secret_jwt_token_12345",
        "Cookie": "session_id=abcdef123456",
        "X-Custom-Tracking": "value_ok",
    }
    clean_headers = sanitize_headers(raw_headers)
    assert clean_headers["Authorization"] == "[REDACTED]"
    assert clean_headers["Cookie"] == "[REDACTED]"
    assert clean_headers["User-Agent"] == "Mozilla/5.0"
    assert clean_headers["X-Custom-Tracking"] == "value_ok"

    # 3. DOM field sanitization
    raw_dom = (
        "<div><h1>Ad Title</h1>"
        "<script>alert('xss');</script>"
        "<p>Password: secret_user_pass123</p>"
        "<style>body { background: red; }</style></div>"
    )
    clean_dom = sanitize_dom_text(raw_dom)
    assert "<script>" not in clean_dom
    assert "<style>" not in clean_dom
    assert "alert" not in clean_dom
    assert "secret_user_pass123" not in clean_dom


# ============================================================================
# 9. Separate Accepted and Rejected Candidate Associations
# ============================================================================

def test_separate_accepted_and_rejected_associations():
    """Verify accepted pairs and rejected candidate pairs are partitioned with rejection reasons."""
    t0 = 1000

    api_events = [
        # API 1: Qualified for primary analysis
        {
            "api_event_id": "api_valid",
            "timestamp_ms": t0,
            "phase": "page_load",
            "script_id": "script_ok",
            "frame_id": "frame_ok",
            "document_id": "doc_1",
        },
        # API 2: Incompatible phase (disclosure_interaction)
        {
            "api_event_id": "api_phase_bad",
            "timestamp_ms": t0,
            "phase": "disclosure_interaction",
            "script_id": "script_ok",
            "frame_id": "frame_ok",
            "document_id": "doc_1",
        },
    ]

    requests = [
        # Request 1: Exact match with API 1
        {
            "requestId": "req_1",
            "timestamp_ms": t0 + 200,
            "phase": "page_load",
            "frameId": "frame_ok",
            "document_id": "doc_1",
            "initiatingScriptIds": ["script_ok"],
        },
        # Request 2: Different unlinked frame & no script
        {
            "requestId": "req_unlinked",
            "timestamp_ms": t0 + 300,
            "phase": "page_load",
            "frameId": "frame_other",
            "document_id": "doc_1",
            "initiatingScriptIds": ["script_other"],
        },
    ]

    res = correlate_apis_and_requests(api_events=api_events, network_requests=requests)

    accepted = res["accepted_associations"]
    rejected = res["rejected_candidate_associations"]

    # Exact script & frame pair is accepted
    assert len(accepted) == 1
    assert accepted[0]["api_event_id"] == "api_valid"
    assert accepted[0]["request_id"] == "req_1"
    assert accepted[0]["rejection_reason"] is None

    # Incompatible phase & unlinked requests are in rejected_candidate_associations
    assert len(rejected) >= 2
    reasons = [r["rejection_reason"] for r in rejected]
    assert any("phase_incompatible" in r for r in reasons if r)
    assert any("unlinked" in r for r in reasons if r)


# ============================================================================
# 10. Synthetic End-to-End Crawl Scenario
# ============================================================================

def test_synthetic_end_to_end_crawl():
    """Verify full end-to-end integration on a synthetic page crawl result."""
    t_start = 5000
    ctx = CrawlContext(crawl_id="crawl_syn_01", website_id="syn_site", attempt_id="att_001")

    syn_result = {
        "url": "https://publisher.com/news",
        "publisher_domain": "publisher.com",
        "document_id": ctx.document_id,
        "attempt_id": ctx.attempt_id,
        "data": {
            "AdCollector": {
                "main_frame_id": "frame_main",
                "adAttrs": [
                    {
                        "ad_impression_id": "ad_imp_01",
                        "frame_id": "frame_ad_slot",
                        "adDisclosureText": "Why this ad: Based on your general location and website content.",
                        "adLinksAndImages": [{"frameId": "frame_ad_slot"}],
                    }
                ],
            },
            "APICallCollector": {
                "savedCalls": [
                    {
                        "api_event_id": "evt_01",
                        "api_name": "document.cookie",
                        "api_category": "Cookies and identifiers",
                        "operation_type": "property_get",
                        "timestamp_ms": t_start,
                        "phase": "page_load",
                        "frame_id": "frame_main",
                        "parent_frame_id": None,
                        "script_id": "pub_script",
                    },
                    {
                        "api_event_id": "evt_02",
                        "api_name": "CanvasRenderingContext2D.prototype.getImageData",
                        "api_category": "Graphics and rendering",
                        "operation_type": "method_call",
                        "timestamp_ms": t_start + 100,
                        "phase": "page_load",
                        "frame_id": "frame_ad_slot",
                        "parent_frame_id": "frame_main",
                        "script_id": "ad_script",
                    },
                ],
                "collectionSummary": {
                    "totalEventsObserved": 2,
                    "totalEventsPersisted": 2,
                    "totalEventsDeduplicated": 0,
                    "totalEventsDropped": 0,
                    "totalEventsSampled": 0,
                },
            },
            "RequestCollector": [
                {
                    "requestId": "req_ad_beacon",
                    "timestamp_ms": t_start + 150,
                    "phase": "page_load",
                    "frameId": "frame_ad_slot",
                    "url": "https://adnetwork.com/beacon?canvas_fp=rendered_hash_value_999",
                    "initiatingScriptIds": ["ad_script"],
                }
            ],
            "AdDisclosureCollector": {
                "attempts": [
                    {
                        "ad_impression_id": "ad_imp_01",
                        "control_detected": True,
                        "interaction_attempted": True,
                        "interaction_succeeded": True,
                        "text_extracted": True,
                    }
                ]
            },
        },
    }

    # 1. Correlate frames and annotate events
    correlate_and_annotate_events(syn_result)

    calls = syn_result["data"]["APICallCollector"]["savedCalls"]
    # Main frame call is page_shared
    assert calls[0]["attribution_scope"] == "page_shared"
    # Ad slot call is single_ad
    assert calls[1]["attribution_scope"] == "single_ad"
    assert calls[1]["related_ad_ids"] == ["ad_imp_01"]

    # 2. Correlate APIs and Requests
    raw_vals = {"evt_02": ["rendered_hash_value_999"]}
    assoc_res = correlate_apis_and_requests(
        api_events=calls,
        network_requests=syn_result["data"]["RequestCollector"],
        raw_api_values=raw_vals,
    )
    assert len(assoc_res["accepted_associations"]) == 1
    assert assoc_res["accepted_associations"][0]["value_match_type"] == "exact_value_match"
    assert len(assoc_res["rejected_candidate_associations"]) >= 1

    # 3. Process Disclosures NLP & Alignment
    disc_nlp_res = process_visit_disclosures(syn_result)
    assert len(disc_nlp_res["statements"]) >= 1
    alignment = disc_nlp_res["alignment"]
    assert alignment["observed_categories_count"] == 2
    assert "Graphics and rendering" in alignment["observed_categories"]
    assert "Cookies and identifiers" in alignment["observed_categories"]
    assert alignment["disclosures_extracted"] is True
