"""AdGraph Playwright crawler."""
import asyncio
from datetime import datetime, timezone
import html as html_lib
import json
import re
import sys
import time
import os
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from Helpers.anti_bot import anti_bot_script
from Helpers.crawl_context import (
    SCHEMA_VERSION,
    CrawlContext,
    EventCounter,
    generate_document_id,
)

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

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
from Helpers.hasher import get_folder_name, get_registrable_domain, get_url_hash
from Helpers.link_extractor import extract_internal_links
from Helpers.logger import close_logger, get_logger

from timeout_manager import (
    AttemptMetadata,
    finalize_and_save_attempt,
    generate_attempt_id,
    generate_website_id,
    get_attempt_dir,
    get_website_folder_name,
)

# Force UTF-8 output on Windows consoles that default to cp1252
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# Stage-specific timeout configuration (seconds)
PAGE_LOAD_TIMEOUT = 30.0          # 30-second timeout for loading each page
POST_LOAD_WAIT_SECONDS = 5.0     # 5-second wait after loading for dynamic content and ads

STAGE_TIMEOUTS = {
    "CookiePopupsCollector": 15.0,     # Consent handling
    "AdCollector": 120.0,               # Ad collection & disclosures & clicks
    "RequestCollector": 30.0,          # Network request collection & attribution
    "CookieCollector": 30.0,           # Cookie extraction
    "ScreenshotCollector": 30.0,       # Full page screenshot
    "FingerprintCollector": 30.0,      # Canvas/audio/WebGL fingerprint calls
    "APICallCollector": 30.0,          # Storage/API call extraction
    "InclusionTreeCollector": 30.0,    # iframe/script inclusion trees
    "TargetCollector": 30.0,           # Window/target handles
}
DEFAULT_STAGE_TIMEOUT = 15.0

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
    """Navigate directly with domcontentloaded, then allow a fast load state wait.

    Modern ad-heavy websites keep background telemetry streams open indefinitely,
    causing networkidle to always stall and waste 10-15s of the timeout budget.
    """
    total_ms = max(5000, int(remaining_seconds * 1000))
    response = await page.goto(url, timeout=total_ms, wait_until="domcontentloaded")

    try:
        await page.wait_for_load_state("load", timeout=3000)
    except Exception:
        pass

    return response


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
    if not isinstance(ad_data, dict):
        return

    request_index = _build_request_index(request_data) if isinstance(request_data, list) else {}

    api_data = result.get("data", {}).get(APICallCollector.COLLECTOR_NAME)
    fp_data = result.get("data", {}).get(FingerprintCollector.COLLECTOR_NAME)
    api_calls = api_data.get("savedCalls", []) if isinstance(api_data, dict) else []
    fp_calls = fp_data.get("savedCalls", []) if isinstance(fp_data, dict) else []

    api_index: dict[str, list[str]] = {}
    for call in api_calls:
        if isinstance(call, dict) and call.get("source"):
            norm = _normalize_request_url(call["source"])
            desc = call.get("description", "")
            if norm and desc:
                api_index.setdefault(norm, [])
                if desc not in api_index[norm]:
                    api_index[norm].append(desc)

    fp_index: dict[str, list[str]] = {}
    for call in fp_calls:
        if isinstance(call, dict) and call.get("source"):
            norm = _normalize_request_url(call["source"])
            desc = call.get("description", "")
            if norm and desc:
                fp_index.setdefault(norm, [])
                if desc not in fp_index[norm]:
                    fp_index[norm].append(desc)

    for ad in ad_data.get("adAttrs", []):
        for frame in ad.get("adLinksAndImages", []):
            frame_scripts = frame.get("scripts") if isinstance(frame.get("scripts"), list) else []
            for field in ("imgs", "bgImgs", "videos", "iframes"):
                for item in frame.get(field, []):
                    if not isinstance(item, dict):
                        continue
                    src_val = item.get("src")
                    if src_val:
                        if request_index:
                            attribution = _request_attribution_for_src(src_val, request_index)
                            if attribution:
                                item["requestAttribution"] = attribution
                        norm_src = _normalize_request_url(src_val)
                        if norm_src in api_index:
                            item["apiCallSources"] = api_index[norm_src]
                        if norm_src in fp_index:
                            item["fingerprintCallSources"] = fp_index[norm_src]
                    if frame_scripts and field in {"bgImgs", "imgs"}:
                        item.setdefault("frameScripts", frame_scripts)

            links = frame.get("links", [])
            if isinstance(links, list):
                for item in links:
                    target_items = item if isinstance(item, list) else [item]
                    for sub_item in target_items:
                        if not isinstance(sub_item, dict):
                            continue
                        href_val = sub_item.get("href")
                        if href_val:
                            if request_index:
                                attribution = _request_attribution_for_src(href_val, request_index)
                                if attribution:
                                    sub_item["requestAttribution"] = attribution
                            norm_href = _normalize_request_url(href_val)
                            if norm_href in api_index:
                                sub_item["apiCallSources"] = api_index[norm_href]
                            if norm_href in fp_index:
                                sub_item["fingerprintCallSources"] = fp_index[norm_href]


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
    if page is None or getattr(page, "is_closed", lambda: True)():
        return None

    # Get frame depth (for bottom-up nesting)
    def depth(f):
        d = 0
        while f.parent_frame:
            f = f.parent_frame
            d += 1
        return d

    # Inline every child iframe's HTML directly into its own <iframe srcdoc="..."> 
    # Evaluate deepest frames first so we recursively encapsulate tree content!
    try:
        sorted_frames = sorted(page.frames, key=depth, reverse=True)
        for frame in sorted_frames[:15]:
            if frame == page.main_frame or getattr(frame, "is_detached", lambda: False)():
                continue
            try:
                content = await asyncio.wait_for(frame.content(), timeout=1.0)
                frame_url = getattr(frame, "url", "") or ""
                network = _infer_ad_network(frame_url, content)
                
                handle = await asyncio.wait_for(frame.frame_element(), timeout=1.0)
                if handle:
                    await handle.evaluate("""(node, args) => {
                        node.srcdoc = args.content;
                        if (args.network) {
                            node.setAttribute("data-adgraph-network", args.network);
                        }
                    }""", {"content": content, "network": network})
            except Exception as exc:
                logger.debug(f"Failed to inline frame HTML for {getattr(frame, 'url', 'unknown')}: {exc}")
    except Exception as exc:
        logger.debug(f"Failed to process iframe frames for snapshot: {exc}")

    try:
        main_html = await asyncio.wait_for(page.content(), timeout=2.0)
    except Exception as exc:
        logger.debug(f'HTML snapshot skipped: failed to read main page HTML: {exc}')
        return None

    index_path = site_dir / "index.html"
    index_path.write_text(main_html, encoding="utf-8")
    logger.info(f"Saved HTML snapshot with inline iframes -> {index_path}")
    return index_path


