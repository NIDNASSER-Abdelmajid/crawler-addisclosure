"""Helpers/frame_correlator.py — Cross-collector correlation by frame hierarchy and initiator chains.

Links API calls, network requests, and fingerprinting events to advertisements
ONLY through real frame relationships or recorded request/script initiator chains.
Exact-URL matching is strictly removed as evidence of ad ownership.
"""

from __future__ import annotations

from typing import Any


def correlate_and_annotate_events(result: dict[str, Any]) -> dict[str, Any]:
    """Link events to advertisements using real frame relationships and initiator chains.

    Each event is enriched with:
    - related_ad_ids: list[str] (0, 1, or multiple candidate ad impression IDs)
    - link_evidence: list[str] (identifiers and relationships supporting the link)
    - link_confidence: "high" | "medium" | "low"
    - ambiguous: bool (True when shared across multiple ads)

    Never forces an event onto an advertisement if no reliable relationship exists.
    """
    data = result.get("data", {})
    if not isinstance(data, dict):
        return {}

    # 1. Build ad frame and script ownership registries
    ad_data = data.get("AdCollector", {})
    ads = ad_data.get("adAttrs", []) if isinstance(ad_data, dict) else []

    frame_to_ads: dict[str, set[str]] = {}
    script_to_ads: dict[str, set[str]] = {}
    loader_to_ads: dict[str, set[str]] = {}

    for ad in ads:
        if not isinstance(ad, dict):
            continue
        ad_id = str(ad.get("ad_impression_id") or ad.get("id") or "")
        if not ad_id:
            continue

        # Check frame info stored in ad
        top_frame_id = ad.get("frame_id") or ad.get("frameId")
        if top_frame_id:
            frame_to_ads.setdefault(str(top_frame_id), set()).add(ad_id)

        top_loader_id = ad.get("loader_id") or ad.get("loaderId")
        if top_loader_id:
            loader_to_ads.setdefault(str(top_loader_id), set()).add(ad_id)

        for frame_entry in ad.get("adLinksAndImages", []):
            if not isinstance(frame_entry, dict):
                continue
            fid = frame_entry.get("frameId") or frame_entry.get("frame_id")
            if fid:
                frame_to_ads.setdefault(str(fid), set()).add(ad_id)

            lid = frame_entry.get("loaderId") or frame_entry.get("loader_id")
            if lid:
                loader_to_ads.setdefault(str(lid), set()).add(ad_id)

            for sid in frame_entry.get("scriptIds", []):
                if sid:
                    script_to_ads.setdefault(str(sid), set()).add(ad_id)

            for ifr in frame_entry.get("iframes", []):
                if isinstance(ifr, dict):
                    nested_fid = ifr.get("frameId") or ifr.get("frame_id")
                    if nested_fid:
                        frame_to_ads.setdefault(str(nested_fid), set()).add(ad_id)

    pub_domain = str(result.get("publisher_domain") or "").lower()
    if not pub_domain:
        try:
            from urllib.parse import urlparse
            p_url = result.get("initial_url") or result.get("url") or ""
            parsed = urlparse(p_url)
            netloc = parsed.netloc.split(":")[0].lower()
            parts = netloc.split(".")
            pub_domain = ".".join(parts[-2:]) if len(parts) >= 2 else netloc
        except Exception:
            pub_domain = ""

    main_frame_id = str(ad_data.get("main_frame_id") or ad_data.get("mainFrameId") or "")

    def _determine_link(
        frame_id: str | None = None,
        loader_id: str | None = None,
        script_ids: list[str] | None = None,
        initiator_stack: dict | None = None,
        is_page_context: bool = False,
        is_main_frame: bool = False,
        is_publisher: bool = False,
        is_isolated_ad_frame: bool = False,
    ) -> tuple[list[str], list[str], str, bool, str, bool]:
        matched_ads: set[str] = set()
        evidence: list[str] = []
        confidence = "low"

        # 1. Direct CDP frame hierarchy match -> high confidence
        if frame_id and str(frame_id) in frame_to_ads:
            matched_ads.update(frame_to_ads[str(frame_id)])
            evidence.append(f"frame_hierarchy:{frame_id}")
            confidence = "high"

        # 2. Loader ID match -> high confidence
        if loader_id and str(loader_id) in loader_to_ads:
            matched_ads.update(loader_to_ads[str(loader_id)])
            evidence.append(f"loader_hierarchy:{loader_id}")
            if confidence != "high":
                confidence = "high"

        # 3. Initiating script ID match -> medium confidence
        if script_ids:
            for sid in script_ids:
                if str(sid) in script_to_ads:
                    matched_ads.update(script_to_ads[str(sid)])
                    evidence.append(f"initiator_chain:script_{sid}")
                    if confidence == "low":
                        confidence = "medium"

        # 4. Initiator stack trace call frames
        if initiator_stack and isinstance(initiator_stack, dict):
            for frame in initiator_stack.get("callFrames", []):
                sid = frame.get("scriptId")
                if sid and str(sid) in script_to_ads:
                    matched_ads.update(script_to_ads[str(sid)])
                    evidence.append(f"initiator_stack:script_{sid}")
                    if confidence == "low":
                        confidence = "medium"

        candidate_ad_ids = sorted(matched_ads)

        # Treat main-frame and publisher activity as page_shared by default
        if (is_main_frame or is_publisher) and not is_isolated_ad_frame:
            attribution_scope = "page_shared"
            unique_ad_attribution = False
            ambiguous = len(candidate_ad_ids) > 1
            if not candidate_ad_ids:
                confidence = "low"
        elif len(candidate_ad_ids) == 1:
            attribution_scope = "single_ad"
            unique_ad_attribution = True
            ambiguous = False
        elif len(candidate_ad_ids) > 1:
            attribution_scope = "multiple_ads"
            unique_ad_attribution = False
            ambiguous = True
        else:
            attribution_scope = "page_shared" if (is_page_context or frame_id or is_main_frame or is_publisher) else "unlinked"
            unique_ad_attribution = False
            ambiguous = False
            confidence = "none" if attribution_scope == "unlinked" else "low"

        return candidate_ad_ids, evidence, confidence, ambiguous, attribution_scope, unique_ad_attribution

    # Annotate Requests
    requests = data.get("RequestCollector", [])
    if isinstance(requests, list):
        for req in requests:
            if not isinstance(req, dict):
                continue
            r_frame = req.get("frameId")
            r_loader = req.get("loaderId")
            r_scripts = req.get("initiatingScriptIds") or []
            r_stack = req.get("initiatorStack")
            r_url = str(req.get("url") or "").lower()

            try:
                from urllib.parse import urlparse
                r_netloc = urlparse(r_url).netloc.split(":")[0].lower()
                is_pub = bool(pub_domain and (r_netloc == pub_domain or r_netloc.endswith("." + pub_domain)))
            except Exception:
                is_pub = bool(pub_domain and pub_domain in r_url)

            is_isolated = bool(
                r_frame
                and str(r_frame) in frame_to_ads
                and (not main_frame_id or str(r_frame) != main_frame_id)
                and not req.get("is_main_frame")
            )
            is_main = bool(
                req.get("is_main_frame")
                or (main_frame_id and str(r_frame) == main_frame_id)
                or (
                    not is_isolated
                    and (
                        r_frame is None
                        or (req.get("parentFrameId") is None and str(r_frame) not in frame_to_ads)
                    )
                )
            )

            ad_ids, ev, conf, amb, scope, unique_attr = _determine_link(
                frame_id=r_frame,
                loader_id=r_loader,
                script_ids=r_scripts,
                initiator_stack=r_stack,
                is_main_frame=is_main,
                is_publisher=is_pub,
                is_isolated_ad_frame=is_isolated,
            )
            req["candidate_ad_ids"] = ad_ids
            req["related_ad_ids"] = ad_ids
            req["link_evidence"] = ev
            req["link_confidence"] = conf
            req["evidence_confidence"] = conf
            req["ambiguous"] = amb
            req["attribution_scope"] = scope
            req["unique_ad_attribution"] = unique_attr

    # Annotate API calls
    api_collector = data.get("APICallCollector", {})
    if isinstance(api_collector, dict):
        for call in api_collector.get("savedCalls", []):
            if not isinstance(call, dict):
                continue
            c_frame = call.get("frame_id")
            c_script = [str(call["script_id"])] if call.get("script_id") else []
            c_url = str(call.get("normalized_script_url") or call.get("source_script") or call.get("source") or "").lower()

            try:
                from urllib.parse import urlparse
                c_netloc = urlparse(c_url).netloc.split(":")[0].lower()
                is_pub = bool(pub_domain and (c_netloc == pub_domain or c_netloc.endswith("." + pub_domain)))
            except Exception:
                is_pub = bool(pub_domain and pub_domain in c_url)

            is_isolated = bool(
                c_frame
                and str(c_frame) in frame_to_ads
                and (not main_frame_id or str(c_frame) != main_frame_id)
                and not call.get("is_main_frame")
            )
            is_main = bool(
                call.get("is_main_frame")
                or (main_frame_id and str(c_frame) == main_frame_id)
                or (
                    not is_isolated
                    and (
                        c_frame is None
                        or (call.get("parent_frame_id") is None and str(c_frame) not in frame_to_ads)
                    )
                )
            )

            ad_ids, ev, conf, amb, scope, unique_attr = _determine_link(
                frame_id=c_frame,
                script_ids=c_script,
                is_main_frame=is_main,
                is_publisher=is_pub,
                is_isolated_ad_frame=is_isolated,
            )
            call["candidate_ad_ids"] = ad_ids
            call["related_ad_ids"] = ad_ids
            call["link_evidence"] = ev
            call["link_confidence"] = conf
            call["evidence_confidence"] = conf
            call["ambiguous"] = amb
            call["attribution_scope"] = scope
            call["unique_ad_attribution"] = unique_attr

    # Annotate Fingerprint calls
    fp_collector = data.get("FingerprintCollector", {})
    if isinstance(fp_collector, dict):
        for call in fp_collector.get("savedCalls", []):
            if not isinstance(call, dict):
                continue
            c_frame = call.get("frame_id")
            is_isolated = bool(
                c_frame
                and str(c_frame) in frame_to_ads
                and (not main_frame_id or str(c_frame) != main_frame_id)
                and not call.get("is_main_frame")
            )
            is_main = bool(
                call.get("is_main_frame")
                or (main_frame_id and str(c_frame) == main_frame_id)
                or (
                    not is_isolated
                    and (
                        c_frame is None
                        or (call.get("parent_frame_id") is None and str(c_frame) not in frame_to_ads)
                    )
                )
            )

            ad_ids, ev, conf, amb, scope, unique_attr = _determine_link(
                frame_id=c_frame,
                is_main_frame=is_main,
                is_isolated_ad_frame=is_isolated,
            )
            call["candidate_ad_ids"] = ad_ids
            call["related_ad_ids"] = ad_ids
            call["link_evidence"] = ev
            call["link_confidence"] = conf
            call["evidence_confidence"] = conf
            call["ambiguous"] = amb
            call["attribution_scope"] = scope
            call["unique_ad_attribution"] = unique_attr

    return build_frame_correlation_index(result)


