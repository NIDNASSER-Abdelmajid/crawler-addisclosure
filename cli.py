"""AdGraph CLI."""

import argparse
import asyncio
import csv
import shutil
import subprocess
import sys
from pathlib import Path

from crawler import crawl
from Helpers.collectors import ALL_CHOICES, ALL_COLLECTORS, resolve_all


def _load_urls_from_file(path: Path) -> list[str]:
    """Read URLs from a .txt or .csv file (one URL per line, # lines skipped)."""
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")

    if suffix == ".csv":
        urls: list[str] = []
        for row in csv.reader(text.splitlines()):
            if row:
                cell = row[0].strip()
                if cell and not cell.startswith("#"):
                    urls.append(cell)
        return urls

    return [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def _resolve_urls(args: argparse.Namespace) -> list[str]:
    if args.url:
        return [args.url]

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
    """Connect to Proton VPN for a specific country before crawling."""
    country_value = country.strip()
    if not country_value:
        print("[ERR] -v/--vpn-country requires a non-empty country value.", file=sys.stderr)
        sys.exit(1)

    candidate_commands = [
        ["protonvpn", "connect", "--country", country_value],
        ["protonvpn-cli", "connect", "--country", country_value],
        ["protonvpn", "c", "--cc", country_value],
        ["protonvpn-cli", "c", "--cc", country_value],
    ]

    available_executables = {cmd[0] for cmd in candidate_commands if shutil.which(cmd[0])}
    if not available_executables:
        print(
            "[ERR] Proton CLI not found (expected 'protonvpn' or 'protonvpn-cli').",
            file=sys.stderr,
        )
        sys.exit(1)

    last_error: str | None = None
    for cmd in candidate_commands:
        if cmd[0] not in available_executables:
            continue

        print(f"[INFO] Running VPN connect command: {' '.join(cmd)}")
        try:
            completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except OSError as exc:
            last_error = f"{' '.join(cmd)} failed to start: {exc}"
            continue

        if completed.returncode == 0:
            if completed.stdout.strip():
                print(completed.stdout.strip())
            print(f"[OK] Proton VPN connected to '{country_value}'.")
            return

        stderr_text = completed.stderr.strip()
        stdout_text = completed.stdout.strip()
        details = stderr_text or stdout_text or f"exit code {completed.returncode}"
        last_error = f"{' '.join(cmd)} failed: {details}"

    print(
        f"[ERR] Failed to connect Proton VPN to '{country_value}'. {last_error or ''}".strip(),
        file=sys.stderr,
    )
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
) -> None:
    total_urls = len(urls)
    number_of_crawlers = max(1, min(int(crawlers), total_urls))
    semaphore = asyncio.Semaphore(number_of_crawlers)
    progress_lock = asyncio.Lock()
    completed = 0

    def _progress_tag(done: int) -> str:
        percent = int((done / total_urls) * 100) if total_urls else 100
        return f"[{done}/{total_urls}] ({percent}%)"

    async def _crawl_one(_index: int, url: str) -> None:
        nonlocal completed
        async with semaphore:
            try:
                result = await crawl(
                    url,
                    output_dir=output_dir,
                    timeout=timeout,
                    headless=headless,
                    collectors=collectors,
                    cmp_action=cmp_action,
                    use_anti_bot=use_anti_bot,
                    max_ads=max_ads,
                )
                data = result["data"]
                ad_data = data.get("AdCollector", [])
                if isinstance(ad_data, dict):
                    total_ads = len(ad_data.get("adAttrs", []))
                else:
                    total_ads = len(ad_data)
                async with progress_lock:
                    completed += 1
                    progress = _progress_tag(completed)
                print(f"[OK] {progress} {url}  ->  {total_ads} ad(s)  |  {result['finalUrl']}")
            except Exception as exc:
                async with progress_lock:
                    completed += 1
                    progress = _progress_tag(completed)
                print(f"[ERR] {progress} {url}  ->  {exc}", file=sys.stderr)

    tasks = [
        asyncio.create_task(_crawl_one(index, url))
        for index, url in enumerate(urls, start=1)
    ]
    await asyncio.gather(*tasks)


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
        dest="collectors",
        metavar="COLLECTOR",
        nargs="+",
        default=["ads"],
        help=(
            "Data collector(s) to run. You may use commas or spaces as separators. "
            "Short names: ads, cookies, cookiepopup, cookiepopups, requests, screenshot, cmp, api, apis, apicall, apicalls, fingerprint, fingerprints, target, targets, inclusiontree. "
            f"Full names: {', '.join(ALL_COLLECTORS)}. "
            "Default: ads."
        ),
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
        "-v",
        "--vpn-country",
        dest="vpn_country",
        metavar="COUNTRY",
        help=(
            "Connect Proton VPN to COUNTRY before crawling "
            "(for example: US or \"United States\")."
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

    args = parser.parse_args()
    if args.crawlers < 1:
        parser.error("--crawlers must be >= 1")

    split_collectors: list[str] = []
    for token in args.collectors:
        split_collectors.extend([c for c in token.split(",") if c])
    args.collectors = resolve_all(split_collectors)

    urls = _resolve_urls(args)
    if not urls:
        parser.error(
            "No URLs found. Use --url <URL> or --urls <file.txt/csv>, "
            "or place a urls.txt in the current directory."
        )

    cmp_action = None if args.cmp_action == "none" else args.cmp_action

    if args.vpn_country:
        _connect_proton_vpn(args.vpn_country)

    active_crawlers = max(1, min(args.crawlers, len(urls)))
    info = [
        f"Crawling {len(urls)} URL(s)",
        f"collectors={args.collectors}",
        f"cmp_action={args.cmp_action}",
        f"timeout={args.timeout}s",
        f"crawlers={active_crawlers}",
    ]
    if args.vpn_country:
        info.append(f"vpn_country={args.vpn_country}")
    if args.headless:
        info.append("headless")
    if args.use_anti_bot:
        info.append("anti_bot")
    max_ads = args.max_ads if args.max_ads > 0 else None
    if max_ads is not None and "AdCollector" in args.collectors:
        info.append(f"max_ads={max_ads}")
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
        )
    )


if __name__ == "__main__":
    main()

