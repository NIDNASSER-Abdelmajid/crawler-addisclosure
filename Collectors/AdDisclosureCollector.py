"""Collect ad-disclosure pages opened in new tabs."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse
from typing import TYPE_CHECKING, Any

from playwright.async_api import Page

from Helpers.ad_disclosure import (
    AD_DISCLOSURE_LINKS,
    _matches_expected_href,
    expand_disclosure_dropdowns,
    process_ad_disclosure_page,
)

if TYPE_CHECKING:
    from Helpers.crawl_context import CrawlContext

DISCLOSURE_NAV_TIMEOUT_MS = 7_000
DISCLOSURE_BUTTON_TIMEOUT_MS = 3_000
DISCLOSURE_POST_CLICK_DELAY_S = 0.3
DISCLOSURE_FALLBACK_SETTLE_MS = 250


class AdDisclosureCollector:
    COLLECTOR_NAME = "AdDisclosureCollector"
    DISCLOSURE_NAV_TIMEOUT_MS = DISCLOSURE_NAV_TIMEOUT_MS
    DISCLOSURE_BUTTON_TIMEOUT_MS = DISCLOSURE_BUTTON_TIMEOUT_MS
    DISCLOSURE_POST_CLICK_DELAY_S = DISCLOSURE_POST_CLICK_DELAY_S
    DISCLOSURE_FALLBACK_SETTLE_MS = DISCLOSURE_FALLBACK_SETTLE_MS
    DISCLOSURE_BUTTONS_XPATH = [
        '//div[@aria-label="About this advertiser" and @role="button"]', 
        '//div[@aria-label="Why you\'re seeing this ad" and @role="button"]'
    ]
    DISCLOSURE_BUTTON_SELECTOR = (
        '//div[(@aria-label="About this advertiser" or @aria-label="Why you\'re seeing this ad") and @role="button"]'
    )

    def init(
        self,
        output_dir: str,
        logger,
        url_hash: str,
        crawl_context: CrawlContext | None = None,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._logger = logger
        self._url_hash = url_hash
        self._crawl_context = crawl_context
        self._disclosures: list[dict] = []
        self._attempts: list[dict] = []
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
        return _matches_expected_href(getattr(page, "url", ""), expected_href)

    async def capture_disclosure_page(
        self,
        disclosure_page: Page,
        ad_screenshot_name: str = "ad_disclosure_page.png",
        expected_href: str | None = None,
        ad_impression_id: str | None = None,
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
            button_timeout_ms=self.DISCLOSURE_BUTTON_TIMEOUT_MS,
        )
        if disclosure:
            if ad_impression_id:
                disclosure["ad_impression_id"] = ad_impression_id
            self._disclosures.append(disclosure)
        return disclosure

    async def open_disclosure_in_new_tab(
        self,
        main_page: Page | None,
        href: str,
        ad_screenshot_name: str = "ad_disclosure_page.png",
        ad_impression_id: str | None = None,
    ) -> dict | None:
        if not href or main_page is None:
            return None

        disclosure_page = None
        try:
            disclosure_page = await main_page.context.new_page()
            try:
                await disclosure_page.goto(
                    href,
                    wait_until="domcontentloaded",
                    timeout=self.DISCLOSURE_NAV_TIMEOUT_MS,
                )
            except TypeError:
                await disclosure_page.goto(href, wait_until="domcontentloaded")
            except Exception as nav_exc:
                curr_url = getattr(disclosure_page, "url", "")
                if not curr_url or curr_url == "about:blank":
                    raise nav_exc
                self._logger.debug(f"[{self.COLLECTOR_NAME}] Navigation reached {curr_url} despite DOMContentLoaded delay.")

            return await self.capture_disclosure_page(
                disclosure_page,
                ad_screenshot_name,
                expected_href=href,
                ad_impression_id=ad_impression_id,
            )
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
                if hasattr(main_page, "bring_to_front"):
                    await main_page.bring_to_front()
            except Exception:
                pass

    async def capture_context_disclosures(
        self,
        page: Page,
        ad_screenshot_name: str = "ad_disclosure_page.png",
        settle_ms: int = 0,
    ) -> list[dict]:
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
        """Collect disclosure pages from browser context (backward-compatible list return)."""
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

    async def interact_and_collect_disclosures(
        self,
        page: Page | None,
        ads: list[dict],
    ) -> dict[str, Any]:
        """Perform disclosure interactions for all ads during the disclosure_interaction phase.

        Records an attempt record for every advertisement (including no control found and failed attempts).
        Directly links the disclosure to the advertisement by its impression ID.
        """
        cached_disclosures: dict[str, dict] = {}
        for idx, ad in enumerate(ads):
            ad_impression_id = ad.get("ad_impression_id") or f"ad_{idx + 1:03d}"
            ad_candidate_id = ad.get("ad_candidate_id") or f"cand_{idx + 1:03d}"
            disc_att_id = (
                self._crawl_context.next_disclosure_attempt_id()
                if self._crawl_context
                else f"disc_att_{idx + 1:03d}"
            )

            start_ts = int(time.time() * 1000)
            attempt: dict[str, Any] = {
                "disclosure_attempt_id": disc_att_id,
                "ad_impression_id": ad_impression_id,
                "ad_candidate_id": ad_candidate_id,
                "control_detected": False,
                "visible": False,
                "interaction_attempted": False,
                "interaction_succeeded": False,
                "destination_reached": False,
                "text_extracted": False,
                "failure_stage": None,
                "failure_reason": None,
                "target_url": None,
                "final_url": None,
                "extracted_text_len": 0,
                "out_links_count": 0,
                "screenshot": None,
                "start_time_ms": start_ts,
                "end_time_ms": None,
            }

            detected_controls = ad.get("detectedDisclosureControls", [])
            # Also check fallback AdChoices links if detected controls is empty
            if not detected_controls:
                for d_link in ad.get("adChoicesLinks", []):
                    if d_link and not d_link.startswith("javascript:"):
                        detected_controls.append({"type": "adchoices_link", "href": d_link})
                clicked_link = ad.get("clickedAdChoiceLink")
                if clicked_link and not clicked_link.startswith("javascript:"):
                    detected_controls.append({"type": "clicked_link", "href": clicked_link})

            if not detected_controls:
                # No disclosure control found for this advertisement
                attempt["failure_stage"] = "detection"
                attempt["failure_reason"] = "no_control_found"
                attempt["end_time_ms"] = int(time.time() * 1000)
                if self._crawl_context:
                    self._crawl_context.enrich_event(attempt, timestamp_ms=start_ts)
                self._attempts.append(attempt)
                ad["disclosure_attempt_id"] = disc_att_id
                continue

            attempt["control_detected"] = True
            attempt["visible"] = True
            target_href = detected_controls[0].get("href")
            attempt["target_url"] = target_href
            attempt["interaction_attempted"] = True

            ad_idx = ad.get("index") if isinstance(ad.get("index"), int) else idx
            ad_shot = ad.get("screenshot")
            if ad_shot:
                ad_screenshot_name = Path(ad_shot).name
            else:
                ad_screenshot_name = f"ad_{ad_idx}_{self._url_hash}.png"

            disclosure_screenshot_name = (
                ad_screenshot_name
                if ad_screenshot_name.startswith("disclosure_")
                else f"disclosure_{ad_screenshot_name}"
            )

            if page is not None and target_href:
                try:
                    is_cached = False
                    if target_href in cached_disclosures:
                        cached_entry = cached_disclosures[target_href]
                        disc_data = dict(cached_entry)
                        is_cached = True
                        orig_shot = cached_entry.get("screenshot")
                        if orig_shot:
                            src_shot = self._output_dir / "ad_disclosures" / orig_shot
                            dest_shot = self._output_dir / "ad_disclosures" / disclosure_screenshot_name
                            if src_shot.is_file() and not dest_shot.is_file():
                                try:
                                    shutil.copyfile(src_shot, dest_shot)
                                except Exception:
                                    pass
                            disc_data["screenshot"] = disclosure_screenshot_name
                        self._logger.debug(
                            f"[{self.COLLECTOR_NAME}] Reusing cached disclosure data for duplicate target URL: {target_href[:80]}"
                        )
                    else:
                        disc_data = await self.open_disclosure_in_new_tab(
                            page,
                            target_href,
                            ad_screenshot_name=ad_screenshot_name,
                            ad_impression_id=ad_impression_id,
                        )
                        # If open_disclosure_in_new_tab did not return, also check context disclosures
                        if not disc_data:
                            ctx_discs = await self.capture_context_disclosures(
                                page,
                                ad_screenshot_name=ad_screenshot_name,
                                settle_ms=self.DISCLOSURE_FALLBACK_SETTLE_MS,
                            )
                            if ctx_discs:
                                disc_data = ctx_discs[0]

                        if disc_data:
                            cached_disclosures[target_href] = disc_data

                    if disc_data:
                        attempt["interaction_succeeded"] = True
                        attempt["destination_reached"] = True
                        attempt["final_url"] = disc_data.get("pageUrl")
                        attempt["screenshot"] = disc_data.get("screenshot")
                        ptext = disc_data.get("pageText", "")
                        attempt["extracted_text_len"] = len(ptext)
                        attempt["text_extracted"] = bool(ptext)
                        out_links = disc_data.get("adDisclosureOutLinks", [])
                        attempt["out_links_count"] = len(out_links)

                        # Update ad directly with exact attribution
                        ad["clickedAdChoiceLink"] = target_href or disc_data.get("pageUrl")
                        ad["adDisclosureOutLinks"] = out_links
                        ad["adDisclosureText"] = ptext
                        ad["adDisclosurePageUrl"] = disc_data.get("pageUrl", "")
                        ad["adDisclosureScreenshot"] = disc_data.get("screenshot", "")
                        ad["disclosure_attempt_id"] = disc_att_id
                        ad["hasDisclosureControl"] = True

                        ad_disc_record = dict(disc_data)
                        ad_disc_record["ad_impression_id"] = ad_impression_id
                        ad_disc_record["ad_candidate_id"] = ad_candidate_id
                        ad_disc_record["disclosure_attempt_id"] = disc_att_id

                        if is_cached:
                            self._disclosures.append(ad_disc_record)
                        else:
                            disc_data["ad_impression_id"] = ad_impression_id
                            disc_data["ad_candidate_id"] = ad_candidate_id
                            disc_data["disclosure_attempt_id"] = disc_att_id
                    else:
                        attempt["failure_stage"] = "navigation"
                        attempt["failure_reason"] = "open_failed_or_timeout"
                except Exception as exc:
                    attempt["failure_stage"] = "interaction"
                    attempt["failure_reason"] = str(exc)
            else:
                attempt["failure_stage"] = "interaction"
                attempt["failure_reason"] = "no_page_or_href"

            attempt["end_time_ms"] = int(time.time() * 1000)
            if self._crawl_context:
                self._crawl_context.enrich_event(attempt, timestamp_ms=start_ts)
            self._attempts.append(attempt)
            ad["disclosure_attempt_id"] = disc_att_id

        return self.get_results()

    def get_results(self) -> dict[str, Any]:
        """Return structured results with separate detected, attempted, opened, extracted counts."""
        detected = sum(1 for a in self._attempts if a.get("control_detected"))
        attempted = sum(1 for a in self._attempts if a.get("interaction_attempted"))
        opened = sum(1 for a in self._attempts if a.get("destination_reached") or a.get("interaction_succeeded"))
        extracted = sum(1 for a in self._attempts if a.get("text_extracted"))

        return {
            "attempts": list(self._attempts),
            "disclosures": list(self._disclosures),
            "counts": {
                "detected": detected,
                "attempted": attempted,
                "opened": opened,
                "extracted": extracted,
            },
        }

    def get_partial_results(self) -> dict[str, Any]:
        return self.get_results()