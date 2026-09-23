"""
Collectors/ScreenshotCollector.py
-----------------------------------
Takes a full-page JPEG screenshot of the page after load.

Inspired by:
https://github.com/duckduckgo/tracker-radar-collector/blob/main/collectors/ScreenshotCollector.js

Output
───────
screenshot_<hash>.jpg   Full-page JPEG (quality 75)
"""

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

# Prevent Playwright from hanging indefinitely on unresolved web fonts
os.environ["PW_TEST_SCREENSHOT_NO_FONTS_READY"] = "1"

from playwright.async_api import Page

if TYPE_CHECKING:
    from Helpers.crawl_context import CrawlContext


class ScreenshotCollector:
    COLLECTOR_NAME = "ScreenshotCollector"

    def init(self, output_dir: str, logger, url_hash: str, crawl_context: CrawlContext | None = None) -> None:
        self._output_dir = Path(output_dir)
        self._logger = logger
        self._url_hash = url_hash
        self._crawl_context = crawl_context

    async def collect(self, page: Page) -> list:
        if page is None or getattr(page, "is_closed", lambda: True)():
            self._logger.warning("[ScreenshotCollector] Page is already closed; skipping screenshot")
            return []

        screenshot_path = self._output_dir / f"screenshot_{self._url_hash}.jpg"
        is_safe_for_full_page = True

        # Check page width before full-page capture.
        # Chromium's Skia crashes (SkBitmap::tryAllocPixels) if width exceeds safe allocation limits (e.g. 600,000px on knowledgekids.ca).
        max_safe_w = 8192
        try:
            scroll_w = await asyncio.wait_for(
                page.evaluate("() => (document.documentElement ? document.documentElement.scrollWidth : window.innerWidth) || 1280"),
                timeout=2.0,
            )
            if int(scroll_w) > max_safe_w:
                is_safe_for_full_page = False
                self._logger.warning(
                    f"[ScreenshotCollector] Page width ({scroll_w}px) exceeds safe limit ({max_safe_w}px); using safe viewport fallback"
                )
        except Exception:
            if getattr(page, "is_closed", lambda: True)():
                self._logger.warning("[ScreenshotCollector] Target page closed before screenshot")
                return []

        if is_safe_for_full_page:
            try:
                await page.wait_for_timeout(200)
                await page.screenshot(
                    path=str(screenshot_path),
                    full_page=True,
                    type="jpeg",
                    quality=75,
                    timeout=15000,
                )
                self._logger.info(
                    f"[ScreenshotCollector] Full-page screenshot → {screenshot_path}"
                )
            except Exception as exc:
                self._logger.warning(f"[ScreenshotCollector] Full-page screenshot failed: {exc}")

        if not screenshot_path.exists():
            if getattr(page, "is_closed", lambda: True)():
                self._logger.warning("[ScreenshotCollector] Target page closed; skipping viewport fallback")
                return []

            try:
                await page.wait_for_timeout(100)
                # Capture current viewport directly without resizing window to prevent layout reflow freezes
                await page.screenshot(
                    path=str(screenshot_path),
                    full_page=False,
                    type="jpeg",
                    quality=75,
                    timeout=8000,
                )
                self._logger.info(
                    f"[ScreenshotCollector] Viewport fallback screenshot → {screenshot_path}"
                )
            except Exception as fallback_exc:
                self._logger.warning(f"[ScreenshotCollector] Screenshot fallback timed out / skipped: {fallback_exc}")
                screenshot_path = None

        if not screenshot_path or not screenshot_path.exists():
            return []

        event_seq = self._crawl_context.event_counter.next() if self._crawl_context else None
        document_id = self._crawl_context.document_id if self._crawl_context else None
        record = {
            "screenshot": str(screenshot_path),
            "filename": Path(screenshot_path).name,
        }
        if event_seq is not None:
            record["event_seq"] = event_seq
        if document_id is not None:
            record["document_id"] = document_id
        return [record]
