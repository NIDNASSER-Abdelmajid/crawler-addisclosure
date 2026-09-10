"""Standalone smoke test for the ad-disclosure collector."""

from __future__ import annotations

import asyncio
import tempfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Collectors.AdDisclosureCollector import AdDisclosureCollector


class FakeLogger:
    def info(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class FakeContext:
    def __init__(self, new_page_factory=None):
        self._listeners: dict[str, list] = {}
        self.pages: list = []
        self._new_page_factory = new_page_factory

    def on(self, event_name: str, callback):
        self._listeners.setdefault(event_name, []).append(callback)

    def remove_listener(self, event_name: str, callback):
        callbacks = self._listeners.get(event_name, [])
        self._listeners[event_name] = [item for item in callbacks if item is not callback]

    def emit_page(self, page):
        for callback in list(self._listeners.get("page", [])):
            callback(page)

    async def new_page(self):
        page = self._new_page_factory() if self._new_page_factory else FakeDisclosurePage(
            url="about:blank",
            body_text="",
            links=[],
        )
        self.pages.append(page)
        return page


class FakeMainPage:
    def __init__(self, context: FakeContext | None = None):
        self.context = context or FakeContext()
        self.waited = []
        self.brought_to_front = 0

    async def wait_for_timeout(self, timeout_ms: int):
        self.waited.append(timeout_ms)

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None

    async def bring_to_front(self):
        self.brought_to_front += 1


class FakeDisclosurePage:
    def __init__(self, url: str, body_text: str, links: list[dict], af_url: str = ""):
        self.url = url
        self.body_text = body_text
        self.links = links
        self.af_url = af_url
        self.closed = False
        self.screenshot_path: str | None = None
        self.waited = []

    def is_closed(self) -> bool:
        return self.closed

    async def wait_for_timeout(self, timeout_ms: int):
        self.waited.append(timeout_ms)

    async def goto(self, url: str, wait_until: str | None = None):
        self.url = url
        return None

    async def evaluate(self, expression: str, selectors=None):
        if "window.document?.body?.innerText" in expression:
            return self.body_text
        if "window.AF_dataServiceRequests" in expression:
            return self.af_url
        if "document.querySelectorAll('a')" in expression:
            selectors = selectors or []
            return [
                {"text": link["text"], "href": link["href"]}
                for link in self.links
                if link.get("text") in selectors and link.get("href") is not None
            ]
        return None

    async def screenshot(self, path: str, full_page: bool = False):
        self.screenshot_path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    async def close(self):
        self.closed = True


async def _test_direct_capture() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        collector = AdDisclosureCollector()
        collector.init(tmpdir, FakeLogger(), "hash123")

        disclosure_page = FakeDisclosurePage(
            url="https://privacy.us.criteo.com/privacy",
            body_text="Why this ad?\nSee more ads by this advertiser\nReport this ad",
            links=[
                {"text": "See more ads by this advertiser", "href": "https://example.com/more"},
                {"text": "Report this ad", "href": "https://example.com/report"},
                {"text": "Not collected", "href": "https://example.com/ignore"},
            ],
        )

        disclosure = await collector.capture_disclosure_page(disclosure_page, "ad_0")

        assert disclosure is not None, "expected the disclosure page to be captured"
        assert disclosure["pageUrl"] == disclosure_page.url
        assert disclosure["adDiscUrl"] == disclosure_page.url
        assert [item["text"] for item in disclosure["adDisclosureOutLinks"]] == [
            "See more ads by this advertiser",
            "Report this ad",
        ]
        assert disclosure["screenshot"] == "disclosure_ad_0"
        assert disclosure_page.closed is True
        assert Path(disclosure_page.screenshot_path).name == "disclosure_ad_0"


async def _test_listener_capture() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        collector = AdDisclosureCollector()
        collector.init(tmpdir, FakeLogger(), "hash456")

        main_page = FakeMainPage()
        await collector.pre_crawl(main_page)

        disclosure_page = FakeDisclosurePage(
            url="https://adssettings.google.com/whythisad",
            body_text="About this ad\nWhy this ad?",
            links=[
                {"text": "See more ads by this advertiser", "href": "/more"},
                {"text": "Report this ad", "href": "/report"},
            ],
            af_url="https://adssettings.google.com/whythisad?tracking=full-url",
        )

        main_page.context.emit_page(disclosure_page)
        disclosures = await collector.collect(main_page, settle_ms=0)

        assert len(disclosures) == 1, "listener path should capture the opened disclosure page"
        assert disclosures[0]["adDiscUrl"] == "https://adssettings.google.com/whythisad?tracking=full-url"
        assert disclosures[0]["adDisclosureOutLinks"] == [
            {"text": "See more ads by this advertiser", "href": "/more"},
            {"text": "Report this ad", "href": "/report"},
        ]
        assert main_page.waited == []


async def _test_context_scan_capture() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        collector = AdDisclosureCollector()
        collector.init(tmpdir, FakeLogger(), "hash789")

        main_page = FakeMainPage()
        disclosure_page = FakeDisclosurePage(
            url="https://adssettings.google.com/whythisad",
            body_text="About this ad\nWhy this ad?",
            links=[
                {"text": "See more ads by this advertiser", "href": "https://example.com/more"},
                {"text": "Report this ad", "href": "https://example.com/report"},
            ],
            af_url="https://adssettings.google.com/whythisad?source=display",
        )

        main_page.context.pages = [main_page, disclosure_page]
        await collector.pre_crawl(main_page)

        disclosures = await collector.collect(main_page, settle_ms=0)

        assert len(disclosures) == 1, "collector should screen disclosure pages already in the context"
        assert disclosures[0]["adDiscUrl"] == "https://adssettings.google.com/whythisad?source=display"


async def _test_strict_click_capture() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        collector = AdDisclosureCollector()
        collector.init(tmpdir, FakeLogger(), "hashstrict")

        main_page = FakeMainPage()
        await collector.pre_crawl(main_page)

        unrelated_page = FakeDisclosurePage(
            url="https://example.com/unrelated",
            body_text="This is not a disclosure page",
            links=[],
        )
        expected_page = FakeDisclosurePage(
            url="https://weather.com/services/ad-choices?source=popup",
            body_text="Ad choices\nWhy this ad?",
            links=[
                {"text": "See more ads by this advertiser", "href": "https://example.com/more"},
                {"text": "Report this ad", "href": "https://example.com/report"},
            ],
        )

        main_page.context.emit_page(unrelated_page)
        main_page.context.emit_page(expected_page)

        disclosures = await collector.capture_click_disclosures(
            main_page,
            expected_href="https://weather.com/services/ad-choices",
            ad_screenshot_name="ad_0.png",
            settle_ms=0,
        )

        assert len(disclosures) == 1, "only the clicked disclosure page should be captured"
        assert disclosures[0]["pageUrl"] == expected_page.url
        assert expected_page.closed is True
        assert main_page.waited == []
        assert main_page.brought_to_front >= 1


async def _test_open_new_tab_capture() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        disclosure_page = FakeDisclosurePage(
            url="about:blank",
            body_text="About this ad\nWhy this ad?",
            links=[
                {"text": "See more ads by this advertiser", "href": "https://example.com/more"},
                {"text": "Report this ad", "href": "https://example.com/report"},
            ],
            af_url="https://privacy.eu.criteo.com/adchoices?source=new-tab",
        )

        context = FakeContext(new_page_factory=lambda: disclosure_page)
        main_page = FakeMainPage(context)
        collector = AdDisclosureCollector()
        collector.init(tmpdir, FakeLogger(), "hash999")

        disclosure = await collector.open_disclosure_in_new_tab(
            main_page,
            "https://privacy.eu.criteo.com/adchoices?source=new-tab",
            "ad_0.png",
        )

        assert disclosure is not None, "expected the disclosure to be captured from a dedicated tab"
        assert disclosure["adDiscUrl"] == "https://privacy.eu.criteo.com/adchoices?source=new-tab"
        assert disclosure_page.closed is True
        assert main_page.brought_to_front >= 1


async def _test_mismatched_google_href_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        collector = AdDisclosureCollector()
        collector.init(tmpdir, FakeLogger(), "hashmismatch")

        disclosure_page = FakeDisclosurePage(
            url="https://adssettings.google.com/whythisad",
            body_text="About this ad\nWhy this ad?",
            links=[
                {"text": "See more ads by this advertiser", "href": "https://example.com/more"},
                {"text": "Report this ad", "href": "https://example.com/report"},
            ],
            af_url="https://adssettings.google.com/whythisad?source=display&reasons=RIGHT_URL",
        )

        disclosure = await collector.capture_disclosure_page(
            disclosure_page,
            "ad_1",
            expected_href="https://adssettings.google.com/whythisad?source=display&reasons=WRONG_URL",
        )

        assert disclosure is None, "mismatched Google disclosure href should be rejected"
        assert disclosure_page.closed is True


async def main() -> None:
    await _test_direct_capture()
    await _test_listener_capture()
    await _test_context_scan_capture()
    await _test_strict_click_capture()
    await _test_open_new_tab_capture()
    await _test_mismatched_google_href_rejected()
    print("AdDisclosureCollector smoke test passed")


if __name__ == "__main__":
    asyncio.run(main())