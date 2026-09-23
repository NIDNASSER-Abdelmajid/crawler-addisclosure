"""Unit tests for Helpers/frame_correlator.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Helpers.frame_correlator import build_frame_correlation_index, correlate_and_annotate_events


def test_frame_correlator_empty():
    res = correlate_and_annotate_events({})
    assert res == {}



def test_frame_correlator_cross_collector_linkage():
    sample_result = {
        "data": {
            "RequestCollector": [
                {
                    "url": "https://ads.network.com/ad.js",
                    "frameId": "F1",
                    "loaderId": "L1",
                    "initiatingScriptIds": ["S100"],
                    "event_seq": 10,
                },
                {
                    "url": "https://tracker.com/pixel.gif",
                    "frameId": "F1",
                    "initiatingScriptIds": ["S100"],
                    "event_seq": 15,
                },
            ],
            "APICallCollector": {
                "savedCalls": [
                    {
                        "source": "https://ads.network.com/ad.js",
                        "frame_id": "F1",
                        "script_id": "S100",
                        "description": "window.localStorage",
                        "event_seq": 20,
                    }
                ]
            },
            "FingerprintCollector": {
                "savedCalls": [
                    {
                        "source": "https://ads.network.com/ad.js",
                        "frame_id": "F1",
                        "description": "HTMLCanvasElement.toDataURL",
                        "event_seq": 25,
                    }
                ]
            },
            "CookieCollector": [
                {
                    "domain": ".network.com",
                    "name": "id",
                    "event_seq": 30,
                }
            ],
            "AdCollector": {
                "adAttrs": [
                    {
                        "ad_impression_id": "ad_001",
                        "frame_id": "F1",
                        "loader_id": "L1",
                        "adLinksAndImages": [
                            {
                                "frameId": "F1",
                                "loaderId": "L1",
                                "scriptIds": ["S100"],
                                "iframes": [{"frameId": "F1_child"}],
                            }
                        ],
                    }
                ]
            },
        }
    }

    index = correlate_and_annotate_events(sample_result)

    assert "frame:F1" in index

    frame_info = index["frame:F1"]
    assert 10 in frame_info["requests"]
    assert 15 in frame_info["requests"]
    assert 20 in frame_info["api_calls"]
    assert 25 in frame_info["fingerprint_calls"]
    assert "ad_001" in frame_info["related_ad_ids"]
    assert frame_info["link_confidence"] == "high"
    assert frame_info["ambiguous"] is False

    # Verify event annotations
    req0 = sample_result["data"]["RequestCollector"][0]
    assert req0["related_ad_ids"] == ["ad_001"]
    assert req0["link_confidence"] == "high"
    assert "frame_hierarchy:F1" in req0["link_evidence"]

    api0 = sample_result["data"]["APICallCollector"]["savedCalls"][0]
    assert api0["related_ad_ids"] == ["ad_001"]
    assert api0["link_confidence"] == "high"