async def _extract_all_collector_data(
    result: dict,
    collector_names: list[str],
    pre_crawl_instances: dict[str, Any],
    ad_collector_instance: AdCollector | None,
    page: Page | None,
    context: Any,
    site_dir: Path,
    final_url: str,
    logger,
) -> None:
    """Ensure every requested collector extracts and populates its data into result['data']."""
    data = result.setdefault("data", {})

    for name in collector_names:
        if name == AdCollector.COLLECTOR_NAME:
            if name not in data or not data[name]:
                if ad_collector_instance is not None:
                    data[name] = ad_collector_instance.get_partial_results()
                else:
                    data[name] = {
                        "scrapeResults": {
                            "nDetectedAds": 0,
                            "nAdsScraped": 0,
                            "nSmallAds": 0,
                            "nEmptyAds": 0,
                            "nRemovedAds": 0,
                            "nSkippedAds": 0,
                            "nTimedOutAds": 0,
                            "nAdDisclosureMatched": 0,
                            "nAdDisclosureUnmatched": 0,
                            "nClickedAdChoices": 0,
                        },
                        "adAttrs": [],
                        "visitedAdUrls": [],
                        "unmatchedAdDisclosureContents": [],
                    }

        elif name == RequestCollector.COLLECTOR_NAME:
            if name not in data or not data[name]:
                rc = pre_crawl_instances.get(name)
                if rc is not None:
                    try:
                        data[name] = rc.get_partial_results(final_url)
                    except Exception as exc:
                        logger.debug(f"[RequestCollector] Partial extraction error: {exc}")
                        data.setdefault(name, [])
                else:
                    data.setdefault(name, [])

        elif name == CookieCollector.COLLECTOR_NAME:
            if name not in data or not data[name]:
                try:
                    if context is not None:
                        raw_cookies = await context.cookies()
                        cookie_list = []
                        for c in raw_cookies:
                            expires_raw = c.get("expires", -1)
                            is_session = expires_raw == -1 or expires_raw is None
                            expires_ms = None if is_session else int(expires_raw * 1000)
                            cookie_list.append({
                                "name": c.get("name", ""),
                                "domain": c.get("domain", ""),
                                "path": c.get("path", "/"),
                                "expires": expires_ms,
                                "session": is_session,
                                "sameSite": c.get("sameSite"),
                                "httpOnly": c.get("httpOnly", False),
                                "secure": c.get("secure", False),
                            })
                        data[name] = cookie_list
                    else:
                        data.setdefault(name, [])
                except Exception as exc:
                    logger.debug(f"[CookieCollector] Cookie extraction error: {exc}")
                    data.setdefault(name, [])

        elif name == FingerprintCollector.COLLECTOR_NAME:
            if name not in data or not data[name]:
                fp = pre_crawl_instances.get(name)
                if fp is not None:
                    try:
                        data[name] = fp.get_partial_results()
                    except Exception as exc:
                        logger.debug(f"[FingerprintCollector] Extraction error: {exc}")
                        data.setdefault(name, {"callStats": {}, "savedCalls": []})
                else:
                    data.setdefault(name, {"callStats": {}, "savedCalls": []})

        elif name == APICallCollector.COLLECTOR_NAME:
            if name not in data or not data[name]:
                api = pre_crawl_instances.get(name)
                if api is not None:
                    try:
                        data[name] = api.get_partial_results()
                    except Exception as exc:
                        logger.debug(f"[APICallCollector] Extraction error: {exc}")
                        data.setdefault(name, {"callStats": {}, "savedCalls": []})
                else:
                    data.setdefault(name, {"callStats": {}, "savedCalls": []})

        elif name == CookiePopupsCollector.COLLECTOR_NAME:
            if name not in data or not data[name]:
                cp = pre_crawl_instances.get(name)
                if cp is not None:
                    try:
                        data[name] = cp.get_partial_results()
                    except Exception:
                        data.setdefault(name, [])
                else:
                    data.setdefault(name, [])

        elif name == TargetCollector.COLLECTOR_NAME:
            if name not in data or not data[name]:
                tc = pre_crawl_instances.get(name)
                if tc is not None:
                    try:
                        data[name] = tc.get_partial_results()
                    except Exception:
                        data.setdefault(name, [])
                else:
                    data.setdefault(name, [])

        elif name == InclusionTreeCollector.COLLECTOR_NAME:
            if name not in data or not data[name]:
                it = pre_crawl_instances.get(name)
                if it is not None:
                    try:
                        data[name] = it.get_partial_results()
                    except Exception:
                        data.setdefault(name, {"inclusionTrees": [], "nTrees": 0, "nNodes": 0, "visualisations": []})
                else:
                    data.setdefault(name, {"inclusionTrees": [], "nTrees": 0, "nNodes": 0, "visualisations": []})

        elif name == ScreenshotCollector.COLLECTOR_NAME:
            if name not in data or not data[name]:
                shot_files = list(site_dir.glob("screenshot_*.jpg")) + list(site_dir.glob("screenshot_*.png"))
                if shot_files:
                    data[name] = [{"screenshot": str(shot_files[0]), "filename": str(shot_files[0].name)}]
                elif page is not None and not getattr(page, "is_closed", lambda: True)():
                    try:
                        shot_col = ScreenshotCollector()
                        shot_col.init(str(site_dir), logger, url_hash, crawl_context=crawl_context if 'crawl_context' in locals() else None)
                        data[name] = await shot_col.collect(page)
                    except Exception:
                        data.setdefault(name, [])
                else:
                    data.setdefault(name, [])

        elif name in pre_crawl_instances:
            if name not in data or not data[name]:
                inst = pre_crawl_instances[name]
                if hasattr(inst, "get_partial_results"):
                    data[name] = inst.get_partial_results()
                else:
                    data.setdefault(name, [])



