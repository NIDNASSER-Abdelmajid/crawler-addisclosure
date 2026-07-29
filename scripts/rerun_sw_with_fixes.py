import argparse
import asyncio
import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawler import crawl
from Helpers.collectors import resolve_all


def load_urls(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")

    if suffix == ".csv":
        urls: list[str] = []
        for row in csv.reader(text.splitlines()):
            if not row:
                continue
            cell = row[0].strip()
            if cell and not cell.startswith("#"):
                urls.append(cell)
        return urls

    return [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def parse_collectors(raw: str) -> list[str]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return resolve_all(parts)


def ad_count_from_result(result: dict) -> int:
    ad_data = result.get("data", {}).get("AdCollector", [])
    if isinstance(ad_data, dict):
        return len(ad_data.get("adAttrs", []))
    if isinstance(ad_data, list):
        return len(ad_data)
    return 0


async def run_one_with_retries(
    url: str,
    output_dir: str,
    collectors: list[str],
    timeout: int,
) -> tuple[dict, list[dict]]:
    attempts = [
        {
            "name": "baseline",
            "timeout": timeout,
            "cmp_action": "in",
            "use_anti_bot": True,
            "headless": False,
        },
        {
            "name": "longer_timeout",
            "timeout": max(timeout + 30, timeout * 2),
            "cmp_action": "none",
            "use_anti_bot": True,
            "headless": False,
        },
        {
            "name": "safe_fallback",
            "timeout": max(timeout + 60, timeout * 3),
            "cmp_action": "none",
            "use_anti_bot": False,
            "headless": True,
        },
    ]

    attempt_rows: list[dict] = []
    final_status = "false"
    final_url = ""
    final_ads = 0
    fixed_by_retry = False
    error_text = ""

    for idx, cfg in enumerate(attempts, start=1):
        start = time.time()
        hard_timeout = int(cfg["timeout"]) + 45
        try:
            cmp_action = None if cfg["cmp_action"] == "none" else cfg["cmp_action"]
            result = await asyncio.wait_for(
                crawl(
                    url,
                    output_dir=output_dir,
                    timeout=int(cfg["timeout"]),
                    headless=bool(cfg["headless"]),
                    collectors=collectors,
                    cmp_action=cmp_action,
                    use_anti_bot=bool(cfg["use_anti_bot"]),
                ),
                timeout=hard_timeout,
            )
            status = str(result.get("successful", "false"))
            final_url = str(result.get("finalUrl", ""))
            ads = ad_count_from_result(result)
            err = ""
        except asyncio.TimeoutError:
            status = "timeout"
            ads = 0
            err = f"Runner timeout after {hard_timeout}s"
        except Exception as exc:
            status = "error"
            ads = 0
            err = f"{type(exc).__name__}: {exc}"

        elapsed = round(time.time() - start, 2)
        attempt_rows.append(
            {
                "url": url,
                "attempt": idx,
                "profile": cfg["name"],
                "timeout": cfg["timeout"],
                "cmp_action": cfg["cmp_action"],
                "anti_bot": cfg["use_anti_bot"],
                "headless": cfg["headless"],
                "status": status,
                "ads": ads,
                "seconds": elapsed,
                "error": err,
            }
        )

        if status == "true":
            final_status = "true"
            final_ads = ads
            fixed_by_retry = idx > 1
            error_text = ""
            break

        final_status = status
        error_text = err

    summary = {
        "url": url,
        "final_status": final_status,
        "ads": final_ads,
        "fixed_by_retry": fixed_by_retry,
        "final_url": final_url,
        "last_error": error_text,
    }
    return summary, attempt_rows


async def main_async(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    if not input_path.is_file():
        raise SystemExit(f"Input file not found: {input_path}")

    urls = load_urls(input_path)
    if args.max_urls > 0:
        urls = urls[: args.max_urls]

    if not urls:
        raise SystemExit("No URLs found to process.")

    collectors = parse_collectors(args.collectors)

    report_path = Path(args.report)
    attempts_path = Path(args.attempts)
    failures_path = Path(args.failures)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    attempts_path.parent.mkdir(parents=True, exist_ok=True)
    failures_path.parent.mkdir(parents=True, exist_ok=True)

    done_urls: set[str] = set()
    if args.resume and report_path.exists():
        with report_path.open("r", encoding="utf-8", newline="") as existing:
            reader = csv.DictReader(existing)
            for row in reader:
                url = (row.get("url") or "").strip()
                if url:
                    done_urls.add(url)

    if done_urls:
        urls = [u for u in urls if u not in done_urls]
        print(f"Resuming run: skipped {len(done_urls)} already-processed URL(s), remaining={len(urls)}")

    if not urls:
        print("No remaining URLs to process.")
        return

    report_mode = "a" if args.resume and report_path.exists() else "w"
    attempts_mode = "a" if args.resume and attempts_path.exists() else "w"

    with report_path.open(report_mode, encoding="utf-8", newline="") as rep_f, attempts_path.open(
        attempts_mode, encoding="utf-8", newline=""
    ) as att_f:
        rep_writer = csv.DictWriter(
            rep_f,
            fieldnames=["url", "final_status", "ads", "fixed_by_retry", "final_url", "last_error"],
        )
        att_writer = csv.DictWriter(
            att_f,
            fieldnames=[
                "url",
                "attempt",
                "profile",
                "timeout",
                "cmp_action",
                "anti_bot",
                "headless",
                "status",
                "ads",
                "seconds",
                "error",
            ],
        )
        if report_mode == "w":
            rep_writer.writeheader()
        if attempts_mode == "w":
            att_writer.writeheader()

        total = len(urls)
        stats = {"successes": 0, "fixed": 0}
        failures: list[str] = []
        write_lock = asyncio.Lock()

        crawlers = max(1, min(int(args.crawlers), total))
        print(f"Concurrent crawlers={crawlers}")

        queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        for item in enumerate(urls, start=1):
            queue.put_nowait(item)

        async def process_url(idx: int, url: str) -> None:
            print(f"[{idx}/{total}] {url}")
            try:
                summary, attempt_rows = await run_one_with_retries(
                    url=url,
                    output_dir=args.output_dir,
                    collectors=collectors,
                    timeout=args.timeout,
                )
            except Exception as exc:
                summary = {
                    "url": url,
                    "final_status": "error",
                    "ads": 0,
                    "fixed_by_retry": False,
                    "final_url": "",
                    "last_error": f"runner_exception: {type(exc).__name__}: {exc}",
                }
                attempt_rows = [
                    {
                        "url": url,
                        "attempt": 0,
                        "profile": "runner",
                        "timeout": 0,
                        "cmp_action": "none",
                        "anti_bot": False,
                        "headless": True,
                        "status": "error",
                        "ads": 0,
                        "seconds": 0,
                        "error": summary["last_error"],
                    }
                ]

            async with write_lock:
                for row in attempt_rows:
                    att_writer.writerow(row)
                att_f.flush()

                rep_writer.writerow(summary)
                rep_f.flush()

                if summary["final_status"] == "true":
                    stats["successes"] += 1
                    if summary["fixed_by_retry"]:
                        stats["fixed"] += 1
                    print(
                        f"  -> OK ads={summary['ads']} fixed_by_retry={summary['fixed_by_retry']}"
                    )
                else:
                    failures.append(url)
                    print(f"  -> FAIL status={summary['final_status']} error={summary['last_error']}")

            if args.sleep_ms > 0:
                await asyncio.sleep(args.sleep_ms / 1000.0)

        async def worker(worker_id: int) -> None:
            while True:
                try:
                    idx, url = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await process_url(idx, url)
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker(i + 1)) for i in range(crawlers)]
        await queue.join()
        await asyncio.gather(*workers)

    with failures_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        for url in failures:
            writer.writerow([url])

    total = len(urls)
    print("Done")
    print(f"total={total}")
    print(f"success={stats['successes']}")
    print(f"failed={len(failures)}")
    print(f"fixed_by_retry={stats['fixed']}")
    print(f"report={report_path}")
    print(f"attempts={attempts_path}")
    print(f"failures={failures_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run crawler on URLs with retry fallback for timeout/error cases.",
    )
    parser.add_argument("--input", default="sw.csv", help="Input URL list (.csv or .txt)")
    parser.add_argument("--output-dir", default="kids_results_sw_recheck", help="Output directory for crawl folders")
    parser.add_argument("--timeout", type=int, default=60, help="Base timeout in seconds for baseline attempt")
    parser.add_argument(
        "--collectors",
        default="ads,requests,cookies,screenshot,fingerprints,cmp",
        help="Comma-separated collectors",
    )
    parser.add_argument("--report", default="results/sw_recheck_report.csv", help="Summary CSV output")
    parser.add_argument("--attempts", default="results/sw_recheck_attempts.csv", help="Per-attempt CSV output")
    parser.add_argument("--failures", default="results/sw_recheck_failures.csv", help="Final failed URLs CSV")
    parser.add_argument("--max-urls", type=int, default=0, help="Optional limit for quick runs (0 = all)")
    parser.add_argument("--sleep-ms", type=int, default=0, help="Optional delay between URLs in milliseconds")
    parser.add_argument(
        "-c",
        "--crawlers",
        "--parallel",
        dest="crawlers",
        type=int,
        default=1,
        help="Number of concurrent crawlers (URL workers).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Resume from existing report file by skipping already-processed URLs (default: enabled).",
    )
    parser.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Disable resume mode and start reports from scratch.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
