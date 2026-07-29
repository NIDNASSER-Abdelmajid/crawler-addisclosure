"""Collect ad-disclosure pages opened in new tabs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Page

from Helpers.ad_disclosure import AD_DISCLOSURE_LINKS, process_ad_disclosure_page


class AdDisclosureCollector:
    COLLECTOR_NAME = "AdDisclosureCollector"
    GOOGLE_BUTTONS_XPATH = [
        '//div[@aria-label="About this advertiser" and @role="button"]', 
        '//div[@aria-label="Why you\'re seeing this ad" and @role="button"]'
        ]

    def init(self, output_dir: str, logger, url_hash: str) -> None:
        self._output_dir = Path(output_dir)
        self._logger = logger
        self._url_hash = url_hash
        self._disclosures: list[dict] = []
        self._pending_pages: list[Page] = []
        self._seen_pages: set[int] = set()
        self._context = None
        self._page_listener = None
        self._ready = False
        (self._output_dir / "ad_disclosures").mkdir(parents=True, exist_ok=True)

    def attach(self, page: Page) -> None:
        context = page.context
        if self._page_listener is not None and self._context is context:
            return

        self._context = context

        def _on_new_page(new_page: Page) -> None:
            self._pending_pages.append(new_page)

        context.on("page", _on_new_page)
        self._page_listener = _on_new_page
        self._ready = True

    async def pre_crawl(self, page: Page) -> None:
        self.attach(page)

    def _is_disclosure_page(self, page: Page) -> bool:
        try:
            hostname = (urlparse(page.url).hostname or "").lower()
        except Exception:
            return False
        return any(link in hostname for link in AD_DISCLOSURE_LINKS)

    def _page_matches_expected_href(self, page: Page, expected_href: str | None) -> bool:
        if not expected_href:
            return False

        try:
            expected = urlparse(expected_href)
            current = urlparse(page.url)
        except Exception:
            return False

        expected_host = (expected.hostname or "").lower()
        current_host = (current.hostname or "").lower()
        if not expected_host or expected_host != current_host:
            return False

        expected_path = (expected.path or "/").rstrip("/") or "/"
        current_path = (current.path or "/").rstrip("/") or "/"
        return current_path == expected_path or current_path.startswith(f"{expected_path}/")

    async def capture_disclosure_page(
        self,
        disclosure_page: Page,
        ad_screenshot_name: str = "ad_disclosure_page.png",
        expected_href: str | None = None,
    ) -> dict | None:
        page_key = id(disclosure_page)
        if page_key in self._seen_pages:
            return None

        self._seen_pages.add(page_key)
        disclosure = await process_ad_disclosure_page(
            disclosure_page,
            ad_screenshot_name,
            self._output_dir,
            self._logger,
            expected_href=expected_href,
        )
        if disclosure:
            self._disclosures.append(disclosure)
        return disclosure

    async def open_disclosure_in_new_tab(
        self,
        main_page: Page,
        href: str,
        ad_screenshot_name: str = "ad_disclosure_page.png",
    ) -> dict | None:
        if not href:
            return None

        disclosure_page = None
        try:
            disclosure_page = await main_page.context.new_page()
            await disclosure_page.goto(href, wait_until="domcontentloaded")
            if "adssettings.google.com" in href:
                for xpath in self.GOOGLE_BUTTONS_XPATH:
                    try:
                        button = await disclosure_page.wait_for_selector(xpath, timeout=5000)
                        if button:
                            await button.click()
                            await asyncio.sleep(1)
                    except Exception as exc:
                        self._logger.debug(f"[{self.COLLECTOR_NAME}] Failed to click Google disclosure button: {exc}")
            return await self.capture_disclosure_page(disclosure_page, ad_screenshot_name, expected_href=href)
        except Exception as exc:
            self._logger.debug(f"[{self.COLLECTOR_NAME}] Failed to open disclosure in a new tab: {exc}")
            return None
        finally:
            try:
                if disclosure_page is not None and not disclosure_page.is_closed():
                    await disclosure_page.close()
            except Exception:
                pass
            try:
                await main_page.bring_to_front()
            except Exception:
                pass

    async def capture_context_disclosures(self, page: Page, ad_screenshot_name: str = "ad_disclosure_page.png", settle_ms: int = 0) -> list[dict]:
        if settle_ms > 0:
            try:
                await page.wait_for_timeout(settle_ms)
            except Exception:
                pass

        try:
            context_pages = list(getattr(self._context, "pages", []) or [])
        except Exception:
            context_pages = []

        captured: list[dict] = []
        for candidate_page in context_pages:
            if candidate_page is None or candidate_page is page or self._seen_pages.__contains__(id(candidate_page)):
                continue
            if not self._is_disclosure_page(candidate_page):
                continue

            disclosure = await self.capture_disclosure_page(candidate_page, ad_screenshot_name)
            if disclosure:
                captured.append(disclosure)

        try:
            await page.bring_to_front()
        except Exception:
            pass

        pending_pages = list(self._pending_pages)
        self._pending_pages.clear()
        for candidate_page in pending_pages:
            if candidate_page is None or candidate_page is page or self._seen_pages.__contains__(id(candidate_page)):
                continue
            if not self._is_disclosure_page(candidate_page):
                continue

            disclosure = await self.capture_disclosure_page(candidate_page, ad_screenshot_name)
            if disclosure:
                captured.append(disclosure)

        return captured

    async def capture_click_disclosures(
        self,
        page: Page,
        expected_href: str,
        ad_screenshot_name: str = "ad_disclosure_page.png",
        settle_ms: int = 0,
    ) -> list[dict]:
        if settle_ms > 0:
            try:
                await page.wait_for_timeout(settle_ms)
            except Exception:
                pass

        captured: list[dict] = []
        candidate_pages = list(self._pending_pages)
        self._pending_pages.clear()

        for candidate_page in candidate_pages:
            if candidate_page is None or self._seen_pages.__contains__(id(candidate_page)):
                continue
            if not self._page_matches_expected_href(candidate_page, expected_href):
                continue

            disclosure = await self.capture_disclosure_page(
                candidate_page,
                ad_screenshot_name,
                expected_href=expected_href,
            )
            if disclosure:
                captured.append(disclosure)

        try:
            await page.bring_to_front()
        except Exception:
            pass

        return captured

    async def collect(self, page: Page, settle_ms: int = 5_000) -> list[dict]:
        if not self._ready:
            self._logger.warning(f"[{self.COLLECTOR_NAME}] pre_crawl was not called; collecting snapshot only")

        try:
            if settle_ms > 0:
                await page.wait_for_timeout(settle_ms)
        except Exception:
            pass

        await self.capture_context_disclosures(page)

        if self._context is not None and self._page_listener is not None:
            try:
                self._context.remove_listener("page", self._page_listener)
            except Exception:
                pass

        self._ready = False
        return list(self._disclosures)