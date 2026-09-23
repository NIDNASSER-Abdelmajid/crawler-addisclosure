"""AdGraph CLI."""

import argparse
import asyncio
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlparse

from crawler import crawl
from Helpers.collectors import ALL_CHOICES, ALL_COLLECTORS, resolve_all


def _normalize_seed_url(raw: str) -> str | None:
    if not raw:
        return None
    val = raw.strip()
    if not val or val.startswith("#"):
        return None
    val = re.sub(r"\(.*?\)", "", val).strip()
    if not val:
        return None
    if " " in val:
        val = val.split()[0].strip()
    if val.lower() in {"n/a", "na", "none", "null", "nan", "-", "unknown"}:
        return None
    if re.fullmatch(r"^[\d.,%+\-]+$", val) or val.endswith("%"):
        return None
    if not val.startswith(("http://", "https://")):
        if not re.search(r"[a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z]{2,}", val):
            return None
        val = f"https://{val}"
    parsed = urlparse(val)
    if parsed.netloc and "." in parsed.netloc:
        return val
    return None


def _load_urls_from_file(path: Path) -> list[str]:
    """Read URLs from a .txt or .csv file with smart header and column detection."""
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return []

    if suffix == ".csv":
        urls: list[str] = []
        rows = list(csv.reader(lines))
        if not rows:
            return []

        # Check if first row is a header
        header = [c.strip().lower() for c in rows[0]]
        url_col_candidates = ["finalurl", "url", "target_url", "inputdomain", "domain", "website", "site", "host", "hostname"]
        
        chosen_col_idx: int | None = None
        # 1. Exact match against candidate list
        for cand in url_col_candidates:
            if cand in header:
                chosen_col_idx = header.index(cand)
                break

        # 2. Substring/token match in header (e.g. "source_domain_provider" containing "domain")
        if chosen_col_idx is None:
            negative_tokens = {"count", "pct", "percent", "share", "meaning", "text", "desc", "id", "len", "length"}
            for cand in ["domain", "url", "website", "site", "host"]:
                for idx, h in enumerate(header):
                    tokens = set(re.split(r"[_\s\-]+", h))
                    if cand in tokens or any(cand in t for t in tokens):
                        if not (tokens & negative_tokens):
                            chosen_col_idx = idx
                            break
                if chosen_col_idx is not None:
                    break

        has_header = chosen_col_idx is not None or any(c in header for c in ["index", "id", "rank", "category", "statuscode", "adstxt", "dimension"])
        data_rows = rows[1:] if has_header else rows

        # 3. Fallback: inspect data rows for column with highest number of valid normalized URLs
        if chosen_col_idx is None:
            best_col = 0
            best_valid_count = 0
            for col_idx in range(len(rows[0])):
                sample_vals = [r[col_idx].strip() for r in data_rows[:20] if len(r) > col_idx]
                valid_count = sum(1 for v in sample_vals if _normalize_seed_url(v) is not None)
                if valid_count > best_valid_count:
                    best_valid_count = valid_count
                    best_col = col_idx
            chosen_col_idx = best_col

        alt_col_idx: int | None = None
        if has_header:
            for cand in ["inputdomain", "domain", "url", "website"]:
                if cand in header and header.index(cand) != chosen_col_idx:
                    alt_col_idx = header.index(cand)
                    break

        seen: set[str] = set()
        for row in data_rows:
            if not row or len(row) <= chosen_col_idx:
                continue
            raw_val = row[chosen_col_idx].strip()
            if not raw_val and alt_col_idx is not None and len(row) > alt_col_idx:
                raw_val = row[alt_col_idx].strip()
            
            norm_url = _normalize_seed_url(raw_val)
            if norm_url and norm_url not in seen:
                seen.add(norm_url)
                urls.append(norm_url)
        return urls

    urls = []
    seen = set()
    for ln in lines:
        norm_url = _normalize_seed_url(ln)
        if norm_url and norm_url not in seen:
            seen.add(norm_url)
            urls.append(norm_url)
    return urls


