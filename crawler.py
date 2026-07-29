"""AdGraph Playwright crawler."""
import asyncio
import html as html_lib
import json
import re
import sys
import time
import os
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlparse

from Helpers.anti_bot import anti_bot_script

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

try:
    from playwright_stealth import Stealth as _Stealth
    _STEALTH_AVAILABLE = True
except ImportError:
    _STEALTH_AVAILABLE = False

from Collectors.AdCollector import AdCollector
from Collectors.APICallCollector import APICallCollector
from Collectors.CookieCollector import CookieCollector
from Collectors.CookiePopupsCollector import CookiePopupsCollector
from Collectors.FingerprintCollector import FingerprintCollector
from Collectors.InclusionTreeCollector import InclusionTreeCollector
from Collectors.RequestCollector import RequestCollector
from Collectors.ScreenshotCollector import ScreenshotCollector
from Collectors.TargetCollector import TargetCollector
from Helpers.hasher import get_folder_name, get_url_hash
from Helpers.logger import close_logger, get_logger

# Force UTF-8 output on Windows consoles that default to cp1252
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


_BLOCK_SIGNALS = [
    "access is temporarily restricted",
    "access denied",
    "just a moment",
    "checking your browser",
    "enable javascript and cookies",
    "why do i have to complete a captcha",
    "please enable cookies",
    "attention required",
]


def _is_timeout_error(exc: Exception) -> bool:
    """Return True when an exception represents a timeout condition."""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return "timeout" in name or "timeout" in text


def _launch_args() -> list[str]:
    import os
    extension_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "resources", "consent-o-matic"))
    return [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-infobars",
        "--disable-application-cache",
        "--disk-cache-size=0",
        "--window-size=1900,1000",
        f"--disable-extensions-except={extension_path}",
        f"--load-extension={extension_path}",
    ]


def _context_options() -> dict:
    return {
        "viewport": {"width": 1900, "height": 1000},
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
    }
async def _is_blocked(page) -> bool:
    """Return True if the loaded page looks like a bot-block / challenge page."""
    try:
        title = (await page.title()).lower()
    except Exception:
        return False
    return any(sig in title for sig in _BLOCK_SIGNALS)


def _normalize_request_url(url: str | None) -> str:
    if not url:
        return ""
    return url.split("#", 1)[0]


async def _goto_with_fallback(page, url: str, remaining_seconds: float, logger):
    """Navigate with a bounded networkidle attempt, then fallback to domcontentloaded.

    Some sites (for example X/Twitter) keep long-lived network activity open,
    so a strict networkidle wait can consume the whole timeout budget.
    """
    total_ms = max(1000, int(remaining_seconds * 1000))
    networkidle_ms = min(15000, max(3000, int(total_ms * 0.35)))

    try:
        return await page.goto(url, timeout=networkidle_ms, wait_until="networkidle")
    except PlaywrightTimeoutError:
        logger.info(
            "Navigation did not reach networkidle within %sms; "
            "falling back to domcontentloaded",
            networkidle_ms,
        )

    fallback_ms = max(1000, total_ms - networkidle_ms)
    return await page.goto(url, timeout=fallback_ms, wait_until="domcontentloaded")


