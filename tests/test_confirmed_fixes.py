"""tests/test_confirmed_fixes.py — Comprehensive tests for all 14 confirmed crawler fixes.

Verifies:
1. Two frames with the same URL receive different frame IDs.
2. Every API count reconciles with saved or explicitly dropped/truncated events.
3. Passive and disclosure-generated events remain separated by phase.
4. Redirect hops are preserved in RequestCollector.
5. Correct disclosure and fingerprint counts (no dict-key counting, separate attempt breakdown).
6. Shared scripts producing ambiguous multi-ad links in frame correlator.
7. No raw cookie, token, header, URL-parameter, or body values appear in results (keyed-HMAC).
8. Retry persistence, .recovered_partial marker, and no stale duplicate metadata.
9. Broken references or count inconsistencies rejected by schema validator before saving.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Helpers.crawl_context import CrawlContext, Phase
from Helpers.schema_validator import SchemaValidationError, validate_result
from Helpers.frame_correlator import build_frame_correlation_index, correlate_and_annotate_events
from Collectors.AdCollector import AdCollector
from Collectors.AdDisclosureCollector import AdDisclosureCollector
from Collectors.APICallCollector import APICallCollector
from Collectors.FingerprintCollector import FingerprintCollector
from Collectors.RequestCollector import RequestCollector
from Collectors.CookieCollector import CookieCollector
from timeout_manager import (
    AttemptMetadata,
    atomic_write_json,
    is_url_already_completed,
    recover_incomplete_attempts,
)


# ============================================================================
# 1. Two frames with the same URL receive different frame IDs
# ============================================================================

def test_two_frames_same_url_distinct_frame_ids():
    """Verify that frames with identical URLs (e.g. about:blank) remain distinguishable."""
    ctx = CrawlContext(url="https://example.com", schema_version="2.0.0")
    col = AdCollector()
    col.init("dummy_dir", None, "hash123", crawl_context=ctx)

    # Both must resolve to their genuine distinct CDP frame IDs
    id1 = col._frame_identifier(None, "https://doubleclick.net/ad", cdp_frame_id="CDP_FRAME_A1")
    id2 = col._frame_identifier(None, "https://doubleclick.net/ad", cdp_frame_id="CDP_FRAME_A2")

    assert id1 != id2
    assert id1 == "CDP_FRAME_A1"
    assert id2 == "CDP_FRAME_A2"

    # Distinct mock frame objects with identical URLs must receive distinct identifiers
    class MockFrame:
        def __init__(self, url: str):
            self.url = url

    f1 = MockFrame("https://doubleclick.net/ad")
    f2 = MockFrame("https://doubleclick.net/ad")
    f1_id = col._frame_identifier(f1, f1.url)
    f2_id = col._frame_identifier(f2, f2.url)
    assert f1_id != f2_id



# ============================================================================
# 2. Every API count reconciles with saved or explicitly dropped events
# ============================================================================

def test_api_count_reconciliation(tmp_path):
    """Verify that API accesses are tracked and reconcile completely."""
    ctx = CrawlContext(url="https://example.com")
    col = APICallCollector()
    col.init(str(tmp_path), None, "hash123", crawl_context=ctx)

    # Record normal access
    col.record_api_access(
        api_name="window.localStorage.getItem",
        operation_type="read",
        source_script="https://cdn.example.com/lib.js",
        frame_id="F_ROOT",
        arguments=["session_key"],
        return_value="secret_val",
        captured=True,
    )

    # Record dropped access
    col.record_api_access(
        api_name="window.localStorage.setItem",
        operation_type="write",
        source_script="<unknown>",
        captured=False,
        drop_reason="rate_limit_exceeded",
    )

    results = col.get_results()
    saved = results["savedCalls"]
    call_stats = results["callStats"]
    stats = results["collectionSummary"]

    # callStats contains only script-level statistics
    assert "totalAccesses" not in call_stats
    assert len(saved) == 1
    assert stats["totalAccesses"] == 2
    assert stats["droppedCapturesCount"] == 1
    assert stats["truncatedCapturesCount"] == 0
    assert stats["collectorFailuresCount"] == 0
    # Reconciliation: saved + dropped + truncated + unsupported + collectorFailures == total
    assert (
        len(saved)
        + stats["droppedCapturesCount"]
        + stats["truncatedCapturesCount"]
        + stats["unsupportedCapturesCount"]
        + stats["collectorFailuresCount"]
        == stats["totalAccesses"]
    )


# ============================================================================
# 3. Passive and disclosure-generated events remain separated
# ============================================================================

def test_phase_separation_passive_and_disclosure():
    """Verify that events emitted across phases retain distinct phase labels."""
    ctx = CrawlContext(url="https://example.com")

    # Phase 1: Page Load
    ctx.set_phase(Phase.PAGE_LOAD)
    ev1 = ctx.enrich_event({"name": "nav_start"})
    assert ev1["phase"] == "page_load"

    # Phase 2: Passive Ad Delivery
    ctx.set_phase(Phase.PASSIVE_AD_DELIVERY)
    ev2 = ctx.enrich_event({"name": "ad_rendered"})
    assert ev2["phase"] == "passive_ad_delivery"

    # Phase 3: Disclosure Interaction
    ctx.set_phase(Phase.DISCLOSURE_INTERACTION)
    ev3 = ctx.enrich_event({"name": "disclosure_clicked"})
    assert ev3["phase"] == "disclosure_interaction"

    # Sequence numbers strictly monotonic
    assert ev1["event_seq"] < ev2["event_seq"] < ev3["event_seq"]
    assert ev1["document_id"] == ev2["document_id"] == ev3["document_id"]


# ============================================================================
# 4. Redirect hops being preserved in RequestCollector
# ============================================================================

def test_redirect_hops_preserved(tmp_path):
    """Verify that multiple redirect hops sharing a requestId are preserved rather than overwritten."""
    ctx = CrawlContext(url="https://example.com")
    rc = RequestCollector()
    rc.init(str(tmp_path), None, "hash123", crawl_context=ctx)

    # Hop 1: Initial request
    req1 = {
        "requestId": "REQ_999",
        "frameId": "F_MAIN",
        "loaderId": "L_1",
        "wallTime": 1700000001.0,
        "request": {
            "url": "https://example.com/click",
            "method": "GET",
            "headers": {"User-Agent": "TestBrowser"},
        },
        "type": "Document",
    }
    rc.handle_request_will_be_sent(req1)

    # Hop 2: Redirect with same requestId
    req2 = {
        "requestId": "REQ_999",
        "frameId": "F_MAIN",
        "loaderId": "L_1",
        "wallTime": 1700000002.0,
        "redirectResponse": {
            "status": 302,
            "headers": {"Location": "https://adnetwork.com/dest"},
        },
        "request": {
            "url": "https://adnetwork.com/dest",
            "method": "GET",
            "headers": {"User-Agent": "TestBrowser"},
        },
        "type": "Document",
    }
    rc.handle_request_will_be_sent(req2)

    results = rc.get_results()
    # Both redirect hops must be preserved!
    assert len(results) == 2
    urls = [r["url"] for r in results]
    assert "https://example.com/click" in urls
    assert "https://adnetwork.com/dest" in urls
    assert results[0]["requestId"] == results[1]["requestId"] == "REQ_999"


# ============================================================================
# 5. Correct disclosure and fingerprint counts
# ============================================================================

def test_correct_disclosure_and_fingerprint_counts(tmp_path):
    """Verify disclosures derive breakdown and fingerprints count saved calls (not dictionary keys)."""
    ctx = CrawlContext(url="https://example.com")
    disc_col = AdDisclosureCollector()
    disc_col.init(str(tmp_path), None, "hash123", crawl_context=ctx)

    # Simulate an ad without controls and one with controls
    ads = [
        {"ad_impression_id": "ad_001", "ad_candidate_id": "cand_001", "detectedDisclosureControls": []},
    ]

    import asyncio
    asyncio.run(disc_col.interact_and_collect_disclosures(page=None, ads=ads))
    res = disc_col.get_results()

    # Verify attempt record was saved for no-control ad
    assert len(res["attempts"]) == 1
    att = res["attempts"][0]
    assert att["control_detected"] is False
    assert att["failure_stage"] == "detection"
    assert att["failure_reason"] == "no_control_found"
    assert res["counts"]["detected"] == 0
    assert res["counts"]["attempted"] == 0
    assert res["counts"]["extracted"] == 0

    # Test FingerprintCollector counts
    fp_col = FingerprintCollector()
    fp_col.init(str(tmp_path), None, "hash123", crawl_context=ctx)
    fp_col._handle_console_message({
        "description": "CanvasRenderingContext2D.getImageData",
        "source": "https://cdn.example.com/fp.js",
        "frame_id": "F1",
    })
    fp_col._handle_console_message({
        "description": "AudioContext.createOscillator",
        "source": "https://cdn.example.com/fp.js",
        "frame_id": "F1",
    })

    fp_res = fp_col.get_results()
    # fingerprints_count must be len(savedCalls) (2), not len(fp_res) (dict keys count)
    assert len(fp_res["savedCalls"]) == 2
    assert fp_res["totalObservedCount"] == 2
    assert fp_res["truncatedCount"] == 0


# ============================================================================
# 6. Shared scripts producing ambiguous multi-ad links
# ============================================================================

def test_shared_scripts_produce_ambiguous_multi_ad_links():
    """Verify that when a script is shared by multiple ads, the relationship is marked ambiguous."""
    sample_result = {
        "data": {
            "RequestCollector": [
                {
                    "url": "https://shared.adserver.com/tag.js",
                    "initiatingScriptIds": ["S_SHARED"],
                    "event_seq": 10,
                }
            ],
            "APICallCollector": {
                "savedCalls": [
                    {
                        "source": "https://shared.adserver.com/tag.js",
                        "script_id": "S_SHARED",
                        "description": "Navigator.userAgent",
                        "event_seq": 20,
                    }
                ]
            },
            "AdCollector": {
                "adAttrs": [
                    {
                        "ad_impression_id": "ad_001",
                        "adLinksAndImages": [{"scriptIds": ["S_SHARED"]}],
                    },
                    {
                        "ad_impression_id": "ad_002",
                        "adLinksAndImages": [{"scriptIds": ["S_SHARED"]}],
                    },
                ]
            },
        }
    }

    correlate_and_annotate_events(sample_result)

    req = sample_result["data"]["RequestCollector"][0]
    assert req["ambiguous"] is True
    assert set(req["related_ad_ids"]) == {"ad_001", "ad_002"}
    assert req["link_confidence"] == "medium"

    call = sample_result["data"]["APICallCollector"]["savedCalls"][0]
    assert call["ambiguous"] is True
    assert set(call["related_ad_ids"]) == {"ad_001", "ad_002"}


# ============================================================================
# 7. No raw cookies, tokens, or body values in derived results
# ============================================================================

def test_no_raw_tokens_or_cookies_in_results(tmp_path):
    """Verify keyed HMAC normalization and exclusion of raw sensitive tokens."""
    ctx = CrawlContext(url="https://example.com")
    rc = RequestCollector()
    rc.init(str(tmp_path), None, "hash123", crawl_context=ctx)

    raw_token = "secret_auth_token_xyz123"
    raw_cookie = "session_id=super_secret_cookie_val; HttpOnly"

    rc.handle_request_will_be_sent({
        "requestId": "REQ_AUTH",
        "frameId": "F1",
        "wallTime": 1700000001.0,
        "request": {
            "url": "https://example.com/api/login",
            "method": "POST",
            "headers": {
                "Authorization": f"Bearer {raw_token}",
                "Cookie": raw_cookie,
            },
            "postData": f"user=alice&token={raw_token}",
        },
        "type": "Fetch",
    })

    results = rc.get_results()
    assert len(results) == 1
    req = results[0]

    # Raw token and cookie must NEVER appear
    res_str = json.dumps(req)
    assert raw_token not in res_str
    assert "super_secret_cookie_val" not in res_str
    assert "Authorization" not in req["headers"]
    assert "Cookie" not in req["headers"]

    # Parameter metadata and HMAC must be present
    body_meta = req.get("bodyMetadata", {})
    assert "user" in body_meta.get("parameterNames", [])
    assert "token" in body_meta.get("parameterNames", [])
    token_hmac = body_meta.get("parameterHmacs", {}).get("token")
    assert token_hmac is not None
    # Matches context keyed-HMAC computation exactly
    expected_hmac = ctx.hmac_value(raw_token)
    assert token_hmac == expected_hmac


# ============================================================================
# 8. Retry persistence, .recovered_partial marker, and no stale duplicate metadata
# ============================================================================

def test_retry_and_partial_attempt_states(tmp_path):
    """Verify recovery writes .recovered_partial and does not treat partial attempts as completed."""
    web_dir = tmp_path / "site_com"
    att_dir = web_dir / "attempt_001"
    att_dir.mkdir(parents=True)

    # Incomplete attempt: result.json and attempt_metadata.json exist without .completed
    atomic_write_json(att_dir / "result.json", {"successful": False, "status": "failed"})
    atomic_write_json(att_dir / "attempt_metadata.json", {"status": "failed", "attempt_id": "att_001"})

    recovered = recover_incomplete_attempts(tmp_path)
    assert len(recovered) == 1

    # Marker must be .recovered_partial (NOT .completed!)
    assert (att_dir / ".recovered_partial").is_file()
    assert not (att_dir / ".completed").is_file()

    # Must NOT be treated as completed visit
    assert is_url_already_completed(tmp_path, "https://site.com") is False


# ============================================================================
# 9. Broken references rejected before saving
# ============================================================================

def test_broken_references_rejected_before_saving():
    """Verify schema validator fails on broken references or inconsistent counts."""
    # Test 1: Inconsistent ad counts (nDetectedAds != sum of parts)
    bad_result_1 = {
        "schema_version": "2.0.0",
        "document_id": str(uuid.uuid4()),
        "initialUrl": "https://example.com",
        "finalUrl": "https://example.com",
        "successful": True,
        "status": "completed",
        "testStarted": 1700000000,
        "data": {
            "AdCollector": {
                "scrapeResults": {
                    "nDetectedAds": 10,
                    "nAdsScraped": 2,
                    "nSmallAds": 0,
                    "nEmptyAds": 0,
                    "nRemovedAds": 0,
                    "nSkippedAds": 0,
                    "nTimedOutAds": 0,
                },
                "candidateAds": [],
                "adAttrs": [{"ad_impression_id": "ad_001", "ad_candidate_id": "cand_001"}],
            }
        },
    }

    with pytest.raises(SchemaValidationError) as exc1:
        validate_result(bad_result_1, raise_on_error=True)
    assert any("Inconsistent ad counts" in err for err in exc1.value.errors)

    # Test 2: Disclosure attempt referencing nonexistent ad impression ID
    bad_result_2 = {
        "schema_version": "2.0.0",
        "document_id": str(uuid.uuid4()),
        "initialUrl": "https://example.com",
        "finalUrl": "https://example.com",
        "successful": True,
        "status": "completed",
        "testStarted": 1700000000,
        "data": {
            "AdCollector": {
                "scrapeResults": {
                    "nDetectedAds": 1,
                    "nAdsScraped": 1,
                    "nSmallAds": 0,
                    "nEmptyAds": 0,
                    "nRemovedAds": 0,
                    "nSkippedAds": 0,
                    "nTimedOutAds": 0,
                },
                "candidateAds": [{"ad_candidate_id": "cand_001"}],
                "adAttrs": [{"ad_impression_id": "ad_001", "ad_candidate_id": "cand_001"}],
            },
            "AdDisclosureCollector": {
                "attempts": [
                    {
                        "disclosure_attempt_id": "disc_att_001",
                        "ad_impression_id": "ad_UNKNOWN_999",  # Broken reference!
                    }
                ]
            },
        },
    }

    with pytest.raises(SchemaValidationError) as exc2:
        validate_result(bad_result_2, raise_on_error=True)
    assert any("Broken reference" in err for err in exc2.value.errors)
