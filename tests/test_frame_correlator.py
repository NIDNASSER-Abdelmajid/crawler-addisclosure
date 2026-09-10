"""Unit tests for Helpers/frame_correlator.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Helpers.frame_correlator import build_frame_correlation_index


def test_frame_correlator_empty():
    res = build_frame_correlation_index({})
    assert res == {}


def test_frame_correlator_cross_collector_linkage():
    sample_result = {
        "data": {
            "RequestCollector": [
                {
                    "url": "https://ads.network.com/ad.js",
                    "initiators": ["https://publisher.com/article"],
                    "event_seq": 10,
                },
                {
                    "url": "https://tracker.com/pixel.gif",
                    "initiators": ["https://ads.network.com/ad.js"],
                    "event_seq": 15,
                },
            ],
            "APICallCollector": {
                "savedCalls": [
                    {
                        "source": "https://ads.network.com/ad.js",
                        "description": "window.localStorage",
                        "event_seq": 20,
                    }
                ]
            },
            "FingerprintCollector": {
                "savedCalls": [
                    {
                        "source": "https://ads.network.com/ad.js",
                        "frame_url": "https://publisher.com/article",
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
                        "id": "ad_001",
                        "adLinksAndImages": [
                            {
                                "iframes": [{"src": "https://ads.network.com/ad.js"}],
                                "imgs": [{"src": "https://tracker.com/pixel.gif"}],
                            }
                        ],
                    }
                ]
            },
        }
    }

    index = build_frame_correlation_index(sample_result)

    assert "https://ads.network.com/ad.js" in index
    ad_frame_info = index["https://ads.network.com/ad.js"]
    assert 10 in ad_frame_info["requests"]
    assert 15 in ad_frame_info["requests"]  # via initiator
    assert 20 in ad_frame_info["api_calls"]
    assert 25 in ad_frame_info["fingerprint_calls"]
    assert "ad_001" in ad_frame_info["ad_impression_ids"]

    assert "network.com" in index
    assert 30 in index["network.com"]["cookies_set"]

    assert "https://tracker.com/pixel.gif" in index
    assert "ad_001" in index["https://tracker.com/pixel.gif"]["ad_impression_ids"]
