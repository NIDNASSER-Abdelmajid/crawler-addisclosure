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

from playwright.async_api import Page


class CookieCollector:
    COLLECTOR_NAME = "CookieCollector"

    def init(self, output_dir: str, logger, url_hash: str) -> None:
        self._output_dir = Path(output_dir)
        self._logger = logger
        self._url_hash = url_hash

    async def collect(self, page: Page) -> list:
        # Wait for all in-flight requests to complete so late-set cookies are captured.
        try:
            await page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:
            pass

        raw_cookies = await page.context.cookies()

        results = []
        for c in raw_cookies:
            # Playwright returns expires as a float Unix timestamp (seconds).
            # -1 signals a session cookie (no explicit expiry).
            expires_raw = c.get("expires", -1)
            is_session = expires_raw == -1 or expires_raw is None
            expires_ms = None if is_session else int(expires_raw * 1000)

            results.append({
                "name":     c.get("name", ""),
                "domain":   c.get("domain", ""),
                "path":     c.get("path", "/"),
                "expires":  expires_ms,
                "session":  is_session,
                "sameSite": c.get("sameSite"),
                "httpOnly": c.get("httpOnly", False),
                "secure":   c.get("secure", False),
            })

        self._logger.info(f"[CookieCollector] Collected {len(results)} cookie(s)")
        return results
