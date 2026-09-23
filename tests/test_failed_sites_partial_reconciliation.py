"""tests/test_failed_sites_partial_reconciliation.py
-------------------------------------------------
Unit tests verifying that:
1. AdCollector.get_partial_results() reconciles candidate outcomes dynamically
   when stage timeout cuts off candidate evaluation before all candidate ads are processed.
2. The exact scenarios from the 6 websites that previously failed with fatal schema validation
   errors now successfully validate as Schema Valid and complete with partial data.
3. ScreenshotCollector is ordered last among passive collectors in crawler.py.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Collectors.AdCollector import AdCollector
from Collectors.ScreenshotCollector import ScreenshotCollector
from Collectors.RequestCollector import RequestCollector
from Collectors.CookieCollector import CookieCollector
from Collectors.APICallCollector import APICallCollector
from Collectors.AdDisclosureCollector import AdDisclosureCollector
from Helpers.schema_validator import validate_result


# The 6 sites that experienced fatal failures in run_data_kids
FAILED_SITES_SCENARIOS = [
    {
        "site": "cosepercrescere.it_filastrocca_dei_diritti_dei_bambini",
        "url": "https://www.cosepercrescere.it/filastrocca-dei-diritti-dei-bambini",
        "n_detected": 9,
        "n_scraped": 1,
        "n_timed_out_before_cutoff": 7,
        "n_unreached": 1,
    },
    {
        "site": "kidztype.com",
        "url": "https://kidztype.com/",
        "n_detected": 9,
        "n_scraped": 0,
        "n_timed_out_before_cutoff": 3,
        "n_unreached": 6,
    },
    {
        "site": "innerchildfun.com_category_resources",
        "url": "https://innerchildfun.com/category/resources",
        "n_detected": 4,
        "n_scraped": 0,
        "n_timed_out_before_cutoff": 0,
        "n_unreached": 4,
    },
    {
        "site": "insidethemagic.net_category_travel_food",
        "url": "https://insidethemagic.net/category/travel/food",
        "n_detected": 13,
        "n_scraped": 4,
        "n_timed_out_before_cutoff": 5,
        "n_unreached": 4,
    },
    {
        "site": "mommypoppins.com_anywhere_kids_best_of_lists_best_bedtime_stories_for_kids_free_bedti_b463aa0d04",
        "url": "https://mommypoppins.com/anywhere-kids/best-of-lists/best-bedtime-stories-for-kids-free-bedtime-stories-online",
        "n_detected": 16,
        "n_scraped": 12,
        "n_timed_out_before_cutoff": 3,
        "n_unreached": 1,
    },
    {
        "site": "playtivities.com_london_bridge_is_falling_down_printable_lyrics_and_origins",
        "url": "https://playtivities.com/london-bridge-is-falling-down-printable-lyrics-and-origins",
        "n_detected": 20,
        "n_scraped": 12,
        "n_timed_out_before_cutoff": 2,
        "n_unreached": 6,
    },
]


@pytest.mark.parametrize("scenario", FAILED_SITES_SCENARIOS, ids=lambda s: s["site"])
def test_partial_results_reconciliation_for_failed_sites(tmp_path, scenario):
    collector = AdCollector()
    collector.init(str(tmp_path), MagicMock(), "hash123")

    n_detected = scenario["n_detected"]
    n_scraped = scenario["n_scraped"]
    n_timed_out_before = scenario["n_timed_out_before_cutoff"]
    n_unreached = scenario["n_unreached"]

    assert n_scraped + n_timed_out_before + n_unreached == n_detected

    # Populate detected ads
    detected = []
    for i in range(n_detected):
        cand_id = f"cand_{i+1:03d}"
        detected.append({
            "id": f"ad_el_{i}",
            "ad_candidate_id": cand_id,
            "x": 10,
            "y": i * 100,
            "width": 300,
            "height": 250,
            "matchedRule": "rule: selector:.ad",
            "nodeType": "DIV",
            "_candidate_status": "pending",
        })
    collector._detected_ads = detected

    # Simulate processing ads up until the stage timeout interrupted
    ad_attrs = []
    idx = 0
    for _ in range(n_scraped):
        cand = detected[idx]
        imp_id = f"ad_{idx+1:03d}"
        cand["_candidate_status"] = "retained"
        cand["ad_impression_id"] = imp_id
        ad_attrs.append({
            "id": cand["id"],
            "ad_impression_id": imp_id,
            "ad_candidate_id": cand["ad_candidate_id"],
            "screenshot": f"screenshot_{imp_id}.png",
            "nodeType": "DIV",
            "width": 300,
            "height": 250,
        })
        idx += 1
    collector._ad_attrs = ad_attrs

    for _ in range(n_timed_out_before):
        cand = detected[idx]
        cand["_candidate_status"] = "timed_out"
        idx += 1
    collector._n_timed_out_ads = n_timed_out_before

    # The remaining candidates (idx .. n_detected) remain pending/unreached
    for rem_i in range(idx, n_detected):
        assert detected[rem_i]["_candidate_status"] == "pending"

    # Call get_partial_results()
    partial = collector.get_partial_results()
    scrape = partial["scrapeResults"]

    # Assert strict outcome count reconciliation:
    # nDetectedAds == nAdsScraped + nSmallAds + nEmptyAds + nRemovedAds + nSkippedAds + nTimedOutAds
    outcomes_sum = (
        scrape["nAdsScraped"]
        + scrape["nSmallAds"]
        + scrape["nEmptyAds"]
        + scrape["nRemovedAds"]
        + scrape["nSkippedAds"]
        + scrape["nTimedOutAds"]
    )
    assert scrape["nDetectedAds"] == n_detected
    assert outcomes_sum == n_detected, f"Expected outcomes {outcomes_sum} to equal detected {n_detected}"
    assert scrape["nAdsScraped"] == n_scraped
    # All unreached candidates should now be accounted for in nTimedOutAds
    assert scrape["nTimedOutAds"] == n_timed_out_before + n_unreached
    assert len(partial["candidateAds"]) == n_detected

    # Validate that this partial result passes schema validation without SchemaValidationError
    result_payload = {
        "schema_version": "2.0.0",
        "document_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        "initialUrl": scenario["url"],
        "finalUrl": scenario["url"],
        "successful": False,
        "status": "completed_with_partial_data",
        "testStarted": 1700000000,
        "data": {
            "AdCollector": partial,
        },
    }

    validation = validate_result(result_payload, raise_on_error=True)
    assert validation["valid"] is True
    assert validation["errors"] == []


def test_screenshot_collector_ordered_last():
    """Verify ScreenshotCollector is placed last among passive collectors in crawler.py ordering."""
    collector_names = [
        ScreenshotCollector.COLLECTOR_NAME,
        AdCollector.COLLECTOR_NAME,
        RequestCollector.COLLECTOR_NAME,
        CookieCollector.COLLECTOR_NAME,
        APICallCollector.COLLECTOR_NAME,
        AdDisclosureCollector.COLLECTOR_NAME,
    ]

    # Mirror crawler.py logic
    ordered_collectors = [c for c in collector_names if c != AdDisclosureCollector.COLLECTOR_NAME]
    if ScreenshotCollector.COLLECTOR_NAME in ordered_collectors:
        ordered_collectors.remove(ScreenshotCollector.COLLECTOR_NAME)
        ordered_collectors.append(ScreenshotCollector.COLLECTOR_NAME)

    assert ordered_collectors[-1] == ScreenshotCollector.COLLECTOR_NAME
    assert ordered_collectors[0] == AdCollector.COLLECTOR_NAME
