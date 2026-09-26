"""Collectors/ProfileCollector.py
---------------------------------
Persona & browsing profile collector.

Creates a dedicated persistent Chromium user_data_dir for a profile named
'profile_<name>' based on the profiles in resources/profile_urls.py (e.g.
profile_finance, profile_shopping, profile_sports, profile_news, profile_random).

Visits the websites in the profile's list, discovers internal same-domain links,
and randomly selects and clicks on two same-domain links on each page to build
authentic browsing signals, history, tracking cookies, and local storage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urldefrag, urljoin, urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Helpers.hasher import get_registrable_domain
from Helpers.link_extractor import is_same_domain

if TYPE_CHECKING:
    from Helpers.crawl_context import CrawlContext

try:
    from resources.profile_urls import profile_directory
except ImportError:
    profile_directory = {}


# Extensions that should not be clicked as suburls (assets, documents, media)
_NON_NAVIGABLE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".pdf", ".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z", ".rar", ".exe",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".txt",
    ".css", ".js", ".mjs", ".json", ".xml", ".rss", ".atom", ".map",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp4", ".m4v", ".webm", ".ogv", ".avi", ".mov", ".wmv", ".flv", ".mp3", ".wav",
})


def get_profile_user_dir(profile_name: str, base_dir: Path | str | None = None) -> Path:
    """Return the path to the persistent user_dir for the profile (e.g. 'profile_finance')."""
    norm_name = profile_name.strip().lower()
    if norm_name.startswith("profile_"):
        norm_name = norm_name[len("profile_"):]
    dir_name = f"profile_{norm_name}"
    if base_dir is not None:
        return Path(base_dir) / dir_name
    return Path("profiles") / dir_name


def profile_exists(profile_name: str, base_dir: Path | str | None = None) -> bool:
    """Return True if the profile user_dir exists on disk and is a directory."""
    p_dir = get_profile_user_dir(profile_name, base_dir=base_dir)
    return p_dir.is_dir()


def get_available_profiles(base_dir: Path | str | None = None) -> list[str]:
    """Return a sorted list of profile names that currently have existing user_dir folders on disk."""
    root = Path(base_dir) if base_dir is not None else Path("profiles")
    if not root.is_dir():
        return []
    names: list[str] = []
    for d in root.iterdir():
        if d.is_dir() and d.name.startswith("profile_"):
            p_name = d.name[len("profile_"):]
            if p_name:
                names.append(p_name)
    return sorted(names)


def copy_profile_to_target(
    profile_name: str,
    target_dir: Path | str,
    base_dir: Path | str | None = None,
) -> Path:
    """Create an isolated copy of a master persona profile to target_dir.

    Skips ephemeral Chromium locks, sockets, and crashpad artifacts so the
    copied profile can be launched independently by Chromium without lock collisions.
    The master profile remains untouched and pristine.
    """
    src = get_profile_user_dir(profile_name, base_dir=base_dir)
    dst = Path(target_dir)

    if not src.is_dir():
        raise FileNotFoundError(f"Master profile directory not found: {src}")

    if dst.exists():
        for _ in range(3):
            try:
                shutil.rmtree(dst)
                break
            except Exception:
                time.sleep(0.1)

    # Exclude ephemeral browser disk caches to make profile copying instantaneous (~15MB vs ~350MB)
    # while preserving 100% of persona identity (cookies, localStorage, indexedDB, history, preferences).
    ephemeral_items = {
        "Cache",
        "Code Cache",
        "DawnWebGPUCache",
        "DawnCache",
        "GPUCache",
        "ShaderCache",
        "GrShaderCache",
        "Media Cache",
        "blob_storage",
        "CacheStorage",
        "ScriptCache",
        "Crashpad",
    }

    def _ignore_patterns(path: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        for n in names:
            if (
                n in ephemeral_items
                or n.startswith("LOCK")
                or n.startswith("Singleton")
                or "Crashpad" in n
            ):
                ignored.add(n)
        return ignored

    shutil.copytree(src, dst, ignore=_ignore_patterns)
    return dst



def _normalize_link_url(raw_href: str, base_url: str, root_url: str) -> str | None:
    """Validate and normalize a link URL to a clean same-domain http/https URL."""
    if not raw_href or not isinstance(raw_href, str):
        return None

    raw_href = raw_href.strip()
    if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:", "data:", "blob:", "about:")):
        return None

    try:
        resolved = urljoin(base_url, raw_href)
    except Exception:
        return None

    clean_url, _ = urldefrag(resolved)
    clean_url = clean_url.strip()
    if not clean_url:
        return None

    try:
        parsed = urlparse(clean_url)
    except Exception:
        return None

    if parsed.scheme.lower() not in ("http", "https"):
        return None

    if not parsed.netloc:
        return None

    # Verify not static asset
    path_lower = parsed.path.lower()
    if any(path_lower.endswith(ext) for ext in _NON_NAVIGABLE_EXTENSIONS):
        return None

    # Verify same registrable domain
    if not is_same_domain(clean_url, root_url):
        return None

    # Ignore login/logout/signup links that could disrupt session
    for token in ("/logout", "/signout", "/auth/logout", "action=logout"):
        if token in clean_url.lower():
            return None

    norm_path = parsed.path.rstrip("/") if parsed.path != "/" else "/"
    normalized = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{norm_path}"
    if parsed.query:
        normalized = f"{normalized}?{parsed.query}"

    return normalized


class ProfileCollector:
    """Collects profiles by maintaining persistent browser user directories and

    visiting profile URLs with random same-domain sublink interactions.
    """

    COLLECTOR_NAME = "ProfileCollector"
    PROFILES_DIR_NAME = "profiles"

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("ProfileCollector")
        self._output_dir: Path = Path("profiles")
        self._url_hash: str = ""
        self._crawl_context: CrawlContext | None = None
        self._profile_name: str | None = None
        self._base_profiles_dir: Path = Path("profiles")
        self._collected_records: list[dict[str, Any]] = []

    def init(
        self,
        output_dir: str,
        logger: logging.Logger | None,
        url_hash: str,
        crawl_context: CrawlContext | None = None,
        profile_name: str | None = None,
        base_profiles_dir: str | Path | None = None,
    ) -> None:
        """Initialize collector for crawl session."""
        self._output_dir = Path(output_dir)
        self._logger = logger or logging.getLogger("ProfileCollector")
        self._url_hash = url_hash
        self._crawl_context = crawl_context
        self._profile_name = profile_name
        self._base_profiles_dir = Path(base_profiles_dir) if base_profiles_dir else (self._output_dir / self.PROFILES_DIR_NAME)
        self._collected_records = []

    async def pre_crawl(self, page: Page) -> None:
        """Prepare collector before page crawl."""
        pass

    async def extract_same_domain_candidates(self, page: Page, root_url: str) -> list[dict[str, str]]:
        """Extract unique same-domain candidate anchor elements from the current page."""
        if page is None or getattr(page, "is_closed", lambda: True)():
            return []

        try:
            curr_url = getattr(page, "url", root_url)
            raw_anchors = await page.evaluate(r"""() => {
                const results = [];
                const anchors = Array.from(document.querySelectorAll('a[href]'));
                for (const a of anchors) {
                    const href = a.getAttribute('href') || a.href;
                    if (!href) continue;
                    const text = (a.innerText || a.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 100);
                    const rect = a.getBoundingClientRect();
                    const visible = rect.width > 0 && rect.height > 0;
                    results.push({
                        href: href,
                        text: text,
                        visible: visible
                    });
                }
                return results;
            }""")
        except Exception as exc:
            self._logger.debug(f"[ProfileCollector] Error extracting page anchors: {exc}")
            return []

        candidates: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        clean_curr, _ = urldefrag(curr_url.strip())
        seen_urls.add(clean_curr.lower().rstrip("/"))

        for item in raw_anchors:
            href = item.get("href", "")
            norm_url = _normalize_link_url(href, curr_url, root_url)
            if not norm_url:
                continue

            clean_norm, _ = urldefrag(norm_url)
            norm_key = clean_norm.lower().rstrip("/")
            if norm_key in seen_urls:
                continue

            seen_urls.add(norm_key)
            candidates.append({
                "url": norm_url,
                "href": href,
                "text": item.get("text", ""),
                "visible": item.get("visible", False),
            })

        return candidates

    async def click_random_suburl(
        self,
        page: Page,
        candidates: list[dict[str, str]],
        timeout_ms: int = 5000,
        settle_ms: int = 1000,
    ) -> dict[str, Any]:
        """Randomly select one same-domain candidate link and click it (with fallback to direct navigation)."""
        if not candidates:
            return {
                "clicked": False,
                "status": "skipped",
                "method": None,
                "reason": "no_candidate_links",
                "target_url": None,
                "final_url": None,
                "anchor_text": "",
                "duration_ms": 0,
                "error": None,
            }

        # Prioritize visible links if available, otherwise any candidate
        visible_candidates = [c for c in candidates if c.get("visible")]
        pool = visible_candidates if visible_candidates else candidates
        chosen = random.choice(pool)
        target_url = chosen["url"]
        href_raw = chosen.get("href", target_url)

        start_time = time.monotonic()
        navigated = False
        click_method = "click"
        error_msg: str | None = None

        try:
            # 1. Attempt to locate anchor element
            escaped_href = href_raw.replace('"', '\\"')
            locator = page.locator(f'a[href="{escaped_href}"]').first
            has_locator = False
            try:
                has_locator = await locator.count() > 0
            except Exception:
                has_locator = False

            if has_locator:
                try:
                    await locator.scroll_into_view_if_needed(timeout=1000)
                    target_attr = ""
                    try:
                        target_attr = (await locator.get_attribute("target")) or ""
                    except Exception:
                        pass

                    if target_attr.strip().lower() == "_blank":
                        await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
                        navigated = True
                        click_method = "direct_navigation"
                    else:
                        await locator.click(timeout=2000)
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                        except Exception:
                            pass
                        navigated = True
                        click_method = "click"
                except Exception as loc_exc:
                    navigated = False
                    error_msg = str(loc_exc)

            if not navigated:
                # 2. Fallback to direct page navigation to the selected suburl
                click_method = "direct_navigation"
                await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
                navigated = True
                error_msg = None

        except Exception as exc:
            self._logger.debug(f"[ProfileCollector] Navigation failed for {target_url}: {exc}")
            error_msg = str(exc)
            navigated = False

        # Settle wait for scripts, dynamic ads, and cookies
        try:
            if not page.is_closed():
                await page.wait_for_timeout(settle_ms)
        except Exception:
            pass

        duration_ms = int((time.monotonic() - start_time) * 1000)
        final_url = getattr(page, "url", target_url)

        return {
            "clicked": navigated,
            "status": "succeeded" if navigated else "failed",
            "method": click_method,
            "target_url": target_url,
            "final_url": final_url,
            "anchor_text": chosen.get("text", ""),
            "duration_ms": duration_ms,
            "error": error_msg if not navigated else None,
            "reason": None if navigated else ("navigation_error" if error_msg else "click_failed"),
        }

    async def random_action_delay(
        self,
        page: Page | None = None,
        min_s: float = 45.0,
        max_s: float = 75.0,
    ) -> float:
        """Wait for a randomized duration between min_s and max_s (default: 45s to 1m15s) between actions."""
        if max_s <= 0 or min_s < 0:
            return 0.0
        actual_min = min(min_s, max_s)
        actual_max = max(min_s, max_s)
        delay = round(random.uniform(actual_min, actual_max), 2)
        self._logger.info(
            f"[ProfileCollector] Human action pause: sleeping {delay}s "
            f"(randomized from {actual_min}s to {actual_max}s)..."
        )
        if page is not None and not getattr(page, "is_closed", lambda: True)():
            try:
                await page.wait_for_timeout(int(delay * 1000))
                return delay
            except Exception:
                pass
        await asyncio.sleep(delay)
        return delay

    async def random_scroll(
        self,
        page: Page,
        min_steps: int = 2,
        max_steps: int = 5,
        min_px: int = 200,
        max_px: int = 500,
        step_delay_range: tuple[float, float] = (0.3, 0.8),
    ) -> int:
        """Perform randomized human-like scrolling on the active page."""
        if page is None or getattr(page, "is_closed", lambda: True)():
            return 0

        steps = random.randint(min_steps, max_steps)
        total_scrolled = 0
        self._logger.debug(f"[ProfileCollector] Performing randomized scroll ({steps} steps)...")

        for step_idx in range(steps):
            if getattr(page, "is_closed", lambda: True)():
                break
            # Mostly scroll down; occasionally scroll back up slightly (~25% chance after first step)
            if step_idx > 0 and random.random() < 0.25:
                delta = -random.randint(min_px // 2, max_px // 2)
            else:
                delta = random.randint(min_px, max_px)

            try:
                await page.evaluate(f"window.scrollBy({{top: {delta}, left: 0, behavior: 'smooth'}})")
                total_scrolled += delta
            except Exception:
                try:
                    await page.mouse.wheel(0, delta)
                    total_scrolled += delta
                except Exception:
                    break

            step_delay = round(random.uniform(step_delay_range[0], step_delay_range[1]), 2)
            try:
                await page.wait_for_timeout(int(step_delay * 1000))
            except Exception:
                await asyncio.sleep(step_delay)

        return total_scrolled

    async def random_refreshes(
        self,
        page: Page,
        min_refreshes: int = 0,
        max_refreshes: int = 2,
        timeout_ms: int = 15000,
        delay_min: float = 45.0,
        delay_max: float = 75.0,
        enable_scroll: bool = True,
    ) -> int:
        """Perform 0 to 2 randomized refreshes on the page with randomized delay between actions."""
        if page is None or getattr(page, "is_closed", lambda: True)():
            return 0

        count = random.randint(min_refreshes, max_refreshes)
        if count == 0:
            self._logger.debug("[ProfileCollector] 0 refreshes chosen for this page.")
            return 0

        refreshes_done = 0
        for r_idx in range(1, count + 1):
            if getattr(page, "is_closed", lambda: True)():
                break
            # Delay between actions (45s to 1m15s)
            await self.random_action_delay(page, min_s=delay_min, max_s=delay_max)
            if getattr(page, "is_closed", lambda: True)():
                break

            self._logger.info(f"[ProfileCollector] Performing page refresh {r_idx}/{count}...")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                refreshes_done += 1
                if enable_scroll:
                    await self.random_scroll(page, min_steps=1, max_steps=3)
            except Exception as ref_exc:
                self._logger.debug(f"[ProfileCollector] Page reload error: {ref_exc}")
                break

        return refreshes_done

    async def interact_on_page(
        self,
        page: Page,
        seed_url: str,
        timeout_sec: float = 15.0,
        settle_sec: float = 1.0,
        action_delay_min: float = 0.0,
        action_delay_max: float = 0.0,
        enable_scroll: bool = False,
        min_refreshes: int = 0,
        max_refreshes: int = 0,
    ) -> dict[str, Any]:
        """Perform the 2 random same-domain link clicks with randomized delays, scrolling, and refreshes."""
        settle_ms = int(settle_sec * 1000)
        nav_timeout_ms = min(8000, max(3000, int((timeout_sec / 2) * 1000)))

        clicks: list[dict[str, Any]] = []

        # --- Click 1 ---
        candidates_1 = await self.extract_same_domain_candidates(page, seed_url)
        self._logger.debug(f"[ProfileCollector] Found {len(candidates_1)} same-domain link(s) for click 1")

        if candidates_1:
            click_1 = await self.click_random_suburl(
                page,
                candidates_1,
                timeout_ms=nav_timeout_ms,
                settle_ms=settle_ms,
            )
            click_1["subsite_index"] = 1
            clicks.append(click_1)
            self._logger.info(
                f"[ProfileCollector] Click 1/2 -> {click_1.get('target_url')} "
                f"({click_1.get('method')}, {click_1.get('duration_ms')}ms, status={click_1.get('status')})"
            )

            # On Click 1 subpage: randomized scroll and optional refreshes
            if click_1.get("clicked"):
                if enable_scroll:
                    await self.random_scroll(page)
                if max_refreshes > 0:
                    click_1["refreshes_completed"] = await self.random_refreshes(
                        page,
                        min_refreshes=min_refreshes,
                        max_refreshes=max_refreshes,
                        timeout_ms=nav_timeout_ms,
                        delay_min=action_delay_min,
                        delay_max=action_delay_max,
                        enable_scroll=enable_scroll,
                    )
        else:
            clicks.append({
                "subsite_index": 1,
                "clicked": False,
                "status": "skipped",
                "method": None,
                "reason": "no_same_domain_links_found",
                "target_url": None,
                "final_url": None,
                "anchor_text": "",
                "duration_ms": 0,
                "error": None,
            })

        # Action delay between Click 1 and Click 2 (45s to 1m15s in profile building)
        if action_delay_max > 0:
            await self.random_action_delay(page, min_s=action_delay_min, max_s=action_delay_max)

        # --- Click 2 ---
        candidates_2 = await self.extract_same_domain_candidates(page, seed_url)
        # Filter out the URL we just visited in Click 1
        if clicks and clicks[0].get("target_url"):
            visited_url = clicks[0]["target_url"].lower().rstrip("/")
            candidates_2 = [c for c in candidates_2 if c["url"].lower().rstrip("/") != visited_url]

        self._logger.debug(f"[ProfileCollector] Found {len(candidates_2)} same-domain link(s) for click 2")

        if candidates_2:
            click_2 = await self.click_random_suburl(
                page,
                candidates_2,
                timeout_ms=nav_timeout_ms,
                settle_ms=settle_ms,
            )
            click_2["subsite_index"] = 2
            clicks.append(click_2)
            self._logger.info(
                f"[ProfileCollector] Click 2/2 -> {click_2.get('target_url')} "
                f"({click_2.get('method')}, {click_2.get('duration_ms')}ms, status={click_2.get('status')})"
            )

            # On Click 2 subpage: randomized scroll and optional refreshes
            if click_2.get("clicked"):
                if enable_scroll:
                    await self.random_scroll(page)
                if max_refreshes > 0:
                    click_2["refreshes_completed"] = await self.random_refreshes(
                        page,
                        min_refreshes=min_refreshes,
                        max_refreshes=max_refreshes,
                        timeout_ms=nav_timeout_ms,
                        delay_min=action_delay_min,
                        delay_max=action_delay_max,
                        enable_scroll=enable_scroll,
                    )
        else:
            clicks.append({
                "subsite_index": 2,
                "clicked": False,
                "status": "skipped",
                "method": None,
                "reason": "no_second_same_domain_link_found",
                "target_url": None,
                "final_url": None,
                "anchor_text": "",
                "duration_ms": 0,
                "error": None,
            })

        return {
            "seed_url": seed_url,
            "final_url": getattr(page, "url", seed_url),
            "clicks_completed": sum(1 for c in clicks if c.get("clicked")),
            "clicks": clicks,
        }

    async def collect(self, page: Page) -> dict[str, Any]:
        """Standard collector collect() hook called by crawler.py on an active page."""
        curr_url = getattr(page, "url", "")
        if not curr_url:
            return {"status": "no_page_url", "clicks": []}

        interaction = await self.interact_on_page(page, curr_url)

        # Snapshot cookies accumulated in context
        cookies_count = 0
        try:
            cookies = await page.context.cookies()
            cookies_count = len(cookies)
        except Exception:
            pass

        result = {
            "collector": self.COLLECTOR_NAME,
            "profile_name": self._profile_name,
            "site_url": curr_url,
            "interaction": interaction,
            "cookies_count": cookies_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._collected_records.append(result)
        return result

    def get_partial_results(self) -> list[dict[str, Any]]:
        """Return partial collected interaction records."""
        return list(self._collected_records)

    async def build_profile(
        self,
        profile_name: str,
        max_sites: int | None = None,
        timeout_per_site: float = 15.0,
        settle_sec: float = 1.0,
        headless: bool = True,
        base_dir: Path | str | None = None,
        custom_urls: list[str] | None = None,
        max_open_pages: int = 5,
        parallel_crawls: int = 1,
        total_open_pages_limit: int | None = None,
        executable_path: str | None = None,
        action_delay_min: float | None = None,
        action_delay_max: float | None = None,
        enable_random_scroll: bool = True,
        min_refreshes: int = 0,
        max_refreshes: int = 2,
    ) -> dict[str, Any]:
        """Build or update a persona profile by creating/using user_dir 'profile_<name>'.

        Iterates over the websites in the profile, spawns a new tab for each website,
        and accumulates open tabs in the browser up to max_open_pages (default: 5 open
        websites simultaneously). When the limit is reached, evicts and closes the
        oldest open tab in FIFO order. Never loads Consent-O-Matic, and keeps all
        cookies, cache, and state strictly within the persistent profile directory.

        Simulates natural human browsing with:
        - Randomized delay (45s to 1m15s) between each action (page load, refresh, clicks).
        - Randomized human-like scrolling.
        - Randomized page reloads (0 to 2 refreshes per page).
        """
        norm_name = profile_name.strip().lower()
        if norm_name.startswith("profile_"):
            norm_name = norm_name[len("profile_"):]
        user_dir = get_profile_user_dir(norm_name, base_dir)
        user_dir.mkdir(parents=True, exist_ok=True)

        if max_open_pages is None:
            max_open_pages = 5

        # Determine effective action delays:
        # Default to 45s-75s for production (settle_sec >= 0.5), or small delays for fast unit tests
        if action_delay_min is not None:
            eff_delay_min = max(0.0, float(action_delay_min))
        else:
            eff_delay_min = 45.0 if settle_sec >= 0.5 else 0.01

        if action_delay_max is not None:
            eff_delay_max = max(eff_delay_min, float(action_delay_max))
        else:
            eff_delay_max = 75.0 if settle_sec >= 0.5 else 0.05

        urls = custom_urls or profile_directory.get(norm_name, [])
        if not urls:
            self._logger.warning(
                f"[ProfileCollector] No URLs defined for profile '{norm_name}'. "
                f"Available in profile_directory: {list(profile_directory.keys())}"
            )
            return {
                "profile_name": norm_name,
                "user_dir": str(user_dir),
                "sites_visited": 0,
                "records": [],
                "error": f"No URLs found for profile '{norm_name}'",
            }

        if max_sites is not None and max_sites > 0:
            urls = urls[:max_sites]

        self._logger.info(
            f"[ProfileCollector] Starting profile build: '{norm_name}' -> "
            f"user_dir='{user_dir}' with {len(urls)} site(s) "
            f"(max open websites: {max_open_pages}, timeout: {timeout_per_site}s, "
            f"action delays: {eff_delay_min}s-{eff_delay_max}s, refreshes: {min_refreshes}-{max_refreshes}/page)"
        )

        records: list[dict[str, Any]] = []
        total_cookies = 0
        total_clicks = 0
        open_pages: list[Page] = []
        profile_start_mono = time.monotonic()
        profile_start_time = datetime.now(timezone.utc).isoformat()

        async with async_playwright() as pw:
            # Note: Consent-O-Matic is intentionally NOT loaded during profile construction
            # so that natural CMP dialogs and authentic tracking cookies accumulate untampered.
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
            launch_kwargs: dict[str, Any] = {
                "headless": headless,
                "args": launch_args,
                "viewport": {"width": 1900, "height": 1000},
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
            }
            if executable_path:
                launch_kwargs["executable_path"] = executable_path

            context: BrowserContext = await pw.chromium.launch_persistent_context(
                str(user_dir),
                **launch_kwargs,
            )

            try:
                initial_page = context.pages[0] if (context.pages and not context.pages[0].is_closed()) else None

                for idx, url in enumerate(urls, start=1):
                    site_start = time.monotonic()
                    target_nav_url = url.strip()
                    if not target_nav_url.startswith(("http://", "https://")):
                        target_nav_url = f"https://{target_nav_url}"

                    self._logger.info(f"[ProfileCollector] [{norm_name} {idx}/{len(urls)}] Navigating to: {target_nav_url}")
                    site_record: dict[str, Any] = {
                        "index": idx,
                        "parent_url": url,
                        "seed_url": url,
                        "resolved_url": target_nav_url,
                        "final_url": None,
                        "initial_final_url": None,
                        "status": "pending",
                        "error": None,
                        "duration_sec": 0.0,
                        "refreshes_completed": 0,
                        "subsites_count": 0,
                        "subsites_succeeded": 0,
                        "subsites_failed": 0,
                        "subsites_skipped": 0,
                        "clicks": [],
                        "subsites": [],
                        "profile_cookies_count": 0,
                    }

                    # Clean any externally closed pages
                    open_pages = [p for p in open_pages if not p.is_closed()]

                    # FIFO Eviction: close oldest page if at or over limit
                    while len(open_pages) >= max_open_pages:
                        oldest_page = open_pages.pop(0)
                        try:
                            if not oldest_page.is_closed():
                                await oldest_page.close()
                                self._logger.info(
                                    f"[ProfileCollector] [{norm_name}] Closed oldest website tab (FIFO) -> "
                                    f"maintaining {max_open_pages} open websites in browser"
                                )
                        except Exception as close_exc:
                            self._logger.debug(f"[ProfileCollector] Error closing oldest page: {close_exc}")

                    # Spawn a new tab for each site (reuse initial blank tab for the first site)
                    try:
                        if idx == 1 and initial_page and not initial_page.is_closed():
                            page = initial_page
                        else:
                            page = await context.new_page()
                    except Exception as new_page_exc:
                        self._logger.warning(
                            f"[ProfileCollector] [{norm_name}] Could not create new page (context closed): {new_page_exc}"
                        )
                        break

                    try:
                        # Fast domcontentloaded navigation
                        timeout_ms = int(timeout_per_site * 1000)
                        try:
                            await page.goto(target_nav_url, wait_until="domcontentloaded", timeout=timeout_ms)
                        except Exception as goto_exc:
                            curr_url = getattr(page, "url", "")
                            if curr_url and curr_url != "about:blank":
                                self._logger.info(
                                    f"[ProfileCollector] [{norm_name} {idx}/{len(urls)}] "
                                    f"DOMContentLoaded timeout on {target_nav_url}, but page reached '{curr_url}'. Continuing."
                                )
                            else:
                                raise goto_exc

                        # Snappy settle wait
                        settle_ms = int(settle_sec * 1000)
                        if settle_ms > 0:
                            await page.wait_for_timeout(settle_ms)
                        site_record["final_url"] = page.url
                        site_record["initial_final_url"] = page.url

                        # Action 1: Randomized scroll on landed page
                        if enable_random_scroll:
                            await self.random_scroll(page)

                        # Action 2: Randomized refreshes (0 to 2 refreshes per page)
                        refreshes_done = await self.random_refreshes(
                            page,
                            min_refreshes=min_refreshes,
                            max_refreshes=max_refreshes,
                            timeout_ms=timeout_ms,
                            delay_min=eff_delay_min,
                            delay_max=eff_delay_max,
                            enable_scroll=enable_random_scroll,
                        )
                        site_record["refreshes_completed"] = refreshes_done

                        # Action 3: Delay (45s to 1m15s) before sublink interactions
                        if eff_delay_max > 0:
                            await self.random_action_delay(page, min_s=eff_delay_min, max_s=eff_delay_max)

                        # Action 4 & 5: Click two same-domain links randomly with action delays and scrolling
                        interaction = await self.interact_on_page(
                            page,
                            target_nav_url,
                            timeout_sec=timeout_per_site,
                            settle_sec=settle_sec,
                            action_delay_min=eff_delay_min,
                            action_delay_max=eff_delay_max,
                            enable_scroll=enable_random_scroll,
                            min_refreshes=min_refreshes,
                            max_refreshes=max_refreshes,
                        )

                        site_subsites = interaction.get("clicks", [])
                        site_record["status"] = "succeeded"
                        site_record["clicks"] = site_subsites
                        site_record["subsites"] = site_subsites
                        site_record["subsites_count"] = len(site_subsites)
                        site_record["subsites_succeeded"] = sum(
                            1 for s in site_subsites if s.get("status") == "succeeded" or s.get("clicked") is True
                        )
                        site_record["subsites_failed"] = sum(
                            1 for s in site_subsites if s.get("status") == "failed"
                        )
                        site_record["subsites_skipped"] = sum(
                            1 for s in site_subsites if s.get("status") == "skipped"
                        )
                        total_clicks += interaction.get("clicks_completed", 0)

                    except Exception as site_exc:
                        site_record["status"] = "failed"
                        site_record["error"] = str(site_exc)
                        site_record["final_url"] = getattr(page, "url", None)
                        self._logger.warning(
                            f"[ProfileCollector] [{norm_name} {idx}/{len(urls)}] "
                            f"Error on {url}: {site_exc}"
                        )
                    finally:
                        # Only retain successfully completed websites in open_pages (max 5 open at once)
                        if site_record.get("status") in ("succeeded", "completed"):
                            if not page.is_closed() and page not in open_pages:
                                open_pages.append(page)
                        else:
                            # If navigation failed or timed out, close this tab immediately to free resources
                            try:
                                if not page.is_closed():
                                    await page.close()
                            except Exception:
                                pass

                    site_record["duration_sec"] = round(time.monotonic() - site_start, 2)

                    # Snapshot cookies in this persistent profile
                    try:
                        site_cookies = await context.cookies()
                        site_record["profile_cookies_count"] = len(site_cookies)
                        total_cookies = len(site_cookies)
                    except Exception:
                        site_record["profile_cookies_count"] = total_cookies

                    records.append(site_record)

                    # Action: Delay 45s-1m15s before advancing to next website
                    if idx < len(urls) and eff_delay_max > 0:
                        await self.random_action_delay(
                            page if (page and not getattr(page, "is_closed", lambda: True)()) else None,
                            min_s=eff_delay_min,
                            max_s=eff_delay_max,
                        )

            finally:
                try:
                    for p in open_pages:
                        try:
                            if not p.is_closed():
                                await p.close()
                        except Exception:
                            pass
                    open_pages.clear()
                    await context.close()
                except Exception as exc:
                    self._logger.debug(f"[ProfileCollector] Context close error: {exc}")

        successful_sites = sum(1 for r in records if r.get("status") in ("completed", "succeeded"))
        failed_sites = len(records) - successful_sites
        total_subsites = sum(r.get("subsites_count", 0) for r in records)
        successful_subsites = sum(r.get("subsites_succeeded", 0) for r in records)
        failed_subsites = sum(r.get("subsites_failed", 0) for r in records)
        skipped_subsites = sum(r.get("subsites_skipped", 0) for r in records)
        total_refreshes = sum(r.get("refreshes_completed", 0) for r in records)

        summary = {
            "profile_name": norm_name,
            "user_dir": str(user_dir.resolve()),
            "status": "completed" if successful_sites > 0 else "failed",
            "sites_configured": len(urls),
            "sites_visited": len(records),
            "successful_sites": successful_sites,
            "failed_sites": failed_sites,
            "total_clicks": total_clicks,
            "total_refreshes": total_refreshes,
            "action_delay_range_sec": [eff_delay_min, eff_delay_max],
            "random_scroll_enabled": enable_random_scroll,
            "refreshes_range": [min_refreshes, max_refreshes],
            "total_subsites_attempted": total_subsites,
            "successful_subsites": successful_subsites,
            "failed_subsites": failed_subsites,
            "skipped_subsites": skipped_subsites,
            "parent_success_rate_percent": round((successful_sites / len(records) * 100), 2) if records else 0.0,
            "subsite_success_rate_percent": round((successful_subsites / total_subsites * 100), 2) if total_subsites else 0.0,
            "accumulated_cookies_count": total_cookies,
            "max_open_pages_limit": max_open_pages,
            "duration_sec": round(time.monotonic() - profile_start_mono, 2),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "records": records,
            "websites": records,
        }

        # Save summary directly into the profile directory
        summary_path = user_dir / f"profile_{norm_name}_summary.json"
        try:
            summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            self._logger.info(f"[ProfileCollector] Saved profile summary -> {summary_path}")
        except Exception as save_exc:
            self._logger.warning(f"[ProfileCollector] Could not write summary file: {save_exc}")

        # Update recap.json in base profiles folder
        try:
            generate_profiles_recap(
                {norm_name: summary},
                base_dir=base_dir,
                operation_name=f"build_profile_{norm_name}",
                start_time=profile_start_time,
                duration_sec=summary["duration_sec"],
            )
        except Exception as recap_exc:
            self._logger.debug(f"[ProfileCollector] Error updating recap.json: {recap_exc}")

        return summary

    async def build_all_profiles(
        self,
        max_sites_per_profile: int | None = None,
        timeout_per_site: float = 15.0,
        settle_sec: float = 1.0,
        headless: bool = True,
        base_dir: Path | str | None = None,
        parallel_crawls: int = 1,
        max_open_pages_per_browser: int = 5,
        total_open_pages_limit: int | None = None,
        executable_path: str | None = None,
        action_delay_min: float | None = None,
        action_delay_max: float | None = None,
        enable_random_scroll: bool = True,
        min_refreshes: int = 0,
        max_refreshes: int = 2,
    ) -> dict[str, Any]:
        """Build all profiles defined in resources/profile_urls.py.

        Each browser maintains up to max_open_pages_per_browser (default: 5) open websites
        simultaneously, closing the oldest in FIFO order when exceeded.
        Simulates natural human pacing with 45s-1m15s action pauses, random scrolls, and 0-2 refreshes.
        Generates and saves recap.json inside the profiles directory.
        """
        all_start_mono = time.monotonic()
        all_start_time = datetime.now(timezone.utc).isoformat()
        profiles = list(profile_directory.keys())
        num_parallel = max(1, min(parallel_crawls, len(profiles)))

        self._logger.info(
            f"[ProfileCollector] Building {len(profiles)} profile(s) ({', '.join(profiles)}) "
            f"with {num_parallel} parallel browser crawl(s) (max {max_open_pages_per_browser} open websites at the same time)."
        )

        semaphore = asyncio.Semaphore(num_parallel)

        async def _run_profile(p_name: str) -> tuple[str, dict[str, Any]]:
            async with semaphore:
                try:
                    res = await self.build_profile(
                        profile_name=p_name,
                        max_sites=max_sites_per_profile,
                        timeout_per_site=timeout_per_site,
                        settle_sec=settle_sec,
                        headless=headless,
                        base_dir=base_dir,
                        max_open_pages=max_open_pages_per_browser,
                        parallel_crawls=num_parallel,
                        executable_path=executable_path,
                        action_delay_min=action_delay_min,
                        action_delay_max=action_delay_max,
                        enable_random_scroll=enable_random_scroll,
                        min_refreshes=min_refreshes,
                        max_refreshes=max_refreshes,
                    )
                    return p_name, res
                except Exception as p_exc:
                    self._logger.error(f"[ProfileCollector] Profile '{p_name}' build error: {p_exc}")
                    return p_name, {
                        "profile_name": p_name,
                        "status": "failed",
                        "error": str(p_exc),
                        "records": [],
                        "websites": [],
                        "sites_configured": 0,
                        "sites_visited": 0,
                        "successful_sites": 0,
                        "failed_sites": 0,
                        "accumulated_cookies_count": 0,
                    }

        tasks = [_run_profile(p) for p in profiles]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        all_results: dict[str, dict[str, Any]] = {}
        for item in results_list:
            if isinstance(item, tuple) and len(item) == 2:
                all_results[item[0]] = item[1]

        total_duration = round(time.monotonic() - all_start_mono, 2)
        recap, recap_file = generate_profiles_recap(
            all_results,
            base_dir=base_dir,
            operation_name="build_all_profiles",
            start_time=all_start_time,
            duration_sec=total_duration,
            config={
                "parallel_crawls": num_parallel,
                "max_open_pages_per_browser": max_open_pages_per_browser,
                "timeout_per_site": timeout_per_site,
                "settle_sec": settle_sec,
                "headless": headless,
                "action_delay_min": action_delay_min,
                "action_delay_max": action_delay_max,
                "enable_random_scroll": enable_random_scroll,
                "refreshes_range": [min_refreshes, max_refreshes],
            },
        )
        print_recap_summary(recap, recap_file)
        return all_results


# =====================================================================
# Profile Operation Recap Reporting (recap.json)
# =====================================================================

def generate_profiles_recap(
    profiles_results: dict[str, dict[str, Any]],
    base_dir: Path | str | None = None,
    operation_name: str = "build_all_profiles",
    start_time: str | None = None,
    duration_sec: float | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Generate or update the comprehensive recap.json file inside the profiles directory."""
    root = Path(base_dir) if base_dir is not None else Path("profiles")
    root.mkdir(parents=True, exist_ok=True)
    recap_file = root / "recap.json"

    # Merge with existing recap if present
    merged_profiles: dict[str, dict[str, Any]] = {}
    if recap_file.is_file():
        try:
            old_recap = json.loads(recap_file.read_text(encoding="utf-8"))
            if isinstance(old_recap.get("profiles"), dict):
                merged_profiles.update(old_recap["profiles"])
        except Exception:
            pass

    for p_name, p_data in profiles_results.items():
        if isinstance(p_data, dict):
            merged_profiles[p_name] = p_data

    # Calculate overall stats
    total_profiles = len(merged_profiles)
    successful_profiles = sum(
        1 for p in merged_profiles.values() if p.get("status") in ("completed", "succeeded")
    )
    failed_profiles = total_profiles - successful_profiles

    total_parent_websites = 0
    visited_parent_websites = 0
    successful_parent_websites = 0
    failed_parent_websites = 0

    total_subsites_attempted = 0
    successful_subsites = 0
    failed_subsites = 0
    skipped_subsites = 0
    total_cookies = 0

    profiles_overview: list[dict[str, Any]] = []

    for p_name, p_data in sorted(merged_profiles.items()):
        p_sites_conf = p_data.get("sites_configured", 0)
        p_sites_vis = p_data.get("sites_visited", 0)
        p_succ_sites = p_data.get("successful_sites", 0)
        p_failed_sites = p_data.get("failed_sites", max(0, p_sites_vis - p_succ_sites))
        p_cookies = p_data.get("accumulated_cookies_count", 0)

        websites = p_data.get("websites") or p_data.get("records") or []
        p_total_subs = 0
        p_succ_subs = 0
        p_fail_subs = 0
        p_skip_subs = 0

        for w in websites:
            subs = w.get("subsites") or w.get("clicks") or []
            for s in subs:
                p_total_subs += 1
                if s.get("status") == "succeeded" or s.get("clicked") is True:
                    p_succ_subs += 1
                elif s.get("status") == "skipped":
                    p_skip_subs += 1
                else:
                    p_fail_subs += 1

        total_parent_websites += (p_sites_conf or len(websites))
        visited_parent_websites += len(websites)
        successful_parent_websites += p_succ_sites
        failed_parent_websites += p_failed_sites

        total_subsites_attempted += p_total_subs
        successful_subsites += p_succ_subs
        failed_subsites += p_fail_subs
        skipped_subsites += p_skip_subs
        total_cookies += p_cookies

        profiles_overview.append({
            "profile_name": p_name,
            "status": p_data.get("status", "completed"),
            "parent_websites_visited": f"{p_succ_sites}/{len(websites)}",
            "subsites_visited": f"{p_succ_subs}/{p_total_subs}",
            "cookies": p_cookies,
            "duration_sec": p_data.get("duration_sec", 0.0),
        })

    parent_success_rate = (
        round((successful_parent_websites / visited_parent_websites) * 100, 2)
        if visited_parent_websites > 0
        else 0.0
    )
    subsite_success_rate = (
        round((successful_subsites / total_subsites_attempted) * 100, 2)
        if total_subsites_attempted > 0
        else 0.0
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    recap = {
        "operation": operation_name,
        "status": "completed" if failed_profiles == 0 else "partial",
        "generated_at": now_iso,
        "start_time": start_time or now_iso,
        "end_time": now_iso,
        "duration_sec": duration_sec if duration_sec is not None else 0.0,
        "config": config or {},
        "overall_stats": {
            "total_profiles": total_profiles,
            "successful_profiles": successful_profiles,
            "failed_profiles": failed_profiles,
            "total_parent_websites_configured": total_parent_websites,
            "visited_parent_websites": visited_parent_websites,
            "successful_parent_websites": successful_parent_websites,
            "failed_parent_websites": failed_parent_websites,
            "parent_success_rate_percent": parent_success_rate,
            "total_subsites_attempted": total_subsites_attempted,
            "successful_subsites": successful_subsites,
            "failed_subsites": failed_subsites,
            "skipped_subsites": skipped_subsites,
            "subsite_success_rate_percent": subsite_success_rate,
            "total_cookies_accumulated": total_cookies,
        },
        "profiles_overview": profiles_overview,
        "profiles": merged_profiles,
    }

    # Write atomic
    tmp_path = root / f"recap.json.tmp.{time.time_ns()}"
    try:
        tmp_path.write_text(json.dumps(recap, indent=2), encoding="utf-8")
        tmp_path.replace(recap_file)
    except Exception:
        recap_file.write_text(json.dumps(recap, indent=2), encoding="utf-8")

    return recap, recap_file


def print_recap_summary(recap: dict[str, Any], file_path: Path | str | None = None) -> None:
    """Print an eye-catching, well-formatted recap summary to stdout."""
    stats = recap.get("overall_stats", {})
    profiles_overview = recap.get("profiles_overview", [])
    path_str = str(file_path) if file_path else "profiles/recap.json"

    print("\n" + "=" * 80)
    print("                     PROFILE OPERATION RECAP (recap.json)                     ")
    print("=" * 80)
    print(f"Operation:          {recap.get('operation', 'profile_operation')}")
    print(f"Duration:           {recap.get('duration_sec', 0.0)}s")
    print(f"Profiles:           {stats.get('successful_profiles', 0)}/{stats.get('total_profiles', 0)} succeeded")
    print(
        f"Parent Websites:    {stats.get('successful_parent_websites', 0)}/{stats.get('visited_parent_websites', 0)} "
        f"succeeded ({stats.get('parent_success_rate_percent', 0.0)}%)"
    )
    print(
        f"Subsites Visited:   {stats.get('successful_subsites', 0)}/{stats.get('total_subsites_attempted', 0)} "
        f"succeeded ({stats.get('subsite_success_rate_percent', 0.0)}%, {stats.get('failed_subsites', 0)} failed, {stats.get('skipped_subsites', 0)} skipped)"
    )
    print(f"Total Cookies:      {stats.get('total_cookies_accumulated', 0)}")
    print("-" * 80)
    print(f"{'Profile':<15} {'Status':<12} {'Parents':<12} {'Subsites':<14} {'Cookies':<10} {'Time':<8}")
    print("-" * 80)
    for p in profiles_overview:
        print(
            f"{p.get('profile_name', ''):<15} "
            f"{p.get('status', ''):<12} "
            f"{p.get('parent_websites_visited', ''):<12} "
            f"{p.get('subsites_visited', ''):<14} "
            f"{p.get('cookies', 0):<10} "
            f"{p.get('duration_sec', 0.0):<8.1f}s"
        )
    print("=" * 80)
    print(f"Recap saved to: {Path(path_str).resolve()}")
    print("=" * 80 + "\n")


# =====================================================================
# CLI Entry Point for Standalone Profile Generation
# =====================================================================

def main() -> None:
    """Standalone CLI runner for ProfileCollector."""
    parser = argparse.ArgumentParser(
        description="ProfileCollector: Build persistent browser profiles (profile_<name>) "
                    "by visiting profile URLs and randomly clicking 2 same-domain sublinks."
    )
    parser.add_argument(
        "--profile",
        "-p",
        dest="profile",
        type=str,
        default="finance",
        help=f"Profile name to build. Options: {list(profile_directory.keys())} or 'all' (default: finance)",
    )
    parser.add_argument(
        "--max-sites",
        "-m",
        dest="max_sites",
        type=int,
        default=None,
        help="Maximum number of sites to visit per profile (default: all)",
    )
    parser.add_argument(
        "--timeout",
        "-t",
        dest="timeout",
        type=float,
        default=15.0,
        help="Per-site timeout in seconds (default: 15.0)",
    )
    parser.add_argument(
        "--settle",
        "-s",
        dest="settle",
        type=float,
        default=1.0,
        help="Settle wait in seconds after each navigation/click (default: 1.0)",
    )
    parser.add_argument(
        "--crawlers",
        "-c",
        dest="crawlers",
        type=int,
        default=1,
        help="Number of parallel profile crawls when building all profiles (default: 1)",
    )
    parser.add_argument(
        "--tab-limit",
        "--tabs",
        dest="tab_limit",
        type=int,
        default=5,
        help="Maximum websites open at the same time per browser (default: 5)",
    )
    parser.add_argument(
        "--min-delay",
        dest="min_delay",
        type=float,
        default=45.0,
        help="Minimum randomized delay between actions in seconds (default: 45.0)",
    )
    parser.add_argument(
        "--max-delay",
        dest="max_delay",
        type=float,
        default=75.0,
        help="Maximum randomized delay between actions in seconds (default: 75.0)",
    )
    parser.add_argument(
        "--min-refreshes",
        dest="min_refreshes",
        type=int,
        default=0,
        help="Minimum randomized refreshes per page (default: 0)",
    )
    parser.add_argument(
        "--max-refreshes",
        dest="max_refreshes",
        type=int,
        default=2,
        help="Maximum randomized refreshes per page (default: 2)",
    )
    parser.add_argument(
        "--no-scroll",
        dest="no_scroll",
        action="store_true",
        default=False,
        help="Disable randomized scrolling on pages",
    )
    parser.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=False,
        help="Run browser in headless mode",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        dest="output_dir",
        type=str,
        default="profiles",
        help="Base directory to store profile user_dirs (default: profiles/)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    )
    logger = logging.getLogger("ProfileCollector")

    collector = ProfileCollector(logger=logger)

    if args.profile.lower() == "all":
        print(
            f"Building ALL profiles into '{args.output_dir}' "
            f"with {args.crawlers} parallel crawler(s) (max {args.tab_limit} open websites at the same time, "
            f"action delays: {args.min_delay}s-{args.max_delay}s, refreshes: {args.min_refreshes}-{args.max_refreshes})..."
        )
        summary = asyncio.run(
            collector.build_all_profiles(
                max_sites_per_profile=args.max_sites,
                timeout_per_site=args.timeout,
                settle_sec=args.settle,
                headless=args.headless,
                base_dir=args.output_dir,
                parallel_crawls=args.crawlers,
                max_open_pages_per_browser=args.tab_limit,
                action_delay_min=args.min_delay,
                action_delay_max=args.max_delay,
                enable_random_scroll=not args.no_scroll,
                min_refreshes=args.min_refreshes,
                max_refreshes=args.max_refreshes,
            )
        )
        print("Completed building all profiles.")
    else:
        print(
            f"Building profile '{args.profile}' into '{args.output_dir}' (max {args.tab_limit} open websites, "
            f"action delays: {args.min_delay}s-{args.max_delay}s, refreshes: {args.min_refreshes}-{args.max_refreshes})..."
        )
        summary = asyncio.run(
            collector.build_profile(
                profile_name=args.profile,
                max_sites=args.max_sites,
                timeout_per_site=args.timeout,
                settle_sec=args.settle,
                headless=args.headless,
                base_dir=args.output_dir,
                parallel_crawls=args.crawlers,
                max_open_pages=args.tab_limit,
                action_delay_min=args.min_delay,
                action_delay_max=args.max_delay,
                enable_random_scroll=not args.no_scroll,
                min_refreshes=args.min_refreshes,
                max_refreshes=args.max_refreshes,
            )
        )
        print(
            f"Done. Visited {summary.get('sites_visited', 0)} site(s), "
            f"accumulated {summary.get('accumulated_cookies_count', 0)} cookie(s)."
        )


if __name__ == "__main__":
    main()



