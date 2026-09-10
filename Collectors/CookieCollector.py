"""
Collectors/CookieCollector.py
------------------------------
Collects all cookies present after page load via Playwright's context API.

Ported from:
https://github.com/duckduckgo/tracker-radar-collector/blob/main/collectors/CookieCollector.js

Recorded fields per cookie
────────────────────────────
name       str        Cookie name
domain     str        Cookie domain
path       str        Cookie path (default "/")
expires    int|None   Expiry as Unix timestamp in milliseconds, or None if session cookie
session    bool       True if the cookie expires when the browser session ends
sameSite   str|None   SameSite policy: "Strict", "Lax", "None", or None if unset
httpOnly   bool       True if the cookie has the HttpOnly flag set
secure     bool       True if the cookie is restricted to HTTPS
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import Page
from Helpers.hasher import get_registrable_domain

if TYPE_CHECKING:
    from Helpers.crawl_context import CrawlContext


class CookieCollector:
    COLLECTOR_NAME = "CookieCollector"

    def init(self, output_dir: str, logger, url_hash: str, crawl_context: CrawlContext | None = None) -> None:
        self._output_dir = Path(output_dir)
        self._logger = logger
        self._url_hash = url_hash
        self._crawl_context = crawl_context

    def _is_first_party(self, cookie_domain: str) -> bool:
        if not self._crawl_context or not self._crawl_context.publisher_domain:
            return True
        pub_domain = self._crawl_context.publisher_domain.lower().lstrip(".")
        cd = (cookie_domain or "").lower().lstrip(".")
        if cd == pub_domain or cd.endswith("." + pub_domain):
            return True
        return get_registrable_domain(cd) == pub_domain

    def _process_cookie(self, c: dict) -> dict:
        expires_raw = c.get("expires", -1)
        is_session = expires_raw == -1 or expires_raw is None
        expires_ms = None if is_session else int(expires_raw * 1000)

        event_seq = self._crawl_context.event_counter.next() if self._crawl_context else None
        document_id = self._crawl_context.document_id if self._crawl_context else None
        first_party = self._is_first_party(c.get("domain", ""))

        record = {
            "name":        c.get("name", ""),
            "domain":      c.get("domain", ""),
            "path":        c.get("path", "/"),
            "expires":     expires_ms,
            "session":     is_session,
            "sameSite":    c.get("sameSite"),
            "httpOnly":    c.get("httpOnly", False),
            "secure":      c.get("secure", False),
            "first_party": first_party,
        }
        if event_seq is not None:
            record["event_seq"] = event_seq
        if document_id is not None:
            record["document_id"] = document_id
        return record

    async def collect(self, page: Page) -> list:
        raw_cookies = await page.context.cookies()
        results = [self._process_cookie(c) for c in raw_cookies]
        self._logger.info(f"[CookieCollector] Collected {len(results)} cookie(s)")
        return results

    async def collect_quick(self, page: Page) -> list:
        """Extract cookies directly from context without waiting for networkidle."""
        try:
            raw_cookies = await page.context.cookies()
        except Exception:
            return []
        return [self._process_cookie(c) for c in raw_cookies]