async def crawl(
    url: str,
    output_dir: str = "output",
    headless: bool = True,
    collectors: list[str] | None = None,
    cmp_action: str = "in",
    timeout: float = 30.0,
    use_anti_bot: bool = False,
    max_ads: int | None = None,
    executable_path: str | Path | None = None,
    custom_chromium: bool | str = False,
    chromium_revision: str | None = None,
    _safeguard_callbacks: dict | None = None,
    _attempt_number: int = 1,
    _retry_number: int = 0,
    attempt_info: dict | None = None,
    crawl_id: str | None = None,
    input_index: int | None = None,
    extract_links: bool = False,
    max_links_per_page: int | None = None,
    exclude_links: set[str] | list[str] | None = None,
    root_seed_url: str | None = None,
) -> dict:
    """Crawl a URL with Playwright, run the requested collectors, and write results to disk."""
    if collectors is None:
        collectors = ["AdCollector"]
    collector_names = list(collectors)

    info = attempt_info or {}
    website_id = info.get("website_id") or generate_website_id(url)
    website_folder = info.get("website_folder") or get_website_folder_name(url)
    attempt_id = info.get("attempt_id") or generate_attempt_id()
    attempt_number = int(info.get("attempt_number", _attempt_number))
    crawl_id = info.get("crawl_id") or crawl_id or f"crawl_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    root_attempt_id = info.get("root_attempt_id") or attempt_id
    retry_of_attempt_id = info.get("retry_of_attempt_id")
    worker_id = info.get("worker_id") or "worker_0"
    input_index = info.get("input_index") if info.get("input_index") is not None else input_index
    depth_level = int(info.get("depth_level", 0))
    parent_url = info.get("parent_url")
    if depth_level == 0 and not parent_url:
        parent_url = url

    document_id = generate_document_id()
    event_counter = EventCounter()

    crawl_context = CrawlContext(
        schema_version=SCHEMA_VERSION,
        crawl_id=crawl_id,
        website_id=website_id,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        document_id=document_id,
        publisher_domain=get_registrable_domain(url),
        initial_url=url,
        event_counter=event_counter,
    )

    result: dict = {
        "schema_version": SCHEMA_VERSION,
        "document_id": document_id,
        "initialUrl": url,
        "finalUrl": url,
        "successful": "false",
        "testStarted": int(time.time()),
        "testFinished": None,
        "data": {},
        "depth": depth_level,
        "depth_level": depth_level,
        "parent": parent_url,
        "parent_url": parent_url,
    }

    url_hash = get_url_hash(url)
    site_dir = get_attempt_dir(output_dir, website_folder, attempt_number, attempt_id)
    site_dir.mkdir(parents=True, exist_ok=True)

    logger_name = f"crawler_{url_hash}_{uuid.uuid4().hex[:8]}"
    logger = get_logger(str(site_dir / "crawl.log"), name=logger_name)
    logger.info(f"Starting crawl: {url}  (hash={url_hash}, attempt={attempt_number}, id={attempt_id})")

    # Resolve custom chromium executable if requested
    if (custom_chromium or chromium_revision) and not executable_path:
        from Helpers.download_custom_chromium import ensure_custom_chromium
        rev = chromium_revision if chromium_revision else ("1687106" if isinstance(custom_chromium, bool) else str(custom_chromium))
        logger.info(f"Ensuring custom Chromium (revision {rev}) is ready...")
        executable_path = ensure_custom_chromium(revision=rev, logger=logger)

    if not executable_path:
        env_path = os.environ.get("CHROMIUM_PATH") or os.environ.get("CUSTOM_CHROMIUM_PATH")
        if env_path and Path(env_path).is_file():
            executable_path = env_path

    if executable_path:
        executable_file = Path(executable_path).resolve()
        if not executable_file.is_file():
            raise FileNotFoundError(f"Chromium executable not found at: {executable_file}")
        executable_path = str(executable_file)
        logger.info(f"Using custom Chromium binary: {executable_path}")

    page_loaded = False
    crawl_started = False
    collectors_started = False
    collectors_completed = True
    had_timeout = False
    current_stage = "initialization"
    last_completed_stage = "initialization"
    timeout_stage = ""
    failure_reason = ""
    pre_crawl_instances: dict[str, object] = {}
    ad_collector_instance: AdCollector | None = None
    crawler_exc: Exception | None = None

    async with async_playwright() as pw:
        user_data_dir = site_dir / ".pw_profile"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        launch_kwargs = {
            "headless": headless,
            "args": _launch_args(),
            **_context_options(),
        }
        if executable_path:
            launch_kwargs["executable_path"] = executable_path

        context = await pw.chromium.launch_persistent_context(
            str(user_data_dir),
            **launch_kwargs
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
            current_stage = "pre_crawl"
            for name in collector_names:
                if name == RequestCollector.COLLECTOR_NAME:
                    rc = RequestCollector()
                    rc.init(str(site_dir), logger, url_hash, crawl_context=crawl_context)
                    await rc.pre_crawl(page)
                    pre_crawl_instances[name] = rc
                elif name == APICallCollector.COLLECTOR_NAME:
                    api_col = APICallCollector()
                    api_col.init(str(site_dir), logger, url_hash, crawl_context=crawl_context)
                    await api_col.pre_crawl(page)
                    pre_crawl_instances[name] = api_col
                elif name == FingerprintCollector.COLLECTOR_NAME:
                    fp_col = FingerprintCollector()
                    fp_col.init(str(site_dir), logger, url_hash, crawl_context=crawl_context)
                    await fp_col.pre_crawl(page)
                    pre_crawl_instances[name] = fp_col
                elif name == TargetCollector.COLLECTOR_NAME:
                    target_col = TargetCollector()
                    target_col.init(str(site_dir), logger, url_hash, crawl_context=crawl_context)
                    await target_col.pre_crawl(page)
                    pre_crawl_instances[name] = target_col
                elif name == InclusionTreeCollector.COLLECTOR_NAME:
                    tree_col = InclusionTreeCollector()
                    tree_col.init(str(site_dir), logger, url_hash, crawl_context=crawl_context)
                    await tree_col.pre_crawl(page)
                    pre_crawl_instances[name] = tree_col
                elif name == CookiePopupsCollector.COLLECTOR_NAME:
                    cookie_popup_col = CookiePopupsCollector()
                    cookie_popup_col.init(str(site_dir), logger, url_hash, cmp_action=cmp_action)
                    await cookie_popup_col.pre_crawl(page)
                    pre_crawl_instances[name] = cookie_popup_col

            last_completed_stage = "pre_crawl"
            crawl_started = True

            # --- Safeguard: attach traffic monitor to CDP if available ---
            _sg = _safeguard_callbacks or {}
            _traffic_mon = _sg.get("traffic_monitor")
            _check_estop = _sg.get("check_emergency_stop")
            _heartbeat_fn = _sg.get("heartbeat")
            if _traffic_mon:
                _sg_cdp = await page.context.new_cdp_session(page)
                await _sg_cdp.send("Network.enable")
                _sg_cdp.on("Network.requestWillBeSent", lambda e: _traffic_mon.handle_request(e))
                _sg_cdp.on("Network.loadingFinished", lambda e: _traffic_mon.handle_finished(e))

            # --- 1. Page Load: 30-second bounded timeout ---
            current_stage = "navigation"
            page_timeout_sec = float(timeout) if (timeout and timeout != 30.0 and timeout < 30.0) else PAGE_LOAD_TIMEOUT
            response = await _goto_with_fallback(page, url, page_timeout_sec, logger)
            result["finalUrl"] = page.url
            page_loaded = True
            last_completed_stage = "navigation"

            status = response.status if response else "?"
            logger.info(f"Loaded {page.url}  (HTTP {status})")

            # Store response status for safeguard engine post-processing
            if isinstance(status, int):
                result["_response_status"] = status
                if status == 429 and response:
                    retry_after = response.headers.get("retry-after")
                    if retry_after:
                        result["_retry_after_header"] = retry_after

            # --- 2. 5-second wait after loading for dynamic content and ads ---
            current_stage = "post_load_wait"
            logger.info(f"Waiting {POST_LOAD_WAIT_SECONDS}s for dynamic content, ads, and scripts to settle...")
            await page.wait_for_timeout(int(POST_LOAD_WAIT_SECONDS * 1000))
            last_completed_stage = "post_load_wait"

            # --- 3. Post-navigation & Bot-block / CAPTCHA checks ---
            current_stage = "post_navigation_checks"
            if _safeguard_callbacks:
                from safeguard_captcha import detect_captcha as _detect_captcha
                captcha_result = await _detect_captcha(page)
                if captcha_result:
                    result["_captcha_detected"] = True
                    result["_captcha_signal_type"] = captcha_result.signal_type
                    result["_captcha_signal_value"] = captcha_result.signal_value
                    logger.warning(f"CAPTCHA detected: {captcha_result.signal_type}={captcha_result.signal_value}")

            if _check_estop and _check_estop():
                logger.warning("Emergency stop active — aborting visit")
                result["successful"] = "false"
                result["_emergency_stop"] = True
            elif _traffic_mon and _traffic_mon.exceeded:
                logger.warning(f"Traffic threshold exceeded: {_traffic_mon.exceeded_reason}")
                result["successful"] = "false"
                result["_traffic_exceeded"] = True
            elif await _is_blocked(page):
                logger.warning("Bot-block detected, retrying navigation once")
                await page.wait_for_timeout(5000)
                response = await _goto_with_fallback(page, url, page_timeout_sec, logger)
                result["finalUrl"] = page.url
                status = response.status if response else "?"
                logger.info(f"Retry loaded {page.url}  (HTTP {status})")

            last_completed_stage = "post_navigation_checks"

            # --- 4. Collector execution with stage-specific short timeouts ---
            async def run_collector(name: str):
                nonlocal ad_collector_instance
                if name == AdCollector.COLLECTOR_NAME:
                    collector = AdCollector()
                    ad_collector_instance = collector
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
                    cookie_col.init(str(site_dir), logger, url_hash, crawl_context=crawl_context)
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
                    shot_col.init(str(site_dir), logger, url_hash, crawl_context=crawl_context)
                    return await shot_col.collect(page)
                if name == CookiePopupsCollector.COLLECTOR_NAME:
                    cookie_popup_col = pre_crawl_instances.get(name)
                    return await cookie_popup_col.collect(page) if cookie_popup_col else []

                logger.warning(f"Unknown collector '{name}' — skipping")
                return []

            collectors_started = bool(collector_names)

            # Prioritize ScreenshotCollector if requested so a clean initial page screenshot is saved before heavy ad scraping
            ordered_collectors = list(collector_names)
            if ScreenshotCollector.COLLECTOR_NAME in ordered_collectors:
                ordered_collectors.remove(ScreenshotCollector.COLLECTOR_NAME)
                ordered_collectors.insert(0, ScreenshotCollector.COLLECTOR_NAME)

            for name in ordered_collectors:
                if _check_estop and _check_estop():
                    logger.warning("Emergency stop active — skipping remaining collectors")
                    had_timeout = True
                    timeout_stage = "emergency_stop"
                    break
                if _traffic_mon and _traffic_mon.exceeded:
                    logger.warning(f"Traffic threshold exceeded — skipping remaining collectors")
                    break
                if _heartbeat_fn:
                    _heartbeat_fn()

                current_stage = name.lower()
                stage_timeout = STAGE_TIMEOUTS.get(name, DEFAULT_STAGE_TIMEOUT)

                try:
                    collector_result = await asyncio.wait_for(
                        run_collector(name), timeout=stage_timeout
                    )
                    result["data"][name] = collector_result
                    last_completed_stage = current_stage
                except Exception as col_exc:
                    collectors_completed = False
                    is_to = isinstance(col_exc, asyncio.TimeoutError) or _is_timeout_error(col_exc)
                    if is_to:
                        had_timeout = True
                        timeout_stage = current_stage
                        logger.warning(
                            f"[{name}] Collector reached stage timeout ({stage_timeout}s). "
                            "Saving partial data and continuing to next step."
                        )
                    else:
                        failure_reason = f"{name} error: {col_exc}"
                        logger.error(f"[{name}] Collector error: {col_exc}")

                    # Extract partial results if available
                    if name == AdCollector.COLLECTOR_NAME and ad_collector_instance is not None:
                        result["data"][name] = ad_collector_instance.get_partial_results()
                        result["ad_timeout_no_retry"] = True
                    elif name in pre_crawl_instances:
                        inst = pre_crawl_instances[name]
                        if hasattr(inst, "get_partial_results"):
                            result["data"][name] = inst.get_partial_results()

            # Determine success status and retry policy
            ad_data = result["data"].get(AdCollector.COLLECTOR_NAME, {})
            has_ads = bool(ad_data.get("adAttrs")) if isinstance(ad_data, dict) else bool(ad_data)
            ad_collector_ran = (AdCollector.COLLECTOR_NAME in result["data"]) or (ad_collector_instance is not None)

            if page_loaded and collectors_completed and not had_timeout:
                result["successful"] = "true"
            elif page_loaded and (has_ads or ad_collector_ran):
                # Page loaded and ad collection ran -> mark as successful or timeout with no-retry
                result["successful"] = "true" if has_ads else "timeout"
                result["ad_timeout_no_retry"] = True
            elif had_timeout and (collectors_started or page_loaded):
                result["successful"] = "timeout"
            else:
                result["successful"] = "false"

            # Enforce ad_timeout_no_retry if ad collection started/ran or captured data
            _AD_STAGE_NAMES = {"adcollector", AdCollector.COLLECTOR_NAME.lower()}
            if had_timeout and timeout_stage.lower() in _AD_STAGE_NAMES:
                result["ad_timeout_no_retry"] = True
            elif ad_collector_ran or has_ads:
                result["ad_timeout_no_retry"] = True

        except Exception as exc:
            crawler_exc = exc
            if _is_timeout_error(exc) and (crawl_started or page_loaded or collectors_started):
                result["successful"] = "timeout"
                had_timeout = True
                timeout_stage = current_stage
            else:
                result["successful"] = "false"
                failure_reason = str(exc)
            logger.error(f"Crawl error at stage '{current_stage}': {exc}")

        finally:
            if page_loaded:

                try:
                    await _write_html_snapshot(page, site_dir, logger)
                    last_completed_stage = "html_snapshot"
                except Exception as exc:
                    logger.debug(f"HTML snapshot write error: {exc}")

                if extract_links:
                    try:
                        result["discovered_links"] = await extract_internal_links(
                            page=page,
                            root_url=root_seed_url or url,
                            parent_url=parent_url,
                            max_links=max_links_per_page,
                            exclude_urls=set(exclude_links or []),
                            output_dir=output_dir,
                        )
                        logger.info(f"Extracted {len(result['discovered_links'])} internal link(s) for depth crawl")
                    except Exception as link_exc:
                        logger.debug(f"Link extraction error: {link_exc}")
                        result["discovered_links"] = []
                else:
                    result["discovered_links"] = []
            else:
                result["discovered_links"] = []

            # Guaranteed extraction of all collector data (never leave data field empty on timeout/error)
            try:
                await _extract_all_collector_data(
                    result=result,
                    collector_names=collector_names,
                    pre_crawl_instances=pre_crawl_instances,
                    ad_collector_instance=ad_collector_instance,
                    page=page,
                    context=context if 'context' in locals() else None,
                    site_dir=site_dir,
                    final_url=result.get("finalUrl", url),
                    logger=logger,
                )
            except Exception as extract_exc:
                logger.error(f"Error extracting collector data: {extract_exc}")

            result["testFinished"] = int(time.time())

            # Detach safeguard CDP session if created
            if _safeguard_callbacks and _safeguard_callbacks.get("traffic_monitor"):
                try:
                    await _sg_cdp.detach()
                except Exception:
                    pass

            # Calculate counts
            ad_data = result["data"].get(AdCollector.COLLECTOR_NAME, {})
            if isinstance(ad_data, dict):
                total_ads = len(ad_data.get("adAttrs", []))
                disclosures_count = len(ad_data.get("unmatchedAdDisclosureContents", []))
            elif isinstance(ad_data, list):
                total_ads = len(ad_data)
                disclosures_count = 0
            else:
                total_ads = 0
                disclosures_count = 0

            cookies_count = len(result["data"].get(CookieCollector.COLLECTOR_NAME, []))
            requests_count = len(result["data"].get(RequestCollector.COLLECTOR_NAME, []))
            fingerprints_count = len(result["data"].get(FingerprintCollector.COLLECTOR_NAME, []))
            has_screenshot = bool(result["data"].get(ScreenshotCollector.COLLECTOR_NAME))

            # Determine attempt status
            has_some_data = bool(total_ads or requests_count or cookies_count or fingerprints_count or page_loaded)
            if had_timeout or result.get("successful") == "timeout":
                status = "completed_with_partial_data" if has_some_data else "timed_out"
                result["successful"] = "timeout"
            elif result.get("successful") == "true":
                status = "completed"
            elif crawler_exc and not has_some_data:
                status = "failed"
            else:
                status = "completed_with_partial_data" if has_some_data else "failed"

            try:
                _annotate_ad_request_attribution(result)
            except Exception as attr_exc:
                logger.debug(f"Attribution annotation error: {attr_exc}")

            # Cross-collector frame correlation index
            try:
                from Helpers.frame_correlator import build_frame_correlation_index
                result["frame_correlation_index"] = build_frame_correlation_index(result)
            except Exception as frame_exc:
                logger.debug(f"Frame correlation error: {frame_exc}")
                result["frame_correlation_index"] = {}

            # Determine if ad-collection timeout -> no retry
            ad_timeout_no_retry = bool(result.get("ad_timeout_no_retry", False))

            # Build comprehensive AttemptMetadata
            metadata = AttemptMetadata(
                website_id=website_id,
                normalized_url=url,
                publisher_domain=get_registrable_domain(url),
                crawl_id=crawl_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                retry_of_attempt_id=retry_of_attempt_id,
                root_attempt_id=root_attempt_id,
                worker_id=worker_id,
                started_at=datetime.fromtimestamp(start_time_crawl, timezone.utc).isoformat(),
                ended_at=datetime.now(timezone.utc).isoformat(),
                status=status,
                failure_reason=failure_reason or (str(crawler_exc) if crawler_exc else ""),
                timeout_stage=timeout_stage,
                last_completed_stage=last_completed_stage,
                input_index=input_index,
                output_folder=str(site_dir),
                website_folder=website_folder,
                partial_data=(status == "completed_with_partial_data"),
                collection_complete=(status == "completed"),
                ad_timeout_no_retry=ad_timeout_no_retry,
                ads_count=total_ads,
                disclosures_count=disclosures_count,
                cookies_count=cookies_count,
                requests_count=requests_count,
                fingerprints_count=fingerprints_count,
                screenshots_saved=has_screenshot,
                configured_timeout_sec=timeout,
                actual_duration_sec=time.time() - start_time_crawl,
                error_type=type(crawler_exc).__name__ if crawler_exc else "",
                depth_level=depth_level,
                parent_url=parent_url,
                schema_version=crawl_context.schema_version,
            )
            result["_attempt_metadata"] = metadata.to_dict()

            # Crash-recovery checkpoint: write partial result before final atomic save
            partial_path = site_dir / "_partial_result.json"
            if has_some_data:
                try:
                    from timeout_manager import atomic_write_json
                    atomic_write_json(partial_path, result)
                except Exception as partial_exc:
                    logger.debug(f"Failed writing _partial_result.json: {partial_exc}")

            # GUARANTEED SAVE BEFORE CLOSING BROWSER
            save_success = finalize_and_save_attempt(output_dir, site_dir, result, metadata)
            if not save_success:
                logger.error("[CRITICAL] Failed finalizing attempt save to disk before browser closure!")
            else:
                # Remove temporary partial checkpoint after verified final atomic save
                try:
                    if partial_path.is_file():
                        partial_path.unlink()
                except Exception:
                    pass

            try:
                # Suppress "Future exception was never retrieved" noise from
                # Playwright's pending internal operations (disclosure nav,
                # screenshot retries) that get cancelled on context close.
                loop = asyncio.get_running_loop()
                _original_handler = loop.get_exception_handler()
                loop.set_exception_handler(lambda _loop, ctx: None)
                try:
                    await context.close()
                finally:
                    loop.set_exception_handler(_original_handler)
            except Exception as exc:
                logger.debug(f"Context close error: {exc}")

            # Windows file-lock release retry loop
            for rm_attempt in range(5):
                try:
                    if user_data_dir.exists():
                        shutil.rmtree(user_data_dir)
                    break
                except Exception as exc:
                    await asyncio.sleep(0.15 * (2 ** rm_attempt))

    try:
        logger.info(f"Done. {total_ads} ad(s) found. Status: {status} -> {site_dir}")
        return result
    finally:
        close_logger(logger)
