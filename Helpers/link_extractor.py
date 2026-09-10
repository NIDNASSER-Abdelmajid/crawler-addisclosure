"""Internal same-domain link extractor for recursive depth crawling."""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Optional, Set
from urllib.parse import urldefrag, urljoin, urlparse

from Helpers.hasher import get_registrable_domain

# Non-navigable asset and media extensions to exclude from recursive page crawling
_EXCLUDED_EXTENSIONS = frozenset({
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".tif", ".tiff", ".psd", ".raw",
    # Documents & Archives
    ".pdf", ".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z", ".rar", ".exe", ".dmg", ".pkg", ".deb", ".rpm", ".iso",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".txt", ".rtf",
    # Stylesheets & Scripts
    ".css", ".js", ".mjs", ".json", ".xml", ".rss", ".atom", ".map",
    # Fonts
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    # Media (Audio / Video)
    ".mp4", ".m4v", ".webm", ".ogv", ".avi", ".mov", ".wmv", ".flv", ".mkv",
    ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma",
})


def is_same_domain(candidate_url: str, root_url: str) -> bool:
    """Check if candidate_url belongs to the same registrable domain as root_url."""
    candidate_reg = get_registrable_domain(candidate_url)
    root_reg = get_registrable_domain(root_url)
    if not candidate_reg or not root_reg:
        return False
    return candidate_reg.lower() == root_reg.lower()


def normalize_and_validate_url(href: str, base_url: str, root_url: str) -> Optional[str]:
    """Normalize a link URL, validate scheme/domain, filter assets, and return canonical URL or None."""
    if not href or not isinstance(href, str):
        return None

    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:", "blob:", "about:")):
        return None

    # Resolve relative URL against page base
    try:
        resolved = urljoin(base_url, href)
    except Exception:
        return None

    # Strip fragments
    clean_url, _ = urldefrag(resolved)
    clean_url = clean_url.strip()
    if not clean_url:
        return None

    try:
        parsed = urlparse(clean_url)
    except Exception:
        return None

    # Only accept http / https schemes
    if parsed.scheme.lower() not in ("http", "https"):
        return None

    if not parsed.netloc:
        return None

    # Filter out static file extensions
    path_lower = parsed.path.lower()
    for ext in _EXCLUDED_EXTENSIONS:
        if path_lower.endswith(ext):
            return None

    # Strict domain scoping
    if not is_same_domain(clean_url, root_url):
        return None

    # Normalize trailing slash for root paths e.g. https://domain.com/ -> https://domain.com
    # Keep query strings intact
    normalized_path = parsed.path.rstrip("/") if parsed.path != "/" else "/"
    normalized = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{normalized_path}"
    if parsed.query:
        normalized = f"{normalized}?{parsed.query}"

    return normalized


def canonical_url_key(url: str | None) -> str:
    """Produce a canonical key for robust URL deduplication and exclusion matching.

    Strips schemes (http/https), www. prefixes, trailing slashes, default ports,
    and client fragments while preserving query parameters and paths.
    """
    if not url or not isinstance(url, str):
        return ""
    clean, _ = urldefrag(url.strip())
    if not clean:
        return ""
    try:
        parsed = urlparse(clean)
        netloc = (parsed.netloc or "").lower().removeprefix("www.").split(":")[0]
        path = parsed.path.rstrip("/") if parsed.path != "/" else ""
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{netloc}{path}{query}".lower()
    except Exception:
        return clean.strip().lower().rstrip("/")


async def extract_internal_links(
    page,
    root_url: str,
    parent_url: Optional[str] = None,
    max_links: Optional[int] = None,
    exclude_urls: Optional[Set[str] | list[str]] = None,
    output_dir: Optional[str | Path] = None,
    min_candidates_target: int = 100,
    random_seed: Optional[int] = None,
) -> list[str]:
    """Extract and filter internal same-domain links from the active page DOM.

    Collects unique valid internal URLs from the entire page (at least 100 candidates
    if available in the DOM), strictly excludes the current page URL, parent URL, root
    URL, all previously visited/queued URLs across the crawl, and any URLs already
    completed in the output directory.

    If `max_links` is specified, chooses `max_links` URLs purely randomly (via SystemRandom
    or seed).
    """
    if page is None or getattr(page, "is_closed", lambda: True)():
        return []

    try:
        raw_hrefs = await page.evaluate("""
            () => {
                const anchors = Array.from(document.querySelectorAll('a[href]'));
                return anchors.map(a => a.getAttribute('href') || a.href).filter(Boolean);
            }
        """)
    except Exception:
        raw_hrefs = []

    base_url = getattr(page, "url", "") or root_url

    # Build comprehensive set of excluded raw URLs and canonical keys
    excluded_raw: set[str] = set()
    excluded_keys: set[str] = set()

    def _add_to_excluded(u: str | None) -> None:
        if not u or not isinstance(u, str):
            return
        cleaned = u.strip()
        if not cleaned:
            return
        excluded_raw.add(cleaned)
        excluded_raw.add(cleaned.rstrip("/"))
        key = canonical_url_key(cleaned)
        if key:
            excluded_keys.add(key)

    # Exclude all visited/queued URLs
    if exclude_urls:
        for u in exclude_urls:
            _add_to_excluded(u)

    # Exclude current page, parent, and root seed URLs
    _add_to_excluded(base_url)
    _add_to_excluded(root_url)
    if parent_url:
        _add_to_excluded(parent_url)

    # Check for already completed URLs on disk if output_dir is given
    is_completed_fn = None
    if output_dir:
        try:
            from timeout_manager import is_url_already_completed
            is_completed_fn = is_url_already_completed
        except Exception:
            pass

    candidates: list[str] = []
    seen_keys: set[str] = set()

    for href in raw_hrefs:
        normalized = normalize_and_validate_url(href, base_url, root_url)
        if not normalized:
            continue

        cand_key = canonical_url_key(normalized)
        if not cand_key or cand_key in seen_keys or cand_key in excluded_keys:
            continue
        if normalized in excluded_raw or normalized.rstrip("/") in excluded_raw:
            continue
        if is_completed_fn and is_completed_fn(output_dir, normalized):
            continue

        seen_keys.add(cand_key)
        candidates.append(normalized)

    if not candidates:
        return []

    # If max_links is requested and fewer than total candidates, pick purely randomly
    if max_links is not None and max_links > 0 and len(candidates) > max_links:
        rng = random.Random(random_seed) if random_seed is not None else random.SystemRandom()
        return rng.sample(candidates, max_links)

    return candidates