def _build_request_index(requests: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for request in requests:
        normalized_url = _normalize_request_url(request.get("url"))
        if not normalized_url:
            continue
        existing = index.get(normalized_url)
        if existing is None or (not existing.get("initiators") and request.get("initiators")):
            index[normalized_url] = request
    return index


def _request_attribution_for_src(src: str | None, request_index: dict[str, dict]) -> dict | None:
    normalized_src = _normalize_request_url(src)
    if not normalized_src:
        return None
    if normalized_src.startswith("data:"):
        return {
            "matchedRequestUrl": None,
            "resourceType": None,
            "status": None,
            "initiators": [],
            "remoteIPAddress": None,
            "note": "No network request exists for data URLs; exact loader script requires runtime assignment instrumentation.",
        }

    request = request_index.get(normalized_src)
    if not request:
        return None

    return {
        "matchedRequestUrl": request.get("url"),
        "resourceType": request.get("type"),
        "status": request.get("status"),
        "initiators": request.get("initiators") or [],
        "remoteIPAddress": request.get("remoteIPAddress"),
    }


def _annotate_ad_request_attribution(result: dict) -> None:
    ad_data = result.get("data", {}).get(AdCollector.COLLECTOR_NAME)
    request_data = result.get("data", {}).get(RequestCollector.COLLECTOR_NAME)
    if not isinstance(ad_data, dict) or not isinstance(request_data, list):
        return

    request_index = _build_request_index(request_data)
    if not request_index:
        return

    for ad in ad_data.get("adAttrs", []):
        for frame in ad.get("adLinksAndImages", []):
            frame_scripts = frame.get("scripts") if isinstance(frame.get("scripts"), list) else []
            for field in ("imgs", "bgImgs", "videos", "iframes"):
                for item in frame.get(field, []):
                    if not isinstance(item, dict):
                        continue
                    attribution = _request_attribution_for_src(item.get("src"), request_index)
                    if attribution:
                        item["requestAttribution"] = attribution
                    if frame_scripts and field in {"bgImgs", "imgs"}:
                        item.setdefault("frameScripts", frame_scripts)

            for link_groups in frame.get("links", []):
                for link in link_groups:
                    if not isinstance(link, dict):
                        continue
                    attribution = _request_attribution_for_src(link.get("href"), request_index)
                    if attribution:
                        link["requestAttribution"] = attribution


def _infer_ad_network(url: str, html_text: str = "") -> str:
    haystack = f"{url} {html_text}".lower()
    if "criteo" in haystack:
        return "criteo"
    if any(token in haystack for token in (
        "googlesyndication",
        "doubleclick",
        "googleads",
        "adservices.google",
        "g.doubleclick.net",
        "pagead",
    )):
        return "google"
    if "taboola" in haystack:
        return "taboola"
    if "outbrain" in haystack:
        return "outbrain"
    return ""


async def _write_html_snapshot(page, site_dir: Path, logger) -> Path | None:
    # Get frame depth (for bottom-up nesting)
    def depth(f):
        d = 0
        while f.parent_frame:
            f = f.parent_frame
            d += 1
        return d

    # Inline every child iframe's HTML directly into its own <iframe srcdoc="..."> 
    # Evaluate deepest frames first so we recursively encapsulate tree content!
    sorted_frames = sorted(page.frames, key=depth, reverse=True)
    for frame in sorted_frames:
        if frame == page.main_frame:
            continue
        try:
            content = await frame.content()
            frame_url = getattr(frame, "url", "") or ""
            network = _infer_ad_network(frame_url, content)
            
            handle = await frame.frame_element()
            if handle:
                await handle.evaluate("""(node, args) => {
                    node.srcdoc = args.content;
                    if (args.network) {
                        node.setAttribute("data-adgraph-network", args.network);
                    }
                }""", {"content": content, "network": network})
        except Exception as exc:
            logger.debug(f"Failed to inline frame HTML for {getattr(frame, 'url', 'unknown')}: {exc}")

    try:
        main_html = await page.content()
    except Exception as exc:
        logger.debug(f"HTML snapshot skipped: failed to read main page HTML: {exc}")
        return None

    index_path = site_dir / "index.html"
    index_path.write_text(main_html, encoding="utf-8")
    logger.info(f"Saved HTML snapshot with inline iframes -> {index_path}")
    return index_path

async def crawl(
    url: str,
    output_dir: str = "data",
    timeout: int = 30,
    headless: bool = False,
    collectors: list[str] | None = None,
    cmp_action: str | None = None,
    use_anti_bot: bool = False,
    max_ads: int | None = None,
) -> dict:
    """Crawl a URL with Playwright, run the requested collectors, and write results to disk."""
    if collectors is None:
        collectors = ["AdCollector"]
    collector_names = list(collectors)

    url_hash = get_url_hash(url)
    base_site_dir = Path(output_dir) / f"{get_folder_name(url)}_{url_hash}"

    # Allocate a unique output directory atomically so repeated URLs can be
    # crawled in parallel without clobbering each other's files.
    suffix = 0
    while True:
        candidate = base_site_dir if suffix == 0 else Path(output_dir) / f"{base_site_dir.name}_{suffix}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            site_dir = candidate
            break
        except FileExistsError:
            suffix += 1

    logger_name = f"crawler_{url_hash}_{uuid.uuid4().hex[:8]}"
    logger = get_logger(str(site_dir / "crawl.log"), name=logger_name)
    logger.info(f"Starting crawl: {url}  (hash={url_hash})")

    result: dict = {
        "initialUrl":   url,
        "finalUrl":     url,
        "successful":   "false",
        "testStarted":  int(time.time()),
        "testFinished": None,
        "data":         {},
    }

    page_loaded = False
    crawl_started = False
    collectors_started = False
    collectors_completed = True
    had_timeout = False

    async with async_playwright() as pw:
        user_data_dir = site_dir / ".pw_profile"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        context = await pw.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=headless,
            args=_launch_args(),
            **_context_options()
        )
        page = context.pages[0] if context.pages else await context.new_page()


        if _STEALTH_AVAILABLE:
            await _Stealth().apply_stealth_async(page)
            logger.info("Stealth mode applied")
        if use_anti_bot:
            await page.add_init_script(anti_bot_script())
        else:
            logger.info("Anti-bot script disabled")

        start_time_crawl = time.time()
        try:
            pre_crawl_instances: dict[str, object] = {}
            for name in collector_names:
                if name == RequestCollector.COLLECTOR_NAME:
                    rc = RequestCollector()
                    rc.init(str(site_dir), logger, url_hash)
                    await rc.pre_crawl(page)
                    pre_crawl_instances[name] = rc
                elif name == APICallCollector.COLLECTOR_NAME:
                    api_col = APICallCollector()
                    api_col.init(str(site_dir), logger, url_hash)
                    await api_col.pre_crawl(page)
                    pre_crawl_instances[name] = api_col
                elif name == FingerprintCollector.COLLECTOR_NAME:
                    fp_col = FingerprintCollector()
                    fp_col.init(str(site_dir), logger, url_hash)
                    await fp_col.pre_crawl(page)
                    pre_crawl_instances[name] = fp_col
                elif name == TargetCollector.COLLECTOR_NAME:
                    target_col = TargetCollector()
                    target_col.init(str(site_dir), logger, url_hash)
                    await target_col.pre_crawl(page)
                    pre_crawl_instances[name] = target_col
                elif name == InclusionTreeCollector.COLLECTOR_NAME:
                    tree_col = InclusionTreeCollector()
                    tree_col.init(str(site_dir), logger, url_hash)
                    await tree_col.pre_crawl(page)
                    pre_crawl_instances[name] = tree_col
                elif name == CookiePopupsCollector.COLLECTOR_NAME:
                    cookie_popup_col = CookiePopupsCollector()
                    cookie_popup_col.init(str(site_dir), logger, url_hash, cmp_action=cmp_action)
                    await cookie_popup_col.pre_crawl(page)
                    pre_crawl_instances[name] = cookie_popup_col

            crawl_started = True
            
            # Bound page goto to the timeout
            remaining_goto = max(1.0, timeout - (time.time() - start_time_crawl))
            response = await _goto_with_fallback(page, url, remaining_goto, logger)
            result["finalUrl"] = page.url
            page_loaded = True

            status = response.status if response else "?"
            logger.info(f"Loaded {page.url}  (HTTP {status})")

            if await _is_blocked(page):
                logger.warning("Bot-block detected, retrying")
                # Adjust remaining time for page wait
                remaining = max(1.0, timeout - (time.time() - start_time_crawl))
                await page.wait_for_timeout(min(8000, int(remaining * 1000)))
                
                remaining = max(1.0, timeout - (time.time() - start_time_crawl))
                response = await _goto_with_fallback(page, url, remaining, logger)
                result["finalUrl"] = page.url
                status = response.status if response else "?"
                logger.info(f"Retry loaded {page.url}  (HTTP {status})")
                if await _is_blocked(page):
                    logger.error("Bot-block persists after retry")

            # Enforce global timeout before collector phase
            if time.time() - start_time_crawl > timeout:
                logger.warning(f"Global timeout set by user ({timeout}s) exceeded. Stopping collection and saving partial results.")
                had_timeout = True
            else:
                async def run_collector(name: str):
                    if name == AdCollector.COLLECTOR_NAME:
                        collector = AdCollector()
                        collector.init(
                            str(site_dir),
                            logger,
                            url_hash,
                            max_ads_captured=max_ads,
                        )
                        return await collector.collect(page)
                    if name == RequestCollector.COLLECTOR_NAME:
                        rc = pre_crawl_instances.get(name)
                        return await rc.collect(page) if rc else []
                    if name == APICallCollector.COLLECTOR_NAME:
                        api_col = pre_crawl_instances.get(name)
                        return await api_col.collect(page) if api_col else []
                    if name == CookieCollector.COLLECTOR_NAME:
                        cookie_col = CookieCollector()
                        cookie_col.init(str(site_dir), logger, url_hash)
                        return await cookie_col.collect(page)
                    if name == FingerprintCollector.COLLECTOR_NAME:
                        fp_col = pre_crawl_instances.get(name)
                        return await fp_col.collect(page) if fp_col else []
                    if name == TargetCollector.COLLECTOR_NAME:
                        target_col = pre_crawl_instances.get(name)
                        return await target_col.collect(page) if target_col else []
                    if name == InclusionTreeCollector.COLLECTOR_NAME:
                        tree_col = pre_crawl_instances.get(name)
                        return await tree_col.collect(page) if tree_col else []
                    if name == ScreenshotCollector.COLLECTOR_NAME:
                        shot_col = ScreenshotCollector()
                        shot_col.init(str(site_dir), logger, url_hash)
                        return await shot_col.collect(page)
                    if name == CookiePopupsCollector.COLLECTOR_NAME:
                        cookie_popup_col = pre_crawl_instances.get(name)
                        return await cookie_popup_col.collect(page) if cookie_popup_col else []

                    logger.warning(f"Unknown collector '{name}' — skipping")
                    return []

                collectors_started = bool(collector_names)

                # Run collectors one-by-one to avoid collector interference on the same page
                # under high URL-level parallelism.
                for name in collector_names:
                    remaining_time = max(1.0, timeout - (time.time() - start_time_crawl))
                    try:
                        collector_result = await asyncio.wait_for(
                            run_collector(name), timeout=remaining_time
                        )
                        result["data"][name] = collector_result
                    except Exception as collector_result:
                        collectors_completed = False
                        if isinstance(collector_result, asyncio.TimeoutError) or _is_timeout_error(collector_result):
                            had_timeout = True
                            logger.warning(f"[{name}] Collector timed out after exceeding the remaining global timeout.")
                        else:
                            logger.error(f"[{name}] Collector error: {collector_result}")
                        result["data"].setdefault(name, [])

            if page_loaded and collectors_completed:
                result["successful"] = "true"
            elif had_timeout and collectors_started:
                result["successful"] = "timeout"
            else:
                result["successful"] = "false"

        except Exception as exc:
            if _is_timeout_error(exc) and (crawl_started or page_loaded or collectors_started):
                result["successful"] = "timeout"
            else:
                result["successful"] = "false"
            logger.error(f"Crawl error: {exc}")

        finally:
            if page_loaded:
                try:
                    await _write_html_snapshot(page, site_dir, logger)
                except Exception as exc:
                    logger.debug(f"HTML snapshot write error: {exc}")
            result["testFinished"] = int(time.time())
            try:
                await context.close()
            except Exception as exc:
                logger.debug(f"Context close error: {exc}")
            try:
                shutil.rmtree(user_data_dir, ignore_errors=True)
            except Exception as exc:
                logger.debug(f"Profile cleanup error: {exc}")

    _annotate_ad_request_attribution(result)

    # Persist result
    result_path = site_dir / "result.json"
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)

    ad_data = result["data"].get(AdCollector.COLLECTOR_NAME, [])
    if isinstance(ad_data, dict):
        total_ads = len(ad_data.get("adAttrs", []))
    else:
        total_ads = len(ad_data)
    try:
        logger.info(f"Done. {total_ads} ad(s) found. Results -> {result_path}")
        return result
    finally:
        close_logger(logger)