def _resolve_urls(args: argparse.Namespace) -> list[str]:
    if args.url:
        norm = _normalize_seed_url(args.url)
        return [norm or args.url]

    if args.urls:
        p = Path(args.urls)
        if not p.is_file():
            print(f"[ERR] --urls file not found: {p}", file=sys.stderr)
            sys.exit(1)
        return _load_urls_from_file(p)

    default = Path("urls.txt")
    if default.is_file():
        return _load_urls_from_file(default)

    return []


def _connect_proton_vpn(country: str) -> None:
    """Connect to Proton VPN for a specific country before crawling using Helpers.vpn."""
    from Helpers.vpn import VPNError, connect_vpn, resolve_country
    code, name = resolve_country(country)
    target_display = f"{name} ({code})" if name != code else code
    print(f"[INFO] Connecting Proton VPN to {target_display}...")
    try:
        connect_vpn(country)
        print(f"[OK] Proton VPN connected to {target_display}.")
    except VPNError as exc:
        print(f"[ERR] Failed to connect Proton VPN: {exc}", file=sys.stderr)
        sys.exit(1)


async def _run_all(
    urls: list[str],
    output_dir: str,
    timeout: int,
    headless: bool,
    collectors: list[str],
    cmp_action: str | None = None,
    use_anti_bot: bool = True,
    max_ads: int | None = None,
    crawlers: int = 1,
    executable_path: str | None = None,
    use_safeguards: bool = False,
    production_mode: bool = False,
    depth: tuple[int, int] | list[int] | None = None,
) -> None:
    crawl_id = f"crawl_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    from crawler import setup_playwright_exception_handler
    setup_playwright_exception_handler()

    from timeout_manager import is_url_already_completed, recover_incomplete_attempts
    recovered = recover_incomplete_attempts(output_dir)
    if recovered:
        print(f"[RECOVERY] Recovered {len(recovered)} unfinalized attempt(s) in {output_dir}")

    # Depth crawl configuration: depth = (max_depth_layers, max_links_per_layer)
    max_depth = int(depth[0]) if depth is not None else 0
    max_links = int(depth[1]) if depth is not None else 0
    is_depth_crawl = (depth is not None and max_depth > 0)

    if use_safeguards:
        from safeguard_audit import SafeguardAuditLogger
        from safeguard_config import MAX_SIMULTANEOUS_CRAWLERS
        from safeguard_engine import SafeguardEngine
        from safeguard_state import SafeguardState

        state = SafeguardState()
        audit = SafeguardAuditLogger()
        num_workers = max(1, min(int(crawlers), MAX_SIMULTANEOUS_CRAWLERS))
        engine = SafeguardEngine(
            state, audit,
            worker_id=f"cli-{id(state)}",
            production_mode=production_mode,
        )
    else:
        state = None
        engine = None
        num_workers = max(1, int(crawlers))

    # --- Per-URL crawl helper (each call gets its own full timeout) ---
    async def _crawl_single(
        url: str,
        seed_idx: int,
        depth_level: int,
        parent_url: str | None,
        extract_links: bool,
        root_seed_url: str | None = None,
        exclude_links: set[str] | None = None,
    ) -> dict:
        effective_parent = parent_url if (depth_level > 0 and parent_url) else url
        crawl_kwargs = {
            "output_dir": output_dir,
            "timeout": timeout,
            "headless": headless,
            "collectors": collectors,
            "cmp_action": cmp_action,
            "use_anti_bot": use_anti_bot,
            "max_ads": max_ads,
            "executable_path": executable_path,
            "crawl_id": crawl_id,
            "input_index": seed_idx,
            "extract_links": extract_links,
            "max_links_per_page": max_links if extract_links else None,
            "exclude_links": exclude_links,
            "root_seed_url": root_seed_url,
            "attempt_info": {
                "depth_level": depth_level,
                "depth": depth_level,
                "parent_url": effective_parent,
                "parent": effective_parent,
                "crawl_id": crawl_id,
                "input_index": seed_idx,
            },
        }
        if use_safeguards and engine is not None:
            return await engine.execute_visit_with_retries(url, crawl, crawl_kwargs)
        return await crawl(url, **crawl_kwargs)

    def _result_tag(result: dict) -> str:
        success = result.get("successful")
        if success is True or success == "true":
            return "OK"
        status = result.get("status")
        if status == "timed_out":
            return "TIMEOUT"
        if status == "rejected":
            return "SKIP"
        return "ERR"

    def _result_ads(result: dict) -> int:
        ad_data = result.get("data", {}).get("AdCollector", [])
        return len(ad_data.get("adAttrs", [])) if isinstance(ad_data, dict) else len(ad_data)

    # --- Save discovered URLs to a temp CSV for resumability ---
    def _save_discovered_urls(
        discovered: list[tuple[str, int, str]],
        layer: int,
        root_url: str,
    ) -> Path:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        csv_path = out_dir / f"depth_layer{layer}_{ts}.csv"
        with open(csv_path, "w", encoding="utf-8", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerow(["url", "depth", "parent_url", "root_url"])
            for url, depth_lvl, parent in discovered:
                writer.writerow([url, depth_lvl, parent, root_url])
        print(f"[DEPTH] Saved {len(discovered)} URL(s) for layer {layer} -> {csv_path}")
        return csv_path

    # --- Main orchestration: layer-by-layer depth crawl ---
    from Helpers.link_extractor import canonical_url_key

    total_urls = len(urls)
    progress_lock = asyncio.Lock()
    completed = 0
    total_planned = total_urls  # grows as layers are discovered

    def _progress_tag(done: int, total: int, depth_lvl: int) -> str:
        percent = int((done / total) * 100) if total else 100
        depth_str = f" [Depth {depth_lvl}]" if is_depth_crawl else ""
        return f"[{done}/{total}] ({percent}%){depth_str}"

    def _get_links_from_completed_site(base_dir_path: Path | str, target_url: str, max_count: int | None = None) -> list[str]:
        from timeout_manager import get_website_folder_name
        from Helpers.link_extractor import normalize_and_validate_url
        base_dir = Path(base_dir_path)
        web_folder = get_website_folder_name(target_url)
        site_dir = base_dir / web_folder
        if not site_dir.is_dir():
            return []

        attempt_dirs = sorted([d for d in site_dir.iterdir() if d.is_dir() and d.name.startswith("attempt_")])
        if not attempt_dirs:
            return []

        latest_attempt = attempt_dirs[-1]
        res_path = latest_attempt / "result.json"
        html_path = latest_attempt / "index.html"

        discovered: list[str] = []
        if res_path.is_file():
            try:
                data = json.loads(res_path.read_text(encoding="utf-8"))
                if data.get("discovered_links"):
                    discovered = list(data["discovered_links"])
            except Exception:
                pass

        if not discovered and html_path.is_file():
            try:
                import re
                html_text = html_path.read_text(encoding="utf-8", errors="ignore")
                hrefs = re.findall(r'<a\s+[^>]*href=["\']([^"\'#\s>]+)["\']', html_text, re.IGNORECASE)
                seen_cand = set()
                cand_list = []
                for h in hrefs:
                    norm = normalize_and_validate_url(h, target_url, target_url)
                    if norm and norm not in seen_cand:
                        seen_cand.add(norm)
                        cand_list.append(norm)
                discovered = cand_list
            except Exception:
                pass

        # Filter out target_url, parent, and completed
        filtered = []
        seen = set()
        target_key = canonical_url_key(target_url)
        for u in discovered:
            k = canonical_url_key(u)
            if not k or k == target_key or k in seen:
                continue
            if is_url_already_completed(base_dir, u):
                continue
            seen.add(k)
            filtered.append(u)

        if max_count and max_count > 0 and len(filtered) > max_count:
            import random
            return random.SystemRandom().sample(filtered, max_count)
        return filtered

    # current_layer holds (url, depth_level, parent_url, seed_idx, root_url)
    current_layer: list[tuple[str, int, str | None, int, str]] = []
    visited_urls: set[str] = set()
    visited_url_keys: set[str] = set()
    depth_layer1_from_completed: list[tuple[str, int, str | None, int, str]] = []

    for idx, u in enumerate(urls, start=1):
        clean_u = u.strip()
        if clean_u:
            clean_key = canonical_url_key(clean_u)
            visited_urls.add(clean_u)
            if clean_key:
                visited_url_keys.add(clean_key)

            if is_url_already_completed(output_dir, clean_u):
                print(f"[SKIP] [Already Completed] {clean_u}")
                if is_depth_crawl and max_depth > 0:
                    saved_links = _get_links_from_completed_site(output_dir, clean_u, max_links)
                    for slink in saved_links:
                        slink_key = canonical_url_key(slink)
                        if slink_key and slink_key not in visited_url_keys and not is_url_already_completed(output_dir, slink):
                            visited_url_keys.add(slink_key)
                            visited_urls.add(slink)
                            depth_layer1_from_completed.append((slink, 1, clean_u, idx, clean_u))
            else:
                current_layer.append((clean_u, 0, clean_u, idx, clean_u))

    current_depth_level = 0
    if not current_layer and depth_layer1_from_completed:
        current_layer = depth_layer1_from_completed
        current_depth_level = 1
        total_planned = len(current_layer)
        save_entries = [(u, d, p) for u, d, p, _si, _r in current_layer]
        _save_discovered_urls(save_entries, 1, urls[0] if urls else "unknown")
        print(f"[DEPTH] Seed already completed; progressing directly to Depth 1 with {len(current_layer)} URL(s)")

    try:
        while current_layer:
            should_extract = bool(is_depth_crawl and current_depth_level < max_depth and max_links > 0)
            next_layer: list[tuple[str, int, str | None, int, str]] = []
            semaphore = asyncio.Semaphore(num_workers)

            async def _crawl_one_in_layer(url: str, depth_lvl: int, parent: str | None, seed_idx: int, root_url: str) -> None:
                nonlocal completed
                # Stagger initial concurrent worker spin-up to prevent simultaneous network & DNS spikes
                if num_workers > 1 and seed_idx < num_workers:
                    await asyncio.sleep(seed_idx * 0.75)

                async with semaphore:
                    try:
                        result = await _crawl_single(
                            url,
                            seed_idx,
                            depth_lvl,
                            parent,
                            extract_links=should_extract,
                            root_seed_url=root_url,
                            exclude_links=set(visited_urls),
                        )
                        tag = _result_tag(result)
                        ads = _result_ads(result)

                        async with progress_lock:
                            completed += 1
                            progress = _progress_tag(completed, total_planned, depth_lvl)

                        if tag == "SKIP":
                            reason = result.get("safeguard_reason", "unknown")
                            print(f"[SKIP] {progress} {url}  ->  safeguard: {reason}")
                        else:
                            print(f"[{tag}] {progress} {url}  ->  {ads} ad(s)  |  {result.get('finalUrl', url)}")

                        # Collect discovered links for the next layer (excluding already completed and visited URLs)
                        if should_extract and result.get("discovered_links"):
                            async with progress_lock:
                                for link in result["discovered_links"]:
                                    link_key = canonical_url_key(link)
                                    parent_key = canonical_url_key(parent)
                                    curr_key = canonical_url_key(url)
                                    root_key = canonical_url_key(root_url)

                                    if not link_key:
                                        continue
                                    if link_key in (curr_key, parent_key, root_key):
                                        continue
                                    if link_key in visited_url_keys:
                                        continue
                                    if is_url_already_completed(output_dir, link):
                                        continue

                                    visited_url_keys.add(link_key)
                                    visited_urls.add(link)
                                    next_layer.append((link, depth_lvl + 1, url, seed_idx, root_url))
                    except Exception as exc:
                        async with progress_lock:
                            completed += 1
                            progress = _progress_tag(completed, total_planned, depth_lvl)
                        print(f"[ERR] {progress} {url}  ->  {exc}", file=sys.stderr)

            tasks = [
                asyncio.create_task(_crawl_one_in_layer(url, d, p, si, r))
                for url, d, p, si, r in current_layer
            ]
            await asyncio.gather(*tasks)

            # Prepare next layer
            if next_layer and is_depth_crawl and current_depth_level < max_depth:
                root_url_for_log = current_layer[0][4] if current_layer else "unknown"
                save_entries = [(u, d, p) for u, d, p, _si, _r in next_layer]
                _save_discovered_urls(save_entries, current_depth_level + 1, root_url_for_log)
                total_planned += len(next_layer)
                current_layer = next_layer
                current_depth_level += 1
            else:
                current_layer = []
    finally:
        if state is not None:
            state.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AdGraph Playwright Crawler — detect and screenshot ads on web pages",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    url_group = parser.add_mutually_exclusive_group()
    url_group.add_argument(
        "--url",
        metavar="URL",
        help="A single URL to crawl.",
    )
    url_group.add_argument(
        "--urls",
        metavar="FILE",
        help=(
            "Path to a .txt or .csv file containing one URL per line "
            "(defaults to urls.txt if neither --url nor --urls is given)."
        ),
    )

    parser.add_argument(
        "-d",
        "--collectors",
        "--data-collectors",
        dest="collectors",
        metavar="COLLECTOR",
        nargs="+",
        default=None,
        help=(
            "Data collector(s) to run. You may use commas or spaces as separators. "
            "Short names: ads, cookies, cookiepopup, cookiepopups, requests, screenshot, cmp, cmps, api, apis, apicall, apicalls, fingerprint, fingerprints, target, targets, inclusiontree, disclosure, disclosures. "
            f"Full names: {', '.join(ALL_COLLECTORS)}. "
            "Default: ads."
        ),
    )
    parser.add_argument(
        "positional_collectors",
        nargs="*",
        default=[],
        help="Optional positional collector(s) if -d/--collectors flag is omitted.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        metavar="SECS",
        help="Page-load timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--output-dir",
        default="data",
        metavar="DIR",
        help="Root directory for per-URL result folders (default: data/).",
    )
    parser.add_argument(
        "-vpn",
        "--vpn",
        "-v",
        "--vpn-country",
        dest="vpn",
        nargs="?",
        const="interactive",
        default=None,
        metavar="COUNTRY",
        help=(
            "Connect Proton VPN before crawling. "
            "Pass country initials (e.g. US, FR, DE, UK) or full name (e.g. 'United States'). "
            "If no argument is passed (just -vpn), opens an interactive shell to select the country."
        ),
    )

    parser.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=False,
        help="Run browser in headless mode (off by default).",
    )

    parser.add_argument(
        "-c",
        "--crawlers",
        type=int,
        default=1,
        metavar="N",
        help="Number of URL crawlers to run in parallel (default: 1).",
    )
    parser.add_argument(
        "--cmp-action",
        dest="cmp_action",
        choices=["in", "out", "none"],
        default="none",
        metavar="ACTION",
        help=(
            "Cookie consent action when the cmp collector is active. "
            "'in' = opt in (accept all), 'out' = opt out (reject all), "
            "'none' = detect only, no interaction (default)."
        ),
    )
    parser.add_argument(
        "--anti-bot",
        dest="use_anti_bot",
        action="store_true",
        default=False,
        help="Inject anti-bot script (disabled by default).",
    )
    parser.add_argument(
        "--max-ads",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Maximum number of successfully captured ads when the ads collector is active. "
            "0 means no cap (default)."
        ),
    )
    parser.add_argument(
        "--custom-chromium",
        dest="custom_chromium",
        action="store_true",
        default=False,
        help="Use downloaded custom Chromium snapshot build for crawling instead of Playwright default.",
    )
    parser.add_argument(
        "--chromium-revision",
        dest="chromium_revision",
        metavar="REV",
        default=None,
        help="Chromium snapshot revision/build number to use (default: 1687106, or 'latest').",
    )
    parser.add_argument(
        "--depth",
        dest="depth",
        nargs=2,
        type=int,
        metavar=("LAYERS", "URLS_PER_LAYER"),
        default=None,
        help=(
            "Enable recursive depth crawling. "
            "Accepts two integers: <max_depth_layers> <max_urls_per_layer>. "
            "Example: --depth 2 5 (crawl root, then up to 5 internal links per page for 2 layers deep). "
            "Each URL gets its own full timeout. Discovered URLs are saved to a CSV before crawling."
        ),
    )
    parser.add_argument(
        "--chromium-path",

        dest="chromium_path",
        metavar="PATH",
        default=None,
        help="Path to an existing custom Chromium executable binary.",
    )

    # Safeguard flags
    safeguard_group = parser.add_mutually_exclusive_group()
    safeguard_group.add_argument(
        "--safeguards",
        dest="use_safeguards",
        action="store_true",
        default=False,
        help="Enable ethical safeguards (rate limits, daily quotas, 5xx backoff, emergency stop).",
    )
    safeguard_group.add_argument(
        "--no-safeguards",
        dest="use_safeguards",
        action="store_false",
        help="Disable ethical safeguards (default).",
    )

    parser.add_argument(
        "--production",
        dest="production_mode",
        action="store_true",
        default=False,
        help="Production mode: validates that all pilot-defined thresholds are configured.",
    )
    parser.add_argument(
        "--emergency-stop",
        dest="emergency_stop",
        action="store_true",
        default=False,
        help="Activate the global emergency stop and exit.",
    )
    parser.add_argument(
        "--clear-emergency-stop",
        dest="clear_emergency_stop",
        action="store_true",
        default=False,
        help="Clear the global emergency stop and exit.",
    )
    parser.add_argument(
        "--reset-safeguards",
        dest="reset_safeguards",
        action="store_true",
        default=False,
        help="Reset the safeguard state database (clears all daily limits, 5xx counters, pauses, exclusions, and active leases) and exit.",
    )
    parser.add_argument(
        "--reset-domain",
        dest="reset_domain",
        type=str,
        default=None,
        metavar="DOMAIN",
        help="Reset safeguard state and daily limits for a specific domain and exit.",
    )


    args = parser.parse_args()
    if args.crawlers < 1:
        parser.error("--crawlers must be >= 1")

    depth_config = None
    if args.depth is not None:
        max_depth, max_links = args.depth
        if max_depth < 0 or max_links < 0:
            parser.error("--depth arguments must both be non-negative integers (>= 0)")
        depth_config = (max_depth, max_links)

    raw_collectors: list[str] = []
    if args.collectors:
        raw_collectors.extend(args.collectors)
    if args.positional_collectors:
        raw_collectors.extend(args.positional_collectors)
    if not raw_collectors:
        raw_collectors = ["ads"]

    split_collectors: list[str] = []
    for token in raw_collectors:
        split_collectors.extend([c for c in token.split(",") if c])
    args.collectors = resolve_all(split_collectors)

    urls = _resolve_urls(args)
    if not urls:
        parser.error(
            "No URLs found. Use --url <URL> or --urls <file.txt/csv>, "
            "or place a urls.txt in the current directory."
        )

    cmp_action = None if args.cmp_action == "none" else args.cmp_action

    if args.vpn:
        _connect_proton_vpn(args.vpn)

    # Resolve custom Chromium executable path if requested
    executable_path = None
    if args.chromium_path:
        executable_path = str(Path(args.chromium_path).resolve())
        if not Path(executable_path).is_file():
            parser.error(f"Specified Chromium executable not found: {executable_path}")
    elif args.custom_chromium or args.chromium_revision:
        from Helpers.download_custom_chromium import ChromiumDownloadError, ensure_custom_chromium
        rev = args.chromium_revision or "1687106"
        print(f"Ensuring custom Chromium (revision {rev}) is ready...")
        try:
            executable_path = ensure_custom_chromium(revision=rev)
            print(f"Custom Chromium ready: {executable_path}")
        except ChromiumDownloadError as exc:
            print(f"[ERR] Failed to prepare custom Chromium: {exc}", file=sys.stderr)
            sys.exit(1)

    active_crawlers = max(1, min(args.crawlers, len(urls)))
    info = [
        f"Crawling {len(urls)} URL(s)",
        f"collectors={args.collectors}",
        f"cmp_action={args.cmp_action}",
        f"timeout={args.timeout}s",
        f"crawlers={active_crawlers}",
    ]
    if depth_config:
        info.append(f"depth=layers:{depth_config[0]},urls/layer:{depth_config[1]}")
    if executable_path:
        info.append(f"chromium={Path(executable_path).name} (r{args.chromium_revision or '1687106'})")
    if args.vpn:
        from Helpers.vpn import resolve_country
        v_code, v_name = resolve_country(args.vpn)
        info.append(f"vpn={v_name} ({v_code})")
    if args.headless:
        info.append("headless")
    if args.use_anti_bot:
        info.append("anti_bot")
    max_ads = args.max_ads if args.max_ads > 0 else None
    if max_ads is not None and "AdCollector" in args.collectors:
        info.append(f"max_ads={max_ads}")
    # Handle emergency stop commands before crawling
    if args.emergency_stop:
        from safeguard_audit import SafeguardAuditLogger
        from safeguard_state import SafeguardState
        state = SafeguardState()
        audit = SafeguardAuditLogger()
        state.activate_emergency_stop("cli", "Activated via --emergency-stop")
        from safeguard_audit import EventType
        audit.log_event(EventType.EMERGENCY_STOP_ACTIVATED, safeguard="emergency_stop", action="activated", reason_code="cli_flag")
        print("[EMERGENCY STOP] Activated. All crawlers will stop.")
        state.close()
        return

    if args.clear_emergency_stop:
        from safeguard_audit import SafeguardAuditLogger
        from safeguard_state import SafeguardState
        state = SafeguardState()
        audit = SafeguardAuditLogger()
        state.clear_emergency_stop("cli", "Cleared via --clear-emergency-stop")
        from safeguard_audit import EventType
        audit.log_event(EventType.EMERGENCY_STOP_CLEARED, safeguard="emergency_stop", action="cleared")
        print("[EMERGENCY STOP] Cleared. Crawlers may resume.")
        state.close()
        return

    if args.reset_safeguards:
        from safeguard_state import SafeguardState
        state = SafeguardState()
        state.reset_all_state()
        print("[SAFEGUARD RESET] Successfully reset safeguard state database (cleared all daily visit counts, intervals, pauses, 5xx counters, backoffs, and active leases).")
        state.close()
        return

    if args.reset_domain:
        from safeguard_state import SafeguardState
        state = SafeguardState()
        state.reset_domain(args.reset_domain)
        print(f"[SAFEGUARD RESET] Successfully reset safeguard state for domain: {args.reset_domain}")
        state.close()
        return


    if args.use_safeguards:
        info.append("safeguards=ON")
    if args.production_mode:
        info.append("production")
    print("  |  ".join(info))
    asyncio.run(
        _run_all(
            urls,
            args.output_dir,
            args.timeout,
            args.headless,
            args.collectors,
            cmp_action,
            args.use_anti_bot,
            max_ads,
            args.crawlers,
            executable_path=executable_path,
            use_safeguards=args.use_safeguards,
            production_mode=args.production_mode,
            depth=depth_config,
        )
    )



if __name__ == "__main__":
    main()

