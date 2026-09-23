import asyncio
import logging
import sys
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Collectors.AdCollector import AdCollector


class DummyPage:
    def __init__(self, width=1280, height=800, scroll_x=0, scroll_y=0):
        self.width = width
        self.height = height
        self.scroll_x = scroll_x
        self.scroll_y = scroll_y
        self.url = "https://example.com"
        self.waited_ms = 0
        self.screenshots = []

    async def evaluate(self, script, *args):
        if "scrollX" in script or "innerWidth" in script:
            return {
                "width": self.width,
                "height": self.height,
                "scrollX": self.scroll_x,
                "scrollY": self.scroll_y,
                "x": self.scroll_x,
                "y": self.scroll_y,
            }
        if "scrollingEl" in script:
            return {"x": 0, "y": 0}
        return {}

    async def wait_for_timeout(self, ms: int):
        self.waited_ms += ms

    async def screenshot(self, path: str, clip=None, full_page=False, timeout=3000):
        self.screenshots.append({"path": path, "clip": clip, "full_page": full_page})


class DummyElementHandle:
    def __init__(self, x=0, y=0, width=300, height=250):
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.scrolled_into_view = False
        self.screenshots = []

    async def bounding_box(self):
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }

    async def scroll_into_view_if_needed(self, timeout=300):
        self.scrolled_into_view = True
        # Simulates moving into viewport when scrolled
        if self.x > 1280:
            self.x = 200

    async def screenshot(self, path: str, timeout=1500):
        self.screenshots.append(path)


@pytest.mark.asyncio
async def test_capture_context_screenshot_in_viewport(tmp_path):
    logger = logging.getLogger("test_logger")
    collector = AdCollector()
    collector.init(str(tmp_path), logger, "testhash")
    page = DummyPage()
    handle = DummyElementHandle(x=100, y=150, width=300, height=250)

    bbox = {"x": 100, "y": 150, "width": 300, "height": 250}
    name, box = await collector._capture_context_screenshot(page, bbox, 0, handle)

    assert name == "ad_0_testhash_context.png"
    assert "x" in box and "y" in box
    assert not box.get("outside_viewport", False)
    assert len(page.screenshots) == 1
    assert page.screenshots[0]["clip"] is not None


@pytest.mark.asyncio
async def test_capture_context_screenshot_outside_viewport_carousel_fallback(tmp_path):
    """When an element is off-screen (e.g. x=2358 in a horizontal carousel),
    _capture_context_screenshot must capture a fallback viewport screenshot
    and NOT raise ValueError('Context clip is empty or outside the viewport').
    """
    logger = logging.getLogger("test_logger")
    collector = AdCollector()
    collector.init(str(tmp_path), logger, "testhash")
    page = DummyPage()

    # Off-screen element handle with x far outside viewport (e.g. carousel slide)
    handle = DummyElementHandle(x=2358, y=150, width=552, height=310)
    # Prevent scroll simulation to test pure off-screen handling
    handle.x = 2358
    bbox = {"x": 2358, "y": 150, "width": 552, "height": 310}

    name, box = await collector._capture_context_screenshot(page, bbox, 1, handle)

    # Must succeed without throwing
    assert name == "ad_1_testhash_context.png"
    assert box.get("outside_viewport") is True
    assert len(page.screenshots) == 1
    # Viewport screenshot has full_page=False and clip=None
    assert page.screenshots[0]["clip"] is None


@pytest.mark.asyncio
async def test_capture_bbox_screenshot_brings_carousel_into_view(tmp_path):
    """Verify that _capture_bbox_screenshot invokes scroll_into_view_if_needed
    bringing carousel elements into viewport and recalculating bounding box.
    """
    logger = logging.getLogger("test_logger")
    collector = AdCollector()
    collector.init(str(tmp_path), logger, "testhash")
    page = DummyPage()

    # Carousel ad starting at x=2358
    handle = DummyElementHandle(x=2358, y=200, width=500, height=300)
    bbox = {"x": 2358, "y": 200, "width": 500, "height": 300}

    name, final_bbox = await collector._capture_bbox_screenshot(page, bbox, 2, handle)

    assert handle.scrolled_into_view is True
    assert final_bbox["x"] == 200  # Updated to live in-viewport coordinates
    assert name == "ad_2_testhash.png"
    assert len(page.screenshots) == 1
    # Total wait time in _capture_bbox_screenshot + _scroll_bbox_into_view should be 50ms, not 250ms
    assert page.waited_ms == 50


@pytest.mark.asyncio
async def test_capture_single_ad_carousel(tmp_path, monkeypatch):
    """Verify that _capture_single_ad processes a carousel ad without warnings
    and returns both ad image and context screenshot.
    """
    logger = logging.getLogger("test_logger")
    collector = AdCollector()
    collector.init(str(tmp_path), logger, "testhash")
    page = DummyPage()

    handle = DummyElementHandle(x=2358, y=200, width=500, height=300)

    # Mock _resolve_best_element_handle and _find_links_in_element to avoid full DOM
    monkeypatch.setattr(collector, "_resolve_best_element_handle", AsyncMock(return_value=handle))
    monkeypatch.setattr(collector, "_prefer_nested_iframe_handle", AsyncMock(return_value=None))
    monkeypatch.setattr(collector, "_find_links_in_element", AsyncMock(return_value=[{
        "adLinks": [],
        "adImages": [],
        "adVideos": [],
        "adOtherLinks": [],
        "adChoicesLinks": [],
        "adText": "Test Ad",
        "adLandingPage": "",
        "adIsImage": False,
        "adIsVideo": False,
        "adIsDynamic": False,
        "containsImgsOrLinks": True,
    }]))

    ad = {
        "nodeType": "ARTICLE",
        "id": "ad-carousel-item",
        "xpath": "/html/body/div[1]/article",
        "x": 2358,
        "y": 200,
        "width": 500,
        "height": 300,
        "rule": "text-label:advertisement",
        "action": "extracted",
    }

    status, record = await collector._capture_single_ad(page, ad, 1)

    assert status == "scraped"
    assert record is not None
    assert record["screenshot"] == "ad_1_testhash.png"
    assert record["contextScreenshot"] == "ad_1_testhash_context.png"
    assert record["contextBoundingBox"] != {}
    # Verified that handle was scrolled into view and x was shifted into viewport
    assert handle.scrolled_into_view is True
    assert record["x"] == 200

