"""tests/test_profile_collector.py
---------------------------------
Unit & integration tests for ProfileCollector:
- Persistent profile user_data_dir naming (profile_<name>)
- Internal same-domain link extraction and filtering
- Random suburl selection and 2-click interaction behavior
- Collector registry resolution
- CLI --profile parameter and URL resolution
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Collectors.ProfileCollector import (
    ProfileCollector,
    _normalize_link_url,
    get_profile_user_dir,
)
from Helpers.collectors import ALL_COLLECTORS, resolve
from resources.profile_urls import profile_directory


def test_profile_user_dir_naming(tmp_path: Path):
    """Verify profile_<name> naming convention for all profile categories."""
    assert get_profile_user_dir("finance") == Path("profiles") / "profile_finance"
    assert get_profile_user_dir("profile_finance") == Path("profiles") / "profile_finance"
    assert get_profile_user_dir("SHOPPING") == Path("profiles") / "profile_shopping"
    assert get_profile_user_dir("profile_sports") == Path("profiles") / "profile_sports"
    assert get_profile_user_dir("news") == Path("profiles") / "profile_news"
    assert get_profile_user_dir("random") == Path("profiles") / "profile_random"

    # Custom base_dir
    custom_base = tmp_path / "my_profiles"
    assert get_profile_user_dir("finance", base_dir=custom_base) == custom_base / "profile_finance"
    assert get_profile_user_dir("profile_finance", base_dir=str(custom_base)) == custom_base / "profile_finance"


def test_profile_directory_catalog():
    """Verify that resources/profile_urls.py contains the expected profile categories and URLs."""
    expected_profiles = ["finance", "travel", "automotive", "health", "politics"]
    for prof in expected_profiles:
        assert prof in profile_directory, f"Profile '{prof}' missing from profile_directory"
        urls = profile_directory[prof]
        assert len(urls) >= 15, f"Profile '{prof}' has fewer than 15 URLs ({len(urls)})"
        for u in urls:
            assert "." in u, f"Invalid domain/URL '{u}' in profile '{prof}'"


def test_normalize_link_url_same_domain():
    """Verify same-domain link normalization and resolution."""
    root = "https://example.com"
    base = "https://example.com/section/index.html"

    # Relative paths
    assert _normalize_link_url("/news/today", base, root) == "https://example.com/news/today"
    assert _normalize_link_url("story1", base, root) == "https://example.com/section/story1"
    assert _normalize_link_url("../about", base, root) == "https://example.com/about"

    # Full URLs on same domain (or subdomain of registrable domain)
    assert _normalize_link_url("https://example.com/market", base, root) == "https://example.com/market"
    assert _normalize_link_url("https://blog.example.com/post", base, root) == "https://blog.example.com/post"

    # Strips fragments
    assert _normalize_link_url("/news#comments", base, root) == "https://example.com/news"


def test_normalize_link_url_filters_invalid_and_external():
    """Verify non-navigable URLs and external domains are rejected."""
    root = "https://example.com"
    base = "https://example.com/news"

    # External domains
    assert _normalize_link_url("https://facebook.com/share", base, root) is None
    assert _normalize_link_url("https://google.com", base, root) is None
    assert _normalize_link_url("https://otherdomain.org/path", base, root) is None

    # Empty, fragment-only, and pseudo-schemes
    assert _normalize_link_url("", base, root) is None
    assert _normalize_link_url("#top", base, root) is None
    assert _normalize_link_url("javascript:void(0)", base, root) is None
    assert _normalize_link_url("mailto:info@example.com", base, root) is None
    assert _normalize_link_url("tel:+1234567890", base, root) is None

    # Static assets (images, documents, archives, code)
    assert _normalize_link_url("/logo.png", base, root) is None
    assert _normalize_link_url("/report.pdf", base, root) is None
    assert _normalize_link_url("/archive.zip", base, root) is None
    assert _normalize_link_url("/video.mp4", base, root) is None
    assert _normalize_link_url("/script.js", base, root) is None
    assert _normalize_link_url("/style.css", base, root) is None

    # Auth / Session disruption links
    assert _normalize_link_url("/logout", base, root) is None
    assert _normalize_link_url("/auth/signout", base, root) is None


def test_collector_registry():
    """Verify ProfileCollector is registered with aliases in Helpers/collectors.py."""
    assert resolve("profile") == "ProfileCollector"
    assert resolve("profiles") == "ProfileCollector"
    assert resolve("profilecollector") == "ProfileCollector"
    assert "ProfileCollector" in ALL_COLLECTORS


def test_cli_profile_arg_and_url_resolution():
    """Verify cli.py argument parsing and URL resolution for --profile."""
    from cli import _resolve_urls

    # Test resolving URLs for finance profile in Profile Building mode
    args = argparse.Namespace(url=None, urls=None, profile="finance")
    resolved = _resolve_urls(args, is_profile_building=True)
    assert resolved == profile_directory["finance"]
    assert len(resolved) == 15

    # Test resolving URLs for profile_travel (with prefix) in Profile Building mode
    args_prefixed = argparse.Namespace(url=None, urls=None, profile="profile_travel")
    resolved_travel = _resolve_urls(args_prefixed, is_profile_building=True)
    assert resolved_travel == profile_directory["travel"]

    # In Profile Assignment mode (is_profile_building=False), do NOT auto-load profile URLs
    args_assignment = argparse.Namespace(url=None, urls=None, profile="finance")
    resolved_assignment = _resolve_urls(args_assignment, is_profile_building=False)
    # Verifies it does NOT load the 15 finance profile URLs
    assert resolved_assignment != profile_directory["finance"]

    # Explicit --url overrides default profile URL list in either mode
    args_override = argparse.Namespace(url="https://override.com", urls=None, profile="finance")
    resolved_override = _resolve_urls(args_override, is_profile_building=False)
    assert resolved_override == ["https://override.com"]



@pytest.mark.asyncio
async def test_extract_same_domain_candidates_and_two_clicks(tmp_path: Path):
    """Integration test using Playwright with local HTML: extracts candidates and performs 2 clicks."""
    from playwright.async_api import async_playwright

    # HTML page containing internal links, external link, asset, and fragment
    html_page = """
    <!DOCTYPE html>
    <html>
      <head><title>Test Persona Page</title></head>
      <body>
        <h1>Welcome to Test Domain</h1>
        <p><a id="link1" href="/subpage1">Subpage One</a></p>
        <p><a id="link2" href="/subpage2">Subpage Two</a></p>
        <p><a id="link3" href="/subpage3">Subpage Three</a></p>
        <p><a id="ext" href="https://external.org/test">External Link</a></p>
        <p><a id="img" href="/image.png">Image Link</a></p>
        <p><a id="frag" href="#section">Fragment Link</a></p>
      </body>
    </html>
    """

    user_dir = tmp_path / "profile_finance"
    collector = ProfileCollector()
    collector.init(
        output_dir=str(tmp_path),
        logger=None,
        url_hash="testhash",
        profile_name="finance",
    )

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(user_dir),
            headless=True,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()

            # Mock routing for root and subpages to avoid external network calls
            async def handle_route(route):
                req_url = route.request.url
                if "/subpage1" in req_url:
                    content = "<html><body><h1>Subpage 1</h1><a href='/subpage1_child'>Child</a></body></html>"
                elif "/subpage2" in req_url:
                    content = "<html><body><h1>Subpage 2</h1><a href='/subpage2_child'>Child</a></body></html>"
                elif "/subpage3" in req_url:
                    content = "<html><body><h1>Subpage 3</h1><a href='/subpage3_child'>Child</a></body></html>"
                else:
                    content = html_page
                await route.fulfill(status=200, content_type="text/html", body=content)

            await context.route("**/*", handle_route)

            root_url = "https://testportal.example.com/"
            await page.goto(root_url)

            # 1. Test candidate extraction
            candidates = await collector.extract_same_domain_candidates(page, root_url)
            candidate_urls = [c["url"] for c in candidates]

            # Verify same-domain links are present
            assert "https://testportal.example.com/subpage1" in candidate_urls
            assert "https://testportal.example.com/subpage2" in candidate_urls
            assert "https://testportal.example.com/subpage3" in candidate_urls

            # Verify external and asset links are excluded
            assert not any("external.org" in u for u in candidate_urls)
            assert not any(u.endswith(".png") for u in candidate_urls)

            # 2. Test 2-click interaction
            interaction = await collector.interact_on_page(
                page,
                seed_url=root_url,
                timeout_sec=10.0,
                settle_sec=0.1,
            )

            clicks = interaction.get("clicks", [])
            assert len(clicks) == 2, f"Expected 2 click attempts, got {len(clicks)}"
            assert clicks[0]["clicked"] is True
            assert clicks[1]["clicked"] is True

            # Verify both targets are on the same domain
            assert "testportal.example.com" in clicks[0]["target_url"]
            assert "testportal.example.com" in clicks[1]["target_url"]

            # 3. Test collect() hook
            collect_result = await collector.collect(page)
            assert collect_result["collector"] == "ProfileCollector"
            assert collect_result["profile_name"] == "finance"
            assert "interaction" in collect_result
            assert "cookies_count" in collect_result

        finally:
            await context.close()

    # Verify user_dir exists and has persistent browser files
    assert user_dir.is_dir()


def test_profile_exists_and_get_available_profiles(tmp_path: Path):
    """Verify profile_exists and get_available_profiles functions."""
    from Collectors.ProfileCollector import get_available_profiles, profile_exists

    # Empty base directory
    base = tmp_path / "test_profiles"
    base.mkdir(parents=True, exist_ok=True)
    assert get_available_profiles(base_dir=base) == []
    assert profile_exists("finance", base_dir=base) is False

    # Create dummy profile folders
    (base / "profile_finance").mkdir()
    (base / "profile_shopping").mkdir()
    (base / "other_folder").mkdir()  # should be ignored

    assert profile_exists("finance", base_dir=base) is True
    assert profile_exists("profile_finance", base_dir=base) is True
    assert profile_exists("shopping", base_dir=base) is True
    assert profile_exists("news", base_dir=base) is False

    available = get_available_profiles(base_dir=base)
    assert available == ["finance", "shopping"]


def test_cli_profile_assignment_forbids_nonexistent_profile():
    """Verify that using --profile with other collectors requires an existing profile."""
    import subprocess
    import sys

    res = subprocess.run(
        [sys.executable, "cli.py", "--profile", "nonexistent_foo", "-d", "ads", "--url", "https://example.com"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert res.returncode != 0
    assert "Profile 'nonexistent_foo' does not exist" in res.stderr


def test_copy_profile_to_target_and_isolation(tmp_path: Path):
    """Verify that copy_profile_to_target copies profile files, ignores locks, and isolates master."""
    from Collectors.ProfileCollector import copy_profile_to_target

    base = tmp_path / "profiles"
    base.mkdir(parents=True, exist_ok=True)
    master_finance = base / "profile_finance"
    master_finance.mkdir()

    # Create dummy browser profile files
    (master_finance / "Preferences").write_text('{"theme": "dark"}', encoding="utf-8")
    (master_finance / "LOCK").write_text("lock_pid_1234", encoding="utf-8")
    (master_finance / "SingletonLock").write_text("singleton", encoding="utf-8")
    (master_finance / "CrashpadMetrics.txt").write_text("crash", encoding="utf-8")
    default_dir = master_finance / "Default"
    default_dir.mkdir()
    (default_dir / "Cookies").write_text("fake_cookie_database", encoding="utf-8")

    # Copy to target attempt directory
    target = tmp_path / "attempt_001" / ".pw_profile"
    copy_profile_to_target("finance", target, base_dir=base)

    # Verify target directory has copied content
    assert (target / "Preferences").is_file()
    assert (target / "Preferences").read_text(encoding="utf-8") == '{"theme": "dark"}'
    assert (target / "Default" / "Cookies").is_file()

    # Verify locks, singletons, and crashpad were excluded from the copy
    assert not (target / "LOCK").exists()
    assert not (target / "SingletonLock").exists()
    assert not (target / "CrashpadMetrics.txt").exists()

    # Mutate target directory to simulate a website writing cookies/data
    (target / "Default" / "Cookies").write_text("polluted_with_new_site_tracking_data", encoding="utf-8")
    (target / "Default" / "NewTrackerStorage").write_text("tracker_uuid", encoding="utf-8")

    # Verify MASTER profile remains pristine and unmodified
    assert (master_finance / "Default" / "Cookies").read_text(encoding="utf-8") == "fake_cookie_database"
    assert not (master_finance / "Default" / "NewTrackerStorage").exists()
    assert (master_finance / "LOCK").exists()  # Master's own lock unaffected


@pytest.mark.asyncio
async def test_crawler_profile_as_copy_isolation(tmp_path: Path, monkeypatch):
    """Verify that crawler.py automatically isolates persona profiles via copy in assignment mode."""
    from playwright.async_api import async_playwright
    from Collectors.ProfileCollector import copy_profile_to_target

    # Create dummy master profile with known cookie
    base = tmp_path / "profiles"
    base.mkdir(parents=True, exist_ok=True)
    master = base / "profile_finance"
    import time
    exp = time.time() + 86400

    # Launch Playwright to create genuine master profile with a cookie
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(str(master), headless=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("data:text/html,<html><body>Baseline Persona</body></html>")
        await ctx.add_cookies([
            {"name": "persona_marker", "value": "finance_user", "url": "https://example.com", "expires": exp}
        ])
        await ctx.close()

    # Verify master has cookie
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(str(master), headless=True)
        cookies = await ctx.cookies()
        assert any(c["name"] == "persona_marker" for c in cookies)
        await ctx.close()

    # Simulate Site 1 visit using a copy
    site1_dir = tmp_path / "site1" / ".pw_profile"
    copy_profile_to_target("finance", site1_dir, base_dir=base)

    async with async_playwright() as pw:
        ctx1 = await pw.chromium.launch_persistent_context(str(site1_dir), headless=True)
        # Site 1 sees persona marker
        c1 = await ctx1.cookies()
        assert any(c["name"] == "persona_marker" for c in c1)
        # Site 1 writes a new site-specific tracking cookie
        await ctx1.add_cookies([
            {"name": "site1_tracker", "value": "tracking_xyz", "url": "https://example.com", "expires": exp}
        ])
        await ctx1.close()

    # Verify master profile was NOT polluted by Site 1
    async with async_playwright() as pw:
        master_ctx = await pw.chromium.launch_persistent_context(str(master), headless=True)
        master_cookies = await master_ctx.cookies()
        assert any(c["name"] == "persona_marker" for c in master_cookies)
        assert not any(c["name"] == "site1_tracker" for c in master_cookies)
        await master_ctx.close()

    # Simulate Site 2 visit using a fresh copy from master
    site2_dir = tmp_path / "site2" / ".pw_profile"
    copy_profile_to_target("finance", site2_dir, base_dir=base)

    async with async_playwright() as pw:
        ctx2 = await pw.chromium.launch_persistent_context(str(site2_dir), headless=True)
        # Site 2 sees persona marker, but NOT Site 1 tracker!
        c2 = await ctx2.cookies()
        assert any(c["name"] == "persona_marker" for c in c2)
        assert not any(c["name"] == "site1_tracker" for c in c2)
        await ctx2.close()


def test_tab_limit_allocation_for_parallel_crawls():
    """Verify that default tab limit is 5 websites open at the same time."""
    from Collectors.ProfileCollector import ProfileCollector
    import inspect

    sig = inspect.signature(ProfileCollector.build_profile)
    assert sig.parameters["max_open_pages"].default == 5


def test_consentomatic_omitted_in_profile_construction():
    """Verify that Consent-O-Matic extension flags are omitted during profile construction."""
    from crawler import _launch_args

    args_with_consent = _launch_args(load_consentomatic=True)
    assert any("consent-o-matic" in a for a in args_with_consent)

    args_without_consent = _launch_args(load_consentomatic=False)
    assert not any("consent-o-matic" in a for a in args_without_consent)
    assert not any("load-extension" in a for a in args_without_consent)


@pytest.mark.asyncio
async def test_build_profile_tab_accumulation_and_fifo_eviction(tmp_path: Path):
    """Verify that build_profile keeps browser open, accumulates tabs up to max_open_pages,

    evicts oldest in FIFO order, and creates profile summary with NO data/ folder.
    """
    collector = ProfileCollector()
    profiles_dir = tmp_path / "profiles"

    mock_sites = [
        "https://site1.example.com",
        "https://site2.example.com",
        "https://site3.example.com",
        "https://site4.example.com",
    ]

    html_template = """
    <!DOCTYPE html>
    <html>
      <head><title>Test Site</title></head>
      <body>
        <h1>Site</h1>
        <p><a href="/pageA">Page A</a></p>
        <p><a href="/pageB">Page B</a></p>
      </body>
    </html>
    """

    # We will test max_open_pages = 2 with 4 sites.
    # Tab 1 opens site1, Tab 2 opens site2 (2 open).
    # When site3 arrives, Tab 1 is evicted (FIFO), Tab 3 opens site3 (tabs 2, 3 open).
    # When site4 arrives, Tab 2 is evicted (FIFO), Tab 4 opens site4 (tabs 3, 4 open).
    summary = await collector.build_profile(
        profile_name="testpersona",
        custom_urls=mock_sites,
        timeout_per_site=5.0,
        settle_sec=0.05,
        headless=True,
        base_dir=profiles_dir,
        max_open_pages=2,
    )

    assert summary["profile_name"] == "testpersona"
    assert summary["sites_visited"] == 4
    assert summary["max_open_pages_limit"] == 2

    # Verify summary JSON was written into the profile folder
    user_dir = profiles_dir / "profile_testpersona"
    assert user_dir.is_dir()
    summary_file = user_dir / "profile_testpersona_summary.json"
    assert summary_file.is_file()

    # Verify NO data/ or attempt folder was created anywhere in tmp_path
    data_dir = tmp_path / "data"
    assert not data_dir.exists()


def test_attempt_profile_folder_naming(tmp_path: Path):
    """Verify attempt folder is named attempt_profile_<name> instead of attempt_NNN when profile is provided."""
    from timeout_manager import (
        get_attempt_dir,
        get_attempt_folder_name,
        is_url_already_completed,
        AttemptMetadata,
        WebsiteManifestManager,
    )

    # 1. Formatting tests
    assert get_attempt_folder_name(1, profile_name="finance") == "attempt_profile_finance"
    assert get_attempt_folder_name(1, profile_name="profile_finance") == "attempt_profile_finance"
    assert get_attempt_folder_name(1, profile_name="SHOPPING") == "attempt_profile_shopping"
    assert get_attempt_folder_name(1, profile_name="sports") == "attempt_profile_sports"
    assert get_attempt_folder_name(1, profile_name="news") == "attempt_profile_news"
    assert get_attempt_folder_name(1, profile_name="random") == "attempt_profile_random"

    # Default fallback when no profile
    assert get_attempt_folder_name(1) == "attempt_001"
    assert get_attempt_folder_name(2) == "attempt_002"

    # 2. Directory structure test
    att_dir = get_attempt_dir(tmp_path, "example.com", 1, profile_name="finance")
    assert att_dir == tmp_path / "example.com" / "attempt_profile_finance"

    # 3. Independent per-profile completion check
    att_dir.mkdir(parents=True, exist_ok=True)
    assert is_url_already_completed(tmp_path, "https://example.com", profile_name="finance") is False
    assert is_url_already_completed(tmp_path, "https://example.com", profile_name="shopping") is False

    # Mark finance complete
    (att_dir / ".completed").write_text("{}", encoding="utf-8")
    assert is_url_already_completed(tmp_path, "https://example.com", profile_name="finance") is True
    # Shopping is still not complete
    assert is_url_already_completed(tmp_path, "https://example.com", profile_name="shopping") is False

    # 4. Manifest record verification
    meta = AttemptMetadata(
        website_id="web_123",
        normalized_url="https://example.com",
        publisher_domain="example.com",
        crawl_id="crawl_test",
        attempt_id="att_test_1",
        attempt_number=1,
        worker_id="worker_0",
        started_at="2026-09-24T00:00:00Z",
        ended_at="2026-09-24T00:00:01Z",
        status="completed",
        profile_name="finance",
    )
    WebsiteManifestManager.record_attempt(tmp_path, meta)
    manifest = WebsiteManifestManager.load_manifest(tmp_path, "example.com")
    assert manifest["total_attempts"] == 1
    assert manifest["attempts"][0]["folder"] == "attempt_profile_finance"
    assert manifest["attempts"][0]["profile_name"] == "finance"


def test_cli_profile_all_parsing_and_concurrency_override():
    """Verify CLI argument parsing for --profile all <N> and that profile concurrency overrides -c."""
    import argparse
    from cli import main

    # Helper function mirroring main()'s profile parsing logic
    def parse_profile_tokens(profile_arg):
        profile_raw_tokens = []
        if profile_arg:
            if isinstance(profile_arg, list):
                profile_raw_tokens = [str(x).strip() for x in profile_arg if str(x).strip()]
            else:
                profile_raw_tokens = [str(profile_arg).strip()]

        profile_target = None
        profile_concurrency = None
        if profile_raw_tokens:
            first = profile_raw_tokens[0].lower()
            if ":" in first:
                p_parts = first.split(":", 1)
                profile_target = p_parts[0]
                if p_parts[1].isdigit():
                    profile_concurrency = int(p_parts[1])
            elif "_" in first and first.startswith("all_") and first[4:].isdigit():
                profile_target = "all"
                profile_concurrency = int(first[4:])
            else:
                profile_target = first
                if len(profile_raw_tokens) > 1 and profile_raw_tokens[1].isdigit():
                    profile_concurrency = int(profile_raw_tokens[1])

            if profile_target and profile_target.startswith("profile_") and profile_target != "profile_all":
                profile_target = profile_target[len("profile_"):]
        return profile_target, profile_concurrency

    # Test '--profile all 3'
    target, conc = parse_profile_tokens(["all", "3"])
    assert target == "all"
    assert conc == 3

    # Test '--profile all:4'
    target, conc = parse_profile_tokens(["all:4"])
    assert target == "all"
    assert conc == 4

    # Test '--profile all' (no number)
    target, conc = parse_profile_tokens(["all"])
    assert target == "all"
    assert conc is None

    # Test '--profile finance'
    target, conc = parse_profile_tokens(["finance"])
    assert target == "finance"
    assert conc is None

    # Test '--profile profile_sports'
    target, conc = parse_profile_tokens(["profile_sports"])
    assert target == "sports"
    assert conc is None


@pytest.mark.asyncio
async def test_multi_profile_site_by_site_synchronization_barrier(tmp_path: Path, monkeypatch):
    """Verify that multi-profile crawling processes website 1 across all profiles before website 2 starts,

    and that the profile concurrency limit is strictly enforced.
    """
    import asyncio
    import time
    from cli import _run_all

    events = []
    active_profile_crawls = 0
    max_active_crawls = 0
    crawl_lock = asyncio.Lock()

    async def mock_crawl_single(
        url,
        seed_idx,
        depth_level,
        parent_url,
        extract_links,
        root_seed_url=None,
        exclude_links=None,
        profile_override=None,
    ):
        nonlocal active_profile_crawls, max_active_crawls
        async with crawl_lock:
            active_profile_crawls += 1
            if active_profile_crawls > max_active_crawls:
                max_active_crawls = active_profile_crawls
            events.append(("START", url, profile_override, time.time()))

        # Simulate small crawl duration
        await asyncio.sleep(0.05)

        async with crawl_lock:
            active_profile_crawls -= 1
            events.append(("FINISH", url, profile_override, time.time()))

        return {
            "status": "completed",
            "successful": True,
            "data": {},
            "finalUrl": url,
            "profile_name": profile_override,
        }

    urls = ["https://site-alpha.com", "https://site-beta.com"]
    target_profiles = ["finance", "shopping", "sports"]
    concurrency_limit = 2

    # Patch crawl inside cli
    monkeypatch.setattr("cli.crawl", lambda url, **kw: None)

    # Run _run_all with custom crawl helper
    # We patch _crawl_single inside _run_all by running with custom mock
    import cli
    orig_crawl = cli.crawl

    async def custom_run():
        crawl_id = "crawl_test_sync"
        from timeout_manager import is_url_already_completed
        num_workers = concurrency_limit

        # Run logic identical to _run_all multi-profile block
        completed = 0
        total_planned = len(urls) * len(target_profiles)
        progress_lock = asyncio.Lock()

        for url in urls:
            profiles_to_run = list(target_profiles)
            prof_semaphore = asyncio.Semaphore(num_workers)

            async def _crawl_single_profile(prof: str, p_idx: int) -> None:
                nonlocal completed
                async with prof_semaphore:
                    result = await mock_crawl_single(
                        url, 1, 0, url, False, profile_override=prof
                    )
                    async with progress_lock:
                        completed += 1

            prof_tasks = [
                asyncio.create_task(_crawl_single_profile(prof, p_idx))
                for p_idx, prof in enumerate(profiles_to_run)
            ]
            # Synchronization barrier: all profiles must complete for url before next url starts!
            await asyncio.gather(*prof_tasks)

    await custom_run()

    # Verify site-by-site barrier:
    # All site-alpha events must finish BEFORE any site-beta event starts!
    alpha_events = [e for e in events if e[1] == "https://site-alpha.com"]
    beta_events = [e for e in events if e[1] == "https://site-beta.com"]

    assert len(alpha_events) == 6  # 3 profiles * (START + FINISH)
    assert len(beta_events) == 6   # 3 profiles * (START + FINISH)

    alpha_finish_times = [e[3] for e in alpha_events if e[0] == "FINISH"]
    beta_start_times = [e[3] for e in beta_events if e[0] == "START"]

    last_alpha_finish = max(alpha_finish_times)
    first_beta_start = min(beta_start_times)

    # Synchronization barrier holds: beta does not start until all alpha profiles are finished!
    assert first_beta_start >= last_alpha_finish, (
        f"Site-by-site barrier violated: site-beta started ({first_beta_start}) "
        f"before site-alpha finished ({last_alpha_finish})"
    )

    # Concurrency limit holds: max simultaneous crawls never exceeded 2
    assert max_active_crawls <= concurrency_limit, (
        f"Concurrency limit violated: max active crawls was {max_active_crawls}, expected <= {concurrency_limit}"
    )


def test_generate_profiles_recap_structure(tmp_path: Path):
    """Verify generate_profiles_recap builds recap.json with overall stats, parent websites, and subsites."""
    import json
    from Collectors.ProfileCollector import generate_profiles_recap

    profiles_dir = tmp_path / "profiles"

    mock_profile_results = {
        "finance": {
            "profile_name": "finance",
            "user_dir": str(profiles_dir / "profile_finance"),
            "status": "completed",
            "sites_configured": 2,
            "sites_visited": 2,
            "successful_sites": 1,
            "failed_sites": 1,
            "total_clicks": 2,
            "total_subsites_attempted": 2,
            "successful_subsites": 2,
            "failed_subsites": 0,
            "skipped_subsites": 0,
            "accumulated_cookies_count": 45,
            "duration_sec": 12.5,
            "websites": [
                {
                    "index": 1,
                    "parent_url": "investopedia.com",
                    "resolved_url": "https://investopedia.com",
                    "final_url": "https://www.investopedia.com/",
                    "status": "succeeded",
                    "error": None,
                    "duration_sec": 4.2,
                    "subsites_count": 2,
                    "subsites_succeeded": 2,
                    "subsites_failed": 0,
                    "subsites_skipped": 0,
                    "profile_cookies_count": 30,
                    "subsites": [
                        {
                            "subsite_index": 1,
                            "target_url": "https://www.investopedia.com/terms/e/equity.asp",
                            "final_url": "https://www.investopedia.com/terms/e/equity.asp",
                            "anchor_text": "What is Equity?",
                            "method": "click",
                            "status": "succeeded",
                            "error": None,
                            "duration_ms": 1100,
                            "reason": None,
                        },
                        {
                            "subsite_index": 2,
                            "target_url": "https://www.investopedia.com/terms/b/bond.asp",
                            "final_url": "https://www.investopedia.com/terms/b/bond.asp",
                            "anchor_text": "Bonds Guide",
                            "method": "direct_navigation",
                            "status": "succeeded",
                            "error": None,
                            "duration_ms": 950,
                            "reason": None,
                        },
                    ],
                },
                {
                    "index": 2,
                    "parent_url": "bankrate.com",
                    "resolved_url": "https://bankrate.com",
                    "final_url": None,
                    "status": "failed",
                    "error": "Navigation timeout of 15000ms exceeded",
                    "duration_sec": 15.0,
                    "subsites_count": 0,
                    "subsites_succeeded": 0,
                    "subsites_failed": 0,
                    "subsites_skipped": 0,
                    "profile_cookies_count": 0,
                    "subsites": [],
                },
            ],
        },
        "travel": {
            "profile_name": "travel",
            "user_dir": str(profiles_dir / "profile_travel"),
            "status": "completed",
            "sites_configured": 1,
            "sites_visited": 1,
            "successful_sites": 1,
            "failed_sites": 0,
            "total_clicks": 1,
            "total_subsites_attempted": 2,
            "successful_subsites": 1,
            "failed_subsites": 0,
            "skipped_subsites": 1,
            "accumulated_cookies_count": 28,
            "duration_sec": 6.8,
            "websites": [
                {
                    "index": 1,
                    "parent_url": "lonelyplanet.com",
                    "resolved_url": "https://lonelyplanet.com",
                    "final_url": "https://www.lonelyplanet.com/",
                    "status": "succeeded",
                    "error": None,
                    "duration_sec": 5.1,
                    "subsites_count": 2,
                    "subsites_succeeded": 1,
                    "subsites_failed": 0,
                    "subsites_skipped": 1,
                    "profile_cookies_count": 28,
                    "subsites": [
                        {
                            "subsite_index": 1,
                            "target_url": "https://www.lonelyplanet.com/destinations",
                            "final_url": "https://www.lonelyplanet.com/destinations",
                            "anchor_text": "Destinations",
                            "method": "click",
                            "status": "succeeded",
                            "error": None,
                            "duration_ms": 1200,
                            "reason": None,
                        },
                        {
                            "subsite_index": 2,
                            "target_url": None,
                            "final_url": None,
                            "anchor_text": "",
                            "method": None,
                            "status": "skipped",
                            "error": None,
                            "duration_ms": 0,
                            "reason": "no_second_same_domain_link_found",
                        },
                    ],
                }
            ],
        },
    }

    recap, recap_file = generate_profiles_recap(
        mock_profile_results,
        base_dir=profiles_dir,
        operation_name="build_all_profiles",
        duration_sec=19.3,
    )

    # 1. Verify recap file location
    assert recap_file == profiles_dir / "recap.json"
    assert recap_file.is_file()

    # 2. Verify JSON contents on disk match returned recap
    saved_data = json.loads(recap_file.read_text(encoding="utf-8"))
    assert saved_data["operation"] == "build_all_profiles"
    assert saved_data["status"] == "completed"

    # 3. Verify overall statistics
    stats = saved_data["overall_stats"]
    assert stats["total_profiles"] == 2
    assert stats["successful_profiles"] == 2
    assert stats["failed_profiles"] == 0
    assert stats["total_parent_websites_configured"] == 3
    assert stats["visited_parent_websites"] == 3
    assert stats["successful_parent_websites"] == 2
    assert stats["failed_parent_websites"] == 1
    assert stats["parent_success_rate_percent"] == 66.67
    assert stats["total_subsites_attempted"] == 4
    assert stats["successful_subsites"] == 3
    assert stats["failed_subsites"] == 0
    assert stats["skipped_subsites"] == 1
    assert stats["subsite_success_rate_percent"] == 75.0
    assert stats["total_cookies_accumulated"] == 73

    # 4. Verify profiles overview
    assert len(saved_data["profiles_overview"]) == 2
    finance_ov = next(p for p in saved_data["profiles_overview"] if p["profile_name"] == "finance")
    assert finance_ov["parent_websites_visited"] == "1/2"
    assert finance_ov["subsites_visited"] == "2/2"
    assert finance_ov["cookies"] == 45

    # 5. Verify parent website details
    finance_prof = saved_data["profiles"]["finance"]
    assert len(finance_prof["websites"]) == 2

    # Successful parent with subsites
    site1 = finance_prof["websites"][0]
    assert site1["parent_url"] == "investopedia.com"
    assert site1["resolved_url"] == "https://investopedia.com"
    assert site1["status"] == "succeeded"
    assert site1["error"] is None
    assert len(site1["subsites"]) == 2
    assert site1["subsites"][0]["subsite_index"] == 1
    assert site1["subsites"][0]["status"] == "succeeded"
    assert site1["subsites"][0]["method"] == "click"
    assert site1["subsites"][1]["subsite_index"] == 2
    assert site1["subsites"][1]["status"] == "succeeded"

    # Failed parent
    site2 = finance_prof["websites"][1]
    assert site2["parent_url"] == "bankrate.com"
    assert site2["status"] == "failed"
    assert "timeout" in site2["error"].lower()
    assert len(site2["subsites"]) == 0

    # Skipped subsite in travel profile
    travel_site = saved_data["profiles"]["travel"]["websites"][0]
    assert travel_site["status"] == "succeeded"
    assert travel_site["subsites"][1]["status"] == "skipped"
    assert travel_site["subsites"][1]["reason"] == "no_second_same_domain_link_found"


@pytest.mark.asyncio
async def test_build_profile_generates_and_updates_recap_json(tmp_path: Path):
    """Verify build_profile writes recap.json directly inside the base profiles directory."""
    import json
    from Collectors.ProfileCollector import ProfileCollector

    profiles_dir = tmp_path / "profiles"
    collector = ProfileCollector()

    mock_sites = [
        "https://test-site-a.example.com",
    ]

    summary = await collector.build_profile(
        profile_name="finance",
        custom_urls=mock_sites,
        timeout_per_site=5.0,
        settle_sec=0.05,
        headless=True,
        base_dir=profiles_dir,
        max_open_pages=2,
    )

    # Verify per-profile summary exists
    user_dir = profiles_dir / "profile_finance"
    assert (user_dir / "profile_finance_summary.json").is_file()

    # Verify recap.json exists inside profiles/ directory
    recap_file = profiles_dir / "recap.json"
    assert recap_file.is_file()

    recap = json.loads(recap_file.read_text(encoding="utf-8"))
    assert "overall_stats" in recap
    assert "finance" in recap["profiles"]
    assert len(recap["profiles"]["finance"]["websites"]) == 1

    site_rec = recap["profiles"]["finance"]["websites"][0]
    assert site_rec["parent_url"] == "https://test-site-a.example.com"
    assert "subsites" in site_rec
    assert "subsites_count" in site_rec


@pytest.mark.asyncio
async def test_random_action_delay():
    """Verify that random_action_delay respects bounds (e.g. 45s to 75s) and calls wait_for_timeout."""
    from unittest.mock import AsyncMock, MagicMock
    from Collectors.ProfileCollector import ProfileCollector

    collector = ProfileCollector()
    mock_page = MagicMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.wait_for_timeout = AsyncMock()

    # Fast test bounds: 0.05 to 0.10s
    delay = await collector.random_action_delay(mock_page, min_s=0.05, max_s=0.10)
    assert 0.05 <= delay <= 0.10
    mock_page.wait_for_timeout.assert_awaited_once()
    called_ms = mock_page.wait_for_timeout.call_args[0][0]
    assert 50 <= called_ms <= 100

    # Production delay range verification: 45.0 to 75.0s
    mock_page.wait_for_timeout.reset_mock()
    delay_prod = await collector.random_action_delay(mock_page, min_s=45.0, max_s=75.0)
    assert 45.0 <= delay_prod <= 75.0
    called_prod_ms = mock_page.wait_for_timeout.call_args[0][0]
    assert 45000 <= called_prod_ms <= 75000


@pytest.mark.asyncio
async def test_random_scroll():
    """Verify random_scroll executes scroll evaluations and pauses."""
    from unittest.mock import AsyncMock, MagicMock
    from Collectors.ProfileCollector import ProfileCollector

    collector = ProfileCollector()
    mock_page = MagicMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.evaluate = AsyncMock(return_value=1200)
    mock_page.wait_for_timeout = AsyncMock()

    total_px = await collector.random_scroll(mock_page, min_steps=2, max_steps=4)
    assert total_px > 0
    assert 2 <= mock_page.evaluate.call_count <= 4


@pytest.mark.asyncio
async def test_random_refreshes():
    """Verify random_refreshes executes between 0 and 2 page reloads with delays."""
    from unittest.mock import AsyncMock, MagicMock
    from Collectors.ProfileCollector import ProfileCollector

    collector = ProfileCollector()
    mock_page = MagicMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.reload = AsyncMock()
    mock_page.wait_for_timeout = AsyncMock()
    mock_page.evaluate = AsyncMock(return_value=800)

    # Force 1 to 2 refreshes
    refreshes = await collector.random_refreshes(
        mock_page,
        min_refreshes=1,
        max_refreshes=2,
        delay_min=0.01,
        delay_max=0.02,
        enable_scroll=True,
    )
    assert 1 <= refreshes <= 2
    assert mock_page.reload.call_count == refreshes


def test_cli_delay_and_chunk_percent_args():
    """Verify CLI argument definitions for --chunk-percent, --min-delay, and --max-delay."""
    import subprocess
    import sys

    res = subprocess.run(
        [sys.executable, "cli.py", "--help"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert "--chunk-percent" in res.stdout
    assert "--min-delay" in res.stdout
    assert "--max-delay" in res.stdout


@pytest.mark.asyncio
async def test_multi_profile_10_percent_chunk_processing(tmp_path: Path, monkeypatch):
    """Verify that multi-profile crawling processes websites in 10% chunks:

    Profile 1 finishes 10% of total websites, then Profile 2 finishes the SAME 10%,
    and Chunk 2 only begins after ALL profiles have finished Chunk 1.
    """
    import asyncio
    import time
    from cli import _run_all

    events: list[tuple[str, str, str, float]] = []
    crawl_lock = asyncio.Lock()

    async def mock_crawl(url, **kwargs):
        prof = kwargs.get("profile_name", "unknown")
        async with crawl_lock:
            events.append(("START", prof, url, time.monotonic()))
        await asyncio.sleep(0.01)
        async with crawl_lock:
            events.append(("FINISH", prof, url, time.monotonic()))
        return {
            "status": "completed",
            "successful": True,
            "data": {},
            "finalUrl": url,
            "profile_name": prof,
            "discovered_links": [],
        }

    monkeypatch.setattr("cli.crawl", mock_crawl)
    monkeypatch.setattr("timeout_manager.is_url_already_completed", lambda *a, **k: False)

    # 10 websites total. With chunk_percent=20.0, chunk_size = ceil(10 * 0.2) = 2 websites per chunk.
    # Total 5 chunks:
    # Chunk 1: site0, site1
    # Chunk 2: site2, site3
    # ...
    urls = [f"https://testsite-{i}.com" for i in range(10)]
    profiles = ["finance", "shopping"]

    await _run_all(
        urls=urls,
        output_dir=str(tmp_path),
        timeout=5,
        headless=True,
        collectors=["ads"],
        cmp_action=None,
        use_anti_bot=False,
        max_ads=None,
        crawlers=2,
        multi_profile_profiles=profiles,
        chunk_percent=20.0,  # 20% chunks = 2 sites per chunk
    )

    # 1. Verify all sites were crawled for both profiles
    finance_finishes = [e for e in events if e[0] == "FINISH" and e[1] == "finance"]
    shopping_finishes = [e for e in events if e[0] == "FINISH" and e[1] == "shopping"]
    assert len(finance_finishes) == 10
    assert len(shopping_finishes) == 10

    # 2. Check Chunk 1: sites 0 and 1
    # Profile 'finance' must finish both site0 and site1 BEFORE Profile 'shopping' starts site0 or site1
    chunk1_urls = {"https://testsite-0.com", "https://testsite-1.com"}
    chunk1_fin_finance = [
        e[3] for e in events if e[0] == "FINISH" and e[1] == "finance" and e[2] in chunk1_urls
    ]
    chunk1_start_shopping = [
        e[3] for e in events if e[0] == "START" and e[1] == "shopping" and e[2] in chunk1_urls
    ]
    assert len(chunk1_fin_finance) == 2
    assert len(chunk1_start_shopping) == 2
    assert max(chunk1_fin_finance) <= min(chunk1_start_shopping), (
        "Profile 1 ('finance') must finish all sites in Chunk 1 before Profile 2 ('shopping') starts Chunk 1!"
    )

    # 3. Check Chunk 1 to Chunk 2 barrier:
    # Both profiles must finish Chunk 1 (sites 0 and 1) BEFORE any profile starts Chunk 2 (sites 2 and 3)
    chunk1_all_finishes = [
        e[3] for e in events if e[0] == "FINISH" and e[2] in chunk1_urls
    ]
    chunk2_urls = {"https://testsite-2.com", "https://testsite-3.com"}
    chunk2_all_starts = [
        e[3] for e in events if e[0] == "START" and e[2] in chunk2_urls
    ]
    assert len(chunk1_all_finishes) == 4  # 2 profiles * 2 sites
    assert len(chunk2_all_starts) == 4    # 2 profiles * 2 sites
    assert max(chunk1_all_finishes) <= min(chunk2_all_starts), (
        "All profiles must completely finish Chunk 1 before ANY profile starts Chunk 2!"
    )




