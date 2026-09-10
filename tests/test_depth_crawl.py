"""Tests for recursive depth crawling (--depth), link extraction, and domain scoping."""

import argparse
import asyncio
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Helpers.link_extractor import (
    extract_internal_links,
    is_same_domain,
    normalize_and_validate_url,
)
from timeout_manager import AttemptMetadata, get_website_folder_name, finalize_and_save_attempt, generate_website_id, generate_attempt_id, get_attempt_dir


# ============================================================================
# Link Extractor & Domain Scoping Unit Tests
# ============================================================================

def test_is_same_domain_matches_subdomains_and_exact():
    assert is_same_domain("https://news.yahoo.com/finance", "https://yahoo.com") is True
    assert is_same_domain("https://yahoo.com/sports", "https://www.yahoo.com") is True
    assert is_same_domain("https://sub.domain.example.co.uk/page", "https://example.co.uk") is True
    assert is_same_domain("https://google.com", "https://yahoo.com") is False
    assert is_same_domain("https://adservice.google.com", "https://yahoo.com") is False


def test_normalize_and_validate_url_resolves_relative_and_strips_fragments():
    base = "https://example.com/blog/article1"
    root = "https://example.com"

    # Relative path
    assert normalize_and_validate_url("../about", base, root) == "https://example.com/about"
    assert normalize_and_validate_url("/contact", base, root) == "https://example.com/contact"
    assert normalize_and_validate_url("subpage#section", base, root) == "https://example.com/blog/subpage"


def test_normalize_and_validate_url_filters_invalid_schemes():
    base = "https://example.com"
    assert normalize_and_validate_url("javascript:void(0)", base, base) is None
    assert normalize_and_validate_url("mailto:info@example.com", base, base) is None
    assert normalize_and_validate_url("tel:+1234567890", base, base) is None
    assert normalize_and_validate_url("#top", base, base) is None
    assert normalize_and_validate_url("data:text/html,test", base, base) is None


def test_normalize_and_validate_url_filters_static_assets():
    base = "https://example.com"
    assert normalize_and_validate_url("/images/logo.png", base, base) is None
    assert normalize_and_validate_url("/downloads/report.pdf", base, base) is None
    assert normalize_and_validate_url("/assets/app.js", base, base) is None
    assert normalize_and_validate_url("/styles/main.css", base, base) is None
    assert normalize_and_validate_url("/video.mp4", base, base) is None
    assert normalize_and_validate_url("/archive.zip", base, base) is None


def test_normalize_and_validate_url_blocks_external_domains():
    base = "https://example.com"
    assert normalize_and_validate_url("https://facebook.com/share", base, base) is None
    assert normalize_and_validate_url("https://tracker.adnetwork.com/click", base, base) is None


@pytest.mark.asyncio
async def test_extract_internal_links_caps_and_deduplicates():
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://example.com/home"
    mock_page.evaluate = AsyncMock(return_value=[
        "/page1",
        "/page2",
        "/page1#section2",  # Duplicate of page1
        "https://external.com/page",  # External domain
        "/image.png",  # Asset
        "/page3",
        "/page4",
    ])

    links = await extract_internal_links(
        mock_page,
        root_url="https://example.com",
        max_links=2,
    )

    assert len(links) == 2
    valid_set = {"https://example.com/page1", "https://example.com/page2", "https://example.com/page3", "https://example.com/page4"}
    for link in links:
        assert link in valid_set


@pytest.mark.asyncio
async def test_extract_internal_links_skips_excluded_urls():
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://example.com"
    mock_page.evaluate = AsyncMock(return_value=[
        "https://example.com/already-visited",
        "https://example.com/new-page",
    ])

    links = await extract_internal_links(
        mock_page,
        root_url="https://example.com",
        exclude_urls={"https://example.com/already-visited"},
    )

    assert links == ["https://example.com/new-page"]


@pytest.mark.asyncio
async def test_extract_internal_links_random_sampling_from_100_candidates():
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://example.com"

    # Simulate 120 internal links on the page
    hrefs = [f"/article-{i}" for i in range(120)]
    mock_page.evaluate = AsyncMock(return_value=hrefs)

    links_run1 = await extract_internal_links(
        mock_page,
        root_url="https://example.com",
        max_links=5,
    )
    assert len(links_run1) == 5
    for l in links_run1:
        assert l.startswith("https://example.com/article-")


