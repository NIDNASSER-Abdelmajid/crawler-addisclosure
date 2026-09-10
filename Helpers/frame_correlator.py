"""Helpers/frame_correlator.py — Cross-collector correlation by frame / URL.

Enables post-hoc linkage of requests, API calls, fingerprinting events,
cookies, and ad impressions back to the common execution frames and script URLs.
"""

from __future__ import annotations

from typing import Any


def _normalize_key(url: str | None) -> str:
    """Normalize a URL or domain key by removing fragment and leading/trailing whitespace."""
    if not url:
        return ""
    return str(url).split("#")[0].strip()


def build_frame_correlation_index(result: dict[str, Any]) -> dict[str, Any]:
    """Correlate events across collectors by frame URL, script source, or domain.

    Returns a mapping of URL/origin -> {
        "requests": list[int],            # event_seq values
        "api_calls": list[int],           # event_seq values
        "fingerprint_calls": list[int],   # event_seq values
        "cookies_set": list[int],         # event_seq values
        "ad_impression_ids": list[str],   # ad id strings
    }
    """
    index: dict[str, dict[str, list]] = {}

    def _ensure_entry(u: str) -> dict[str, list]:
        if u not in index:
            index[u] = {
                "requests": [],
                "api_calls": [],
                "fingerprint_calls": [],
                "cookies_set": [],
                "ad_impression_ids": [],
            }
        return index[u]

    data = result.get("data", {})
    if not isinstance(data, dict):
        return {}

    # 1. Requests
    requests = data.get("RequestCollector", [])
    if isinstance(requests, list):
        for req in requests:
            if not isinstance(req, dict):
                continue
            u = _normalize_key(req.get("url"))
            seq = req.get("event_seq")
            if u and seq is not None:
                _ensure_entry(u)["requests"].append(seq)
            # Also associate with initiators
            for init_url in req.get("initiators", []):
                init_norm = _normalize_key(init_url)
                if init_norm and seq is not None:
                    _ensure_entry(init_norm)["requests"].append(seq)

    # 2. API calls
    api_collector = data.get("APICallCollector", {})
    if isinstance(api_collector, dict):
        for call in api_collector.get("savedCalls", []):
            if not isinstance(call, dict):
                continue
            u = _normalize_key(call.get("source"))
            seq = call.get("event_seq")
            if u and seq is not None:
                _ensure_entry(u)["api_calls"].append(seq)

    # 3. Fingerprint calls
    fp_collector = data.get("FingerprintCollector", {})
    if isinstance(fp_collector, dict):
        for call in fp_collector.get("savedCalls", []):
            if not isinstance(call, dict):
                continue
            seq = call.get("event_seq")
            src = _normalize_key(call.get("source"))
            frame_url = _normalize_key(call.get("frame_url"))
            if src and seq is not None:
                _ensure_entry(src)["fingerprint_calls"].append(seq)
            if frame_url and frame_url != src and seq is not None:
                _ensure_entry(frame_url)["fingerprint_calls"].append(seq)

    # 4. Cookies
    cookies = data.get("CookieCollector", [])
    if isinstance(cookies, list):
        for c in cookies:
            if not isinstance(c, dict):
                continue
            seq = c.get("event_seq")
            domain = c.get("domain", "").lstrip(".")
            if domain and seq is not None:
                _ensure_entry(domain)["cookies_set"].append(seq)

    # 5. AdCollector impressions
    ad_data = data.get("AdCollector", {})
    if isinstance(ad_data, dict):
        for ad in ad_data.get("adAttrs", []):
            if not isinstance(ad, dict):
                continue
            ad_id = str(ad.get("id") or "")
            for frame in ad.get("adLinksAndImages", []):
                if not isinstance(frame, dict):
                    continue
                for ifr in frame.get("iframes", []):
                    if isinstance(ifr, dict):
                        src = _normalize_key(ifr.get("src"))
                        if src and ad_id:
                            entry = _ensure_entry(src)
                            if ad_id not in entry["ad_impression_ids"]:
                                entry["ad_impression_ids"].append(ad_id)
                for item in (frame.get("imgs", []) or []) + (frame.get("videos", []) or []):
                    if isinstance(item, dict):
                        src = _normalize_key(item.get("src"))
                        if src and ad_id:
                            entry = _ensure_entry(src)
                            if ad_id not in entry["ad_impression_ids"]:
                                entry["ad_impression_ids"].append(ad_id)

    # Deduplicate and sort sequence numbers in each bucket
    for u, buckets in index.items():
        buckets["requests"] = sorted(set(buckets["requests"]))
        buckets["api_calls"] = sorted(set(buckets["api_calls"]))
        buckets["fingerprint_calls"] = sorted(set(buckets["fingerprint_calls"]))
        buckets["cookies_set"] = sorted(set(buckets["cookies_set"]))

    return index
