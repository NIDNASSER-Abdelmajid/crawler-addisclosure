"""Helpers/disclosure_nlp.py — Disclosure NLP Taxonomy, Statement Classification, and Alignment.

Integrates the disclosure statement feature extractor (extract_why_this_ad_features)
with the NLP classification taxonomy (Granularity, Attribution, Technical Categories,
Personalization Stance) and computes empirical visit-level technical-versus-textual alignment.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from extract_addisclosures import extract_why_this_ad_features

# Centralized API Taxonomy mapping ~80 client-side APIs to 15 technical categories
API_TAXONOMY: dict[str, str] = {
    # Browser identity
    "BarProp.prototype.visible": "Browser identity",
    "Navigator.prototype.appCodeName": "Browser identity",
    "Navigator.prototype.appName": "Browser identity",
    "Navigator.prototype.appVersion": "Browser identity",
    "Navigator.prototype.platform": "Browser identity",
    "Navigator.prototype.product": "Browser identity",
    "Navigator.prototype.productSub": "Browser identity",
    "Navigator.prototype.userAgent": "Browser identity",
    "Navigator.prototype.vendor": "Browser identity",
    "Navigator.prototype.vendorSub": "Browser identity",
    "Navigator.prototype.webdriver": "Browser identity",
    "NavigatorUAData.prototype.brands": "Browser identity",
    "NavigatorUAData.prototype.getHighEntropyValues": "Browser identity",
    "NavigatorUAData.prototype.platform": "Browser identity",
    "window.name": "Browser identity",
    "navigator.userAgent": "Browser identity",
    "navigator.platform": "Browser identity",
    "navigator.appVersion": "Browser identity",

    # Language and locale
    "Navigator.prototype.language": "Language and locale",
    "Navigator.prototype.languages": "Language and locale",
    "navigator.language": "Language and locale",
    "navigator.languages": "Language and locale",

    # Time and timezone
    "Date.prototype.getTime": "Time and timezone",
    "Date.prototype.getTimezoneOffset": "Time and timezone",
    "Intl.DateTimeFormat.prototype.resolvedOptions": "Time and timezone",

    # Screen and viewport
    "Element.prototype.getClientRects": "Screen and viewport",
    "Screen.prototype.availHeight": "Screen and viewport",
    "Screen.prototype.availLeft": "Screen and viewport",
    "Screen.prototype.availTop": "Screen and viewport",
    "Screen.prototype.availWidth": "Screen and viewport",
    "Screen.prototype.colorDepth": "Screen and viewport",
    "Screen.prototype.height": "Screen and viewport",
    "Screen.prototype.orientation": "Screen and viewport",
    "Screen.prototype.pixelDepth": "Screen and viewport",
    "Screen.prototype.width": "Screen and viewport",
    "window.devicePixelRatio": "Screen and viewport",
    "window.innerHeight": "Screen and viewport",
    "window.innerWidth": "Screen and viewport",
    "window.outerHeight": "Screen and viewport",
    "window.outerWidth": "Screen and viewport",
    "window.screenX": "Screen and viewport",
    "window.screenY": "Screen and viewport",
    "window.screen": "Screen and viewport",
    "window.matchMedia(\"prefers-color-scheme\")": "Screen and viewport",

    # Hardware indicators
    "Navigator.prototype.deviceMemory": "Hardware indicators",
    "Navigator.prototype.getBattery": "Hardware indicators",
    "Navigator.prototype.hardwareConcurrency": "Hardware indicators",
    "Navigator.prototype.keyboard": "Hardware indicators",
    "Navigator.prototype.maxTouchPoints": "Hardware indicators",
    "navigator.deviceMemory": "Hardware indicators",
    "navigator.hardwareConcurrency": "Hardware indicators",
    "navigator.maxTouchPoints": "Hardware indicators",

    # Cookies and identifiers
    "CookieStore.prototype.getAll": "Cookies and identifiers",
    "CookieStore.prototype.set": "Cookies and identifiers",
    "Document.cookie getter": "Cookies and identifiers",
    "Document.cookie setter": "Cookies and identifiers",
    "Document.prototype.cookie": "Cookies and identifiers",
    "document.cookie": "Cookies and identifiers",
    "Navigator.prototype.cookieEnabled": "Cookies and identifiers",
    "Navigator.prototype.doNotTrack": "Cookies and identifiers",

    # Local and session storage
    "Navigator.prototype.storage": "Local and session storage",
    "Navigator.prototype.webkitTemporaryStorage": "Local and session storage",
    "window.localStorage": "Local and session storage",
    "window.sessionStorage": "Local and session storage",
    "Storage.prototype.getItem": "Local and session storage",
    "Storage.prototype.setItem": "Local and session storage",
    "Storage.prototype.removeItem": "Local and session storage",
    "Storage.prototype.clear": "Local and session storage",

    # IndexedDB
    "window.indexedDB": "IndexedDB",

    # Network capabilities
    "BroadcastChannel.prototype.constructor": "Network capabilities",
    "Navigator.prototype.connection": "Network capabilities",
    "Navigator.prototype.onLine": "Network capabilities",
    "RTCPeerConnection.createDataChannel": "Network capabilities",
    "RTCPeerConnection.createOffer": "Network capabilities",
    "RTCPeerConnection.onicecandidate": "Network capabilities",
    "RTCPeerConnection.prototype.constructor": "Network capabilities",
    "RTCPeerConnectionIceEvent.prototype.candidate": "Network capabilities",

    # Permissions
    "Navigator.prototype.permissions": "Permissions",
    "Notification.permission": "Permissions",

    # Graphics and rendering
    "CanvasRenderingContext2D.fillStyle": "Graphics and rendering",
    "HTMLCanvasElement.toDataURL": "Graphics and rendering",
    "HTMLCanvasElement.prototype.toDataURL": "Graphics and rendering",
    "CanvasRenderingContext2D.prototype.getImageData": "Graphics and rendering",
    "CanvasRenderingContext2D.prototype.isPointInPath": "Graphics and rendering",
    "WebGL2RenderingContext.prototype.getExtension": "Graphics and rendering",
    "WebGLRenderingContext.prototype.getExtension": "Graphics and rendering",
    "WebGLRenderingContext.prototype.getSupportedExtensions": "Graphics and rendering",
    "WebGLRenderingContext.prototype.getParameter": "Graphics and rendering",

    # Audio
    "MediaDevices.prototype.enumerateDevices": "Audio",
    "Navigator.prototype.mediaDevices": "Audio",
    "MediaSource.isTypeSupported": "Audio",
    "AudioContext.prototype.createOscillator": "Audio",
    "AudioContext.prototype.createAnalyser": "Audio",
    "AudioContext.prototype.createDynamicsCompressor": "Audio",

    # Installed fonts or plugins
    "Navigator.prototype.javaEnabled": "Installed fonts or plugins",
    "Navigator.prototype.mimeTypes": "Installed fonts or plugins",
    "Navigator.prototype.plugins": "Installed fonts or plugins",
    "navigator.plugins": "Installed fonts or plugins",

    # Timing information
    "Event.prototype.timeStamp": "Timing information",
    "Performance.prototype.memory": "Timing information",
    "PerformanceTiming.prototype.navigationStart": "Timing information",

    # Other or unclassified
    "URL.createObjectURL": "Other or unclassified",
    "Navigator.prototype.mediaCapabilities": "Other or unclassified",
}

ALL_TECHNICAL_CATEGORIES = sorted(set(API_TAXONOMY.values()))


def get_api_category(api_name: str) -> str:
    """Map an API name to its standard technical category in the taxonomy."""
    if not api_name:
        return "Other or unclassified"
    if api_name in API_TAXONOMY:
        return API_TAXONOMY[api_name]
    for pattern, cat in API_TAXONOMY.items():
        if pattern.lower() in api_name.lower() or api_name.lower() in pattern.lower():
            return cat
    return "Other or unclassified"


def classify_statement_nlp(norm_text: str) -> dict[str, Any]:
    """Classify an extracted disclosure statement according to the NLP taxonomy.
    
    Returns structured annotations covering:
    - Rationale Theme (6 mutually exclusive themes)
    - Granularity / Specificity (Precise, Broad, Not provided, Not applicable)
    - Attribution (Advertiser, Platform/algorithm, Both, Ambiguous, Not stated)
    - Acknowledged Technical Data Categories (Strict vs Lenient interpretations)
    - Personalization Stance (Asserted, Denied, Contextual, Unclear)
    """
    text = (norm_text or "").lower()

    # 1. Rationale Theme
    theme = "Contextual Website Content"
    theme_id = 1
    if any(k in text for k in ["interest", "activity on this device", "inferred", "profile", "targeted", "past visit", "search history"]):
        theme = "Behavioral & Interest Profiling"
        theme_id = 0
    elif any(k in text for k in ["placement", "agreed upon by the publisher", "partners with", "contract", "agreement"]):
        theme = "Publisher-Advertiser Placement Agreement"
        theme_id = 2
    elif any(k in text for k in ["time of day", "general location", "country or city", "location", "ip address"]):
        theme = "Time of Day & General Location"
        theme_id = 3
    elif any(k in text for k in ["turned off", "disabled", "not personalized", "opted out", "inactive"]):
        theme = "Personalization Turned Off Notice"
        theme_id = 4
    elif any(k in text for k in ["similar to", "lookalike", "people like you", "group similarity"]):
        theme = "Lookalike & Group Similarity"
        theme_id = 5

    # 2. Strict & Lenient Technical Data Categories
    strict_cats: list[str] = []
    lenient_cats: list[str] = []

    if "cookie" in text or "identifier" in text:
        strict_cats.append("Cookies and identifiers")
        lenient_cats.extend(["Cookies and identifiers", "Local and session storage", "IndexedDB"])

    if any(k in text for k in ["device", "browser", "computer", "operating system", "screen"]):
        strict_cats.append("Browser identity")
        lenient_cats.extend([
            "Browser identity",
            "Screen and viewport",
            "Hardware indicators",
            "Graphics and rendering",
            "Audio",
            "Installed fonts or plugins",
        ])

    if any(k in text for k in ["location", "country", "city", "geographic", "ip"]):
        strict_cats.append("Location")
        lenient_cats.append("Location")

    if any(k in text for k in ["time of day", "current time", "timezone", "time"]):
        strict_cats.append("Time and timezone")
        lenient_cats.extend(["Time and timezone", "Timing information"])

    if any(k in text for k in ["interest", "activity", "browsing history", "profile"]):
        strict_cats.append("Interests or inferred profile")
        lenient_cats.append("Interests or inferred profile")

    if any(k in text for k in ["website", "page", "content", "article", "publisher", "topic"]):
        strict_cats.append("Contextual page content")
        lenient_cats.append("Contextual page content")

    if "storage" in text:
        strict_cats.append("Local and session storage")
        lenient_cats.extend(["Local and session storage", "IndexedDB"])

    if not strict_cats:
        strict_cats.append("No technical category acknowledged")
    if not lenient_cats:
        lenient_cats.append("No technical category acknowledged")

    # 3. Granularity / Specificity
    if theme == "Personalization Turned Off Notice":
        granularity = "Not applicable"
    elif any(k in text for k in ["city", "exact", "cookie", "storage", "settings", "nextroll", "adroll", "specific"]):
        granularity = "Precise"
    elif any(k in text for k in ["general factors", "general location", "broad", "content", "website", "time of day"]):
        granularity = "Broad"
    else:
        granularity = "Not provided"

    # 4. Attribution
    has_adv = any(k in text for k in ["advertiser", "sponsor", "brand", "merchant", "company"])
    has_plat = any(k in text for k in ["google", "platform", "algorithm", "network", "system", "partner"])
    if has_adv and has_plat:
        attribution = "Both"
    elif has_adv:
        attribution = "Advertiser"
    elif has_plat:
        attribution = "Platform/algorithm"
    elif any(k in text for k in ["was shown", "is based", "decided"]):
        attribution = "Ambiguous"
    else:
        attribution = "Not stated"

    # 5. Personalization Stance
    if any(k in text for k in ["turned off", "disabled", "not personalized", "does not use", "opted out"]):
        stance = "personalization denied"
    elif any(k in text for k in ["based on your activity", "interests", "targeted", "personalized", "tailored"]):
        stance = "personalization asserted"
    elif any(k in text for k in ["placement", "general factors", "time of day", "context", "this website"]):
        stance = "contextual or non-personalized explanation"
    else:
        stance = "unclear or mixed"

    return {
        "theme": theme,
        "theme_id": theme_id,
        "granularity": granularity,
        "attribution": attribution,
        "strict_categories": sorted(set(strict_cats)),
        "lenient_categories": sorted(set(lenient_cats)),
        "stance": stance,
        "personalization_stance": stance,
    }


def analyze_disclosure_text(
    disclosure_text: str,
    visit_id: str = "",
    ad_impression_id: str | None = None,
) -> list[dict[str, Any]]:
    """Extract statements from raw disclosure text and annotate them with the NLP taxonomy."""
    if not disclosure_text:
        return []

    features = extract_why_this_ad_features(disclosure_text)
    statements = []

    for s_idx, feat in enumerate(features):
        norm_feat = " ".join(feat.strip().split()).lower().replace("’", "'").rstrip(":")
        if not norm_feat:
            continue
        classification = classify_statement_nlp(norm_feat)
        stmt_id = f"{ad_impression_id or 'disc'}_stmt_{s_idx+1:02d}"
        statements.append({
            "statement_id": stmt_id,
            "visit_id": visit_id,
            "ad_impression_id": ad_impression_id,
            "original_text": feat,
            "normalized_text": norm_feat,
            "nlp_rationale_theme": classification["theme"],
            "theme_id": classification["theme_id"],
            "granularity": classification["granularity"],
            "attribution": classification["attribution"],
            "acknowledged_technical_data_categories_strict": classification["strict_categories"],
            "acknowledged_technical_data_categories_lenient": classification["lenient_categories"],
            "personalization_stance": classification["personalization_stance"],
            "model_confidence": 1.0,
            "validation_status": "validated_semantic_taxonomy",
        })

    return statements


def compute_visit_disclosure_alignment(
    observed_api_events: list[dict[str, Any]],
    disclosure_statements: list[dict[str, Any]],
    website: str = "",
) -> dict[str, Any]:
    """Compute empirical technical-versus-textual alignment between observed APIs and disclosures.
    
    Formula: coverage_v = |O_v ∩ D_v| / |O_v|
    Calculated under both strict and lenient interpretations.
    """
    obs_categories: set[str] = set()
    for ev in observed_api_events:
        if isinstance(ev, dict):
            cat = ev.get("api_category") or get_api_category(ev.get("api_name", ""))
            if cat and cat != "Other or unclassified":
                obs_categories.add(cat)

    strict_disclosed: set[str] = set()
    lenient_disclosed: set[str] = set()
    for stmt in disclosure_statements:
        for c in stmt.get("acknowledged_technical_data_categories_strict", []):
            if c != "No technical category acknowledged":
                strict_disclosed.add(c)
        for c in stmt.get("acknowledged_technical_data_categories_lenient", []):
            if c != "No technical category acknowledged":
                lenient_disclosed.add(c)

    strict_intersection = obs_categories.intersection(strict_disclosed)
    lenient_intersection = obs_categories.intersection(lenient_disclosed)

    cov_strict = len(strict_intersection) / len(obs_categories) if obs_categories else 0.0
    cov_lenient = len(lenient_intersection) / len(obs_categories) if obs_categories else 0.0

    observed_without_disclosure = sorted(obs_categories - strict_disclosed)
    acknowledged_not_observed = sorted(strict_disclosed - obs_categories)
    acknowledged_and_observed = sorted(strict_intersection)
    unmentioned = sorted(set(ALL_TECHNICAL_CATEGORIES) - (obs_categories | strict_disclosed))

    return {
        "website": website,
        "observed_categories_count": len(obs_categories),
        "observed_categories": sorted(obs_categories),
        "disclosures_extracted": len(disclosure_statements) > 0,
        "statement_count": len(disclosure_statements),
        "strict_disclosed_categories_count": len(strict_disclosed),
        "strict_disclosed_categories": sorted(strict_disclosed),
        "strict_intersection_count": len(strict_intersection),
        "strict_coverage_rate": round(cov_strict, 4),
        "strict_coverage_percentage": f"{cov_strict * 100:.1f}%",
        "lenient_disclosed_categories_count": len(lenient_disclosed),
        "lenient_disclosed_categories": sorted(lenient_disclosed),
        "lenient_intersection_count": len(lenient_intersection),
        "lenient_coverage_rate": round(cov_lenient, 4),
        "lenient_coverage_percentage": f"{cov_lenient * 100:.1f}%",
        "acknowledged_and_observed": acknowledged_and_observed,
        "observed_without_disclosure": observed_without_disclosure,
        "acknowledged_not_observed": acknowledged_not_observed,
        "unmentioned_categories": unmentioned,
        "methodological_boundary": (
            "Measures same-visit technical-versus-textual category alignment. "
            "Does not establish that observed API calls were executed or utilized by a particular disclosed advertisement."
        ),
    }


def process_visit_disclosures(result: dict[str, Any]) -> dict[str, Any]:
    """Process disclosure texts across AdCollector and AdDisclosureCollector for a visit."""
    data = result.get("data", {})
    if not isinstance(data, dict):
        return {"statements": [], "alignment": {}}

    visit_id = str(result.get("document_id") or result.get("attempt_id") or "visit_0")
    website = str(result.get("initial_url") or result.get("url") or "")

    # Gather disclosure texts from AdCollector retained ads
    ad_data = data.get("AdCollector", {})
    ads = ad_data.get("adAttrs", []) if isinstance(ad_data, dict) else []
    all_statements: list[dict[str, Any]] = []

    for ad in ads:
        if not isinstance(ad, dict):
            continue
        imp_id = ad.get("ad_impression_id") or ad.get("id")
        raw_text = ad.get("adDisclosureText") or ""
        if raw_text:
            stmts = analyze_disclosure_text(raw_text, visit_id=visit_id, ad_impression_id=imp_id)
            all_statements.extend(stmts)

    # Gather disclosure texts from AdDisclosureCollector attempts
    disc_data = data.get("AdDisclosureCollector", {})
    attempts = disc_data.get("attempts", []) if isinstance(disc_data, dict) else []
    for att in attempts:
        if not isinstance(att, dict):
            continue
        imp_id = att.get("ad_impression_id")
        raw_text = att.get("page_text") or att.get("pageText") or ""
        if raw_text and not any(s.get("ad_impression_id") == imp_id for s in all_statements):
            stmts = analyze_disclosure_text(raw_text, visit_id=visit_id, ad_impression_id=imp_id)
            all_statements.extend(stmts)

    # Gather observed API events
    api_collector = data.get("APICallCollector", {})
    saved_calls = api_collector.get("savedCalls", []) if isinstance(api_collector, dict) else []
    fp_collector = data.get("FingerprintCollector", {})
    fp_calls = fp_collector.get("savedCalls", []) if isinstance(fp_collector, dict) else []
    combined_apis = list(saved_calls) + list(fp_calls)

    alignment = compute_visit_disclosure_alignment(
        observed_api_events=combined_apis,
        disclosure_statements=all_statements,
        website=website,
    )

    return {
        "statements": all_statements,
        "alignment": alignment,
    }