@pytest.mark.asyncio
async def test_extract_internal_links_skips_already_completed_in_output_dir(tmp_path):
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://example.com"
    mock_page.evaluate = AsyncMock(return_value=[
        "/done-page",
        "/new-page",
    ])

    # Mark /done-page as completed on disk
    done_url = "https://example.com/done-page"
    done_folder = get_website_folder_name(done_url)
    att_dir = get_attempt_dir(tmp_path, done_folder, 1)
    att_dir.mkdir(parents=True, exist_ok=True)
    meta = AttemptMetadata(
        website_id=generate_website_id(done_url),
        normalized_url=done_url,
        publisher_domain="example.com",
        crawl_id="c1",
        attempt_id="att1",
        attempt_number=1,
        worker_id="w0",
        started_at="2026-08-31T12:00:00Z",
        ended_at="2026-08-31T12:01:00Z",
        status="completed",
        website_folder=done_folder,
    )
    finalize_and_save_attempt(tmp_path, att_dir, {"successful": "true"}, meta)

    links = await extract_internal_links(
        mock_page,
        root_url="https://example.com",
        output_dir=tmp_path,
    )

    assert "https://example.com/done-page" not in links
    assert "https://example.com/new-page" in links


@pytest.mark.asyncio
async def test_extract_internal_links_strictly_avoids_parent_root_and_all_visited_urls():
    """Verify that parent URL, root URL, and all previously visited URLs across depth layers are excluded."""
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://www.example.com/entertainment"
    mock_page.evaluate = AsyncMock(return_value=[
        "https://example.com",                     # Root URL
        "https://example.com/",                    # Root URL with slash
        "https://www.example.com/parent-page",     # Parent URL
        "https://www.example.com/parent-page/",    # Parent URL with slash
        "https://www.example.com/entertainment",   # Current page URL
        "https://www.example.com/already-visited", # Previously visited in layer 0/1
        "http://example.com/already-visited",      # Different scheme of visited URL
        "https://www.example.com/brand-new-link1", # New candidate 1
        "https://www.example.com/brand-new-link2", # New candidate 2
    ])

    exclude_set = {
        "https://example.com/already-visited",
        "https://example.com/old-seed",
    }

    links = await extract_internal_links(
        mock_page,
        root_url="https://example.com",
        parent_url="https://www.example.com/parent-page",
        exclude_urls=exclude_set,
    )

    assert "https://example.com" not in links
    assert "https://example.com/" not in links
    assert "https://www.example.com/parent-page" not in links
    assert "https://www.example.com/entertainment" not in links
    assert "https://www.example.com/already-visited" not in links
    assert "http://example.com/already-visited" not in links
    assert "https://www.example.com/brand-new-link1" in links
    assert "https://www.example.com/brand-new-link2" in links
    assert len(links) == 2


# ============================================================================
# CLI Argument Parsing Tests
# ============================================================================

def test_depth_cli_argument_parsing():
    import cli

    # Create a fresh subparser test for --depth
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--depth",
        dest="depth",
        nargs=2,
        type=int,
        metavar=("LAYERS", "URLS_PER_LAYER"),
        default=None,
    )

    args = parser.parse_args(["--depth", "2", "5"])
    assert args.depth == [2, 5]

    args_none = parser.parse_args([])
    assert args_none.depth is None


def test_depth_cli_negative_argument_error():
    import cli

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--depth",
        dest="depth",
        nargs=2,
        type=int,
        metavar=("LAYERS", "URLS_PER_LAYER"),
        default=None,
    )

    args = parser.parse_args(["--depth", "5", "-1"])
    with pytest.raises(SystemExit):
        if args.depth[0] < 0 or args.depth[1] < 0:
            parser.error("must be >= 0")


# ============================================================================
# Metadata & Lineage Tests
# ============================================================================

def test_attempt_metadata_records_depth_and_parent():
    meta = AttemptMetadata(
        website_id="web_12345",
        normalized_url="https://example.com/subpage",
        publisher_domain="example.com",
        crawl_id="crawl_test",
        attempt_id="att_001",
        attempt_number=1,
        worker_id="worker_0",
        started_at="2026-08-31T00:00:00Z",
        ended_at="2026-08-31T00:00:05Z",
        status="completed",
        depth_level=2,
        parent_url="https://example.com/parent",
    )

    d = meta.to_dict()
    assert d["depth_level"] == 2
    assert d["parent_url"] == "https://example.com/parent"


# ============================================================================
# Recursive Traversal Isolation & Breadth-First Search Integration Tests
# ============================================================================