def build_frame_correlation_index(result: dict[str, Any]) -> dict[str, Any]:
    """Construct index mapping verified frame IDs and loader IDs to associated events and ads."""
    index: dict[str, dict[str, Any]] = {}

    data = result.get("data", {})
    if not isinstance(data, dict):
        return {}

    def _ensure_entry(key: str) -> dict[str, Any]:
        if key not in index:
            index[key] = {
                "requests": [],
                "api_calls": [],
                "fingerprint_calls": [],
                "related_ad_ids": [],
                "link_evidence": [],
                "link_confidence": "low",
                "ambiguous": False,
            }
        return index[key]

    # AdCollector frame ownership

    ad_data = data.get("AdCollector", {})
    ads = ad_data.get("adAttrs", []) if isinstance(ad_data, dict) else []
    for ad in ads:
        if not isinstance(ad, dict):
            continue
        ad_id = str(ad.get("ad_impression_id") or ad.get("id") or "")
        if not ad_id:
            continue
        top_frame_id = ad.get("frame_id") or ad.get("frameId")
        if top_frame_id:
            entry = _ensure_entry(f"frame:{top_frame_id}")
            if ad_id not in entry["related_ad_ids"]:
                entry["related_ad_ids"].append(ad_id)
            entry["link_confidence"] = "high"
        for frame_entry in ad.get("adLinksAndImages", []):
            if isinstance(frame_entry, dict):
                fid = frame_entry.get("frameId") or frame_entry.get("frame_id")
                if fid:
                    entry = _ensure_entry(f"frame:{fid}")
                    if ad_id not in entry["related_ad_ids"]:
                        entry["related_ad_ids"].append(ad_id)
                    entry["link_confidence"] = "high"

    # Requests
    requests = data.get("RequestCollector", [])

    if isinstance(requests, list):
        for req in requests:
            if not isinstance(req, dict):
                continue
            seq = req.get("event_seq")
            fid = req.get("frameId")
            if fid and seq is not None:
                entry = _ensure_entry(f"frame:{fid}")
                entry["requests"].append(seq)
                if req.get("related_ad_ids"):
                    for ad_id in req["related_ad_ids"]:
                        if ad_id not in entry["related_ad_ids"]:
                            entry["related_ad_ids"].append(ad_id)
                    entry["link_evidence"] = req.get("link_evidence", [])
                    entry["link_confidence"] = req.get("link_confidence", "low")
                    entry["ambiguous"] = len(entry["related_ad_ids"]) > 1

    # API calls
    api_collector = data.get("APICallCollector", {})
    if isinstance(api_collector, dict):
        for call in api_collector.get("savedCalls", []):
            if not isinstance(call, dict):
                continue
            seq = call.get("event_seq")
            fid = call.get("frame_id")
            if fid and seq is not None:
                entry = _ensure_entry(f"frame:{fid}")
                entry["api_calls"].append(seq)
                if call.get("related_ad_ids"):
                    for ad_id in call["related_ad_ids"]:
                        if ad_id not in entry["related_ad_ids"]:
                            entry["related_ad_ids"].append(ad_id)
                    entry["link_evidence"] = call.get("link_evidence", [])
                    entry["link_confidence"] = call.get("link_confidence", "low")
                    entry["ambiguous"] = len(entry["related_ad_ids"]) > 1

    # Fingerprint calls
    fp_collector = data.get("FingerprintCollector", {})
    if isinstance(fp_collector, dict):
        for call in fp_collector.get("savedCalls", []):
            if not isinstance(call, dict):
                continue
            seq = call.get("event_seq")
            fid = call.get("frame_id")
            if fid and seq is not None:
                entry = _ensure_entry(f"frame:{fid}")
                entry["fingerprint_calls"].append(seq)
                if call.get("related_ad_ids"):
                    for ad_id in call["related_ad_ids"]:
                        if ad_id not in entry["related_ad_ids"]:
                            entry["related_ad_ids"].append(ad_id)
                    entry["link_evidence"] = call.get("link_evidence", [])
                    entry["link_confidence"] = call.get("link_confidence", "low")
                    entry["ambiguous"] = len(entry["related_ad_ids"]) > 1

    for entry in index.values():
        entry["requests"] = sorted(set(entry["requests"]))
        entry["api_calls"] = sorted(set(entry["api_calls"]))
        entry["fingerprint_calls"] = sorted(set(entry["fingerprint_calls"]))

    return index