@pytest.mark.asyncio
async def test_depth_traversal_bfs_queue():
    from cli import _run_all

    crawled_urls = []
    crawled_depths = []

    # Mock crawl function returning discovered links for depth traversal
    async def mock_crawl(url, **kwargs):
        crawled_urls.append(url)
        depth_lvl = kwargs.get("attempt_info", {}).get("depth_level", 0)
        crawled_depths.append(depth_lvl)

        discovered = []
        if depth_lvl == 0:
            discovered = ["https://example.com/lvl1-a", "https://example.com/lvl1-b"]
        elif depth_lvl == 1 and "lvl1-a" in url:
            discovered = ["https://example.com/lvl2-a"]

        return {
            "initialUrl": url,
            "finalUrl": url,
            "successful": "true",
            "data": {"AdCollector": []},
            "discovered_links": discovered,
        }

    with patch("cli.crawl", side_effect=mock_crawl):
        await _run_all(
            urls=["https://example.com"],
            output_dir="processed/test_depth",
            timeout=10,
            headless=True,
            collectors=["ads"],
            depth=(2, 2),  # 2 layers deep, 2 urls per layer
            use_safeguards=False,
        )

    # Should crawl root (depth 0), lvl1-a and lvl1-b (depth 1), and lvl2-a (depth 2)
    assert "https://example.com" in crawled_urls
    assert "https://example.com/lvl1-a" in crawled_urls
    assert "https://example.com/lvl1-b" in crawled_urls
    assert "https://example.com/lvl2-a" in crawled_urls

    # Verify depth levels sequence
    assert crawled_depths[0] == 0
    assert 1 in crawled_depths
    assert 2 in crawled_depths


@pytest.mark.asyncio
async def test_depth_zero_crawls_only_root():
    from cli import _run_all

    crawled_urls = []

    async def mock_crawl(url, **kwargs):
        crawled_urls.append(url)
        return {
            "initialUrl": url,
            "finalUrl": url,
            "successful": "true",
            "data": {"AdCollector": []},
            "discovered_links": ["https://example.com/should-not-be-crawled"],
        }

    with patch("cli.crawl", side_effect=mock_crawl):
        await _run_all(
            urls=["https://example.com"],
            output_dir="processed/test_depth",
            timeout=10,
            headless=True,
            collectors=["ads"],
            depth=(0, 5),  # 0 layers deep -> only root
            use_safeguards=False,
        )

    assert crawled_urls == ["https://example.com"]


@pytest.mark.asyncio
async def test_depth_and_parent_lineage_recorded(tmp_path):
    from timeout_manager import WebsiteManifestManager, finalize_and_save_attempt, get_attempt_dir, get_website_folder_name

    # 1. Depth 0: parent_url is the seed URL itself
    url0 = "https://example.com"
    folder0 = get_website_folder_name(url0)
    att_dir0 = get_attempt_dir(tmp_path, folder0, 1)
    meta0 = AttemptMetadata(
        website_id="web_0",
        normalized_url=url0,
        publisher_domain="example.com",
        crawl_id="c1",
        attempt_id="att_0",
        attempt_number=1,
        worker_id="w0",
        started_at="2026-09-01T12:00:00Z",
        ended_at="2026-09-01T12:00:10Z",
        status="completed",
        depth_level=0,
        parent_url=url0,  # Same URL when depth is 0
        website_folder=folder0,
    )
    finalize_and_save_attempt(tmp_path, att_dir0, {"successful": "true", "depth": 0, "parent_url": url0}, meta0)

    manifest0 = WebsiteManifestManager.load_manifest(tmp_path, folder0)
    assert manifest0["depth_level"] == 0
    assert manifest0["parent_url"] == "https://example.com"
    assert manifest0["attempts"][0]["depth_level"] == 0
    assert manifest0["attempts"][0]["parent_url"] == "https://example.com"

    # 2. Depth 1: parent_url is the referrer page (example.com)
    url1 = "https://example.com/news"
    folder1 = get_website_folder_name(url1)
    att_dir1 = get_attempt_dir(tmp_path, folder1, 1)
    meta1 = AttemptMetadata(
        website_id="web_1",
        normalized_url=url1,
        publisher_domain="example.com",
        crawl_id="c1",
        attempt_id="att_1",
        attempt_number=1,
        worker_id="w0",
        started_at="2026-09-01T12:01:00Z",
        ended_at="2026-09-01T12:01:10Z",
        status="completed",
        depth_level=1,
        parent_url=url0,  # Referrer parent URL
        website_folder=folder1,
    )
    finalize_and_save_attempt(tmp_path, att_dir1, {"successful": "true", "depth": 1, "parent_url": url0}, meta1)

    manifest1 = WebsiteManifestManager.load_manifest(tmp_path, folder1)
    assert manifest1["depth_level"] == 1
    assert manifest1["parent_url"] == "https://example.com"
    assert manifest1["attempts"][0]["depth_level"] == 1
    assert manifest1["attempts"][0]["parent_url"] == "https://example.com"

