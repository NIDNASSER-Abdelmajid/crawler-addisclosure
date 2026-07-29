"""Run the `dit` tool (when available) against a list of domains.

This script is intended as a preprocessing utility (not part of the main crawler).
It reads a domain list (CSV) and runs `dit` per-domain (or falls back to a stub if not installed).

Output is stored as JSON for later review.
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request as urllib_request
from urllib.parse import urlparse
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _download_latest_dit_to_cache(cache_dir: Path) -> Path:
    """Download the latest `dit` release and extract a platform binary.

    Returns the path to the extracted executable.
    """

    cache_dir.mkdir(parents=True, exist_ok=True)

    # Determine platform-specific asset name.
    sys_plat = sys.platform
    arch = platform.machine().lower()

    if sys_plat.startswith("win"):
        platform_key = "windows"
        ext = ".zip"
    elif sys_plat.startswith("linux"):
        platform_key = "linux"
        ext = ".tar.gz"
    elif sys_plat.startswith("darwin"):
        platform_key = "darwin"
        ext = ".tar.gz"
    else:
        raise RuntimeError(f"Unsupported platform for dit binary download: {sys_plat}")

    # Prefer amd64/x86_64
    if arch in ("amd64", "x86_64"):
        arch_key = "amd64"
    elif arch in ("arm64", "aarch64"):
        arch_key = "arm64"
    else:
        arch_key = "amd64"

    release_api = "https://api.github.com/repos/happyhackingspace/dit/releases/latest"
    with urllib_request.urlopen(release_api, timeout=15) as resp:
        release = json.load(resp)

    tag = release.get("tag_name") or "latest"
    version = tag.lstrip("v")
    assets = release.get("assets", [])

    desired_name = f"dit_{version}_{platform_key}_{arch_key}{ext}"
    match = None
    for asset in assets:
        if asset.get("name") == desired_name:
            match = asset
            break
    if not match:
        raise RuntimeError(f"Could not find dist asset for {desired_name} in release {tag}")

    download_url = match["browser_download_url"]

    archive_path = cache_dir / desired_name
    if not archive_path.exists():
        with urllib_request.urlopen(download_url, timeout=60) as resp:
            archive_path.write_bytes(resp.read())

    extracted_dir = cache_dir / f"dit_{platform_key}_{arch_key}"
    extracted_dir.mkdir(parents=True, exist_ok=True)

    if ext == ".zip":
        with zipfile.ZipFile(archive_path, "r") as z:
            z.extractall(extracted_dir)
    else:
        with tarfile.open(archive_path, "r:gz") as t:
            t.extractall(extracted_dir)

    # Look for executable (likely called `dit` or `dit.exe`)
    for candidate in extracted_dir.rglob("dit*"):
        if candidate.is_file():
            try:
                candidate.chmod(0o755)
            except PermissionError:
                # On Windows the file may be locked by another process; still usable.
                pass
            return candidate

    raise RuntimeError("Could not locate extracted dit executable")


def _read_domains(input_path: Path, max_domains: int) -> List[str]:
    domains: List[str] = []
    unlimited = max_domains <= 0

    with input_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if "cruxDomain" in reader.fieldnames:
            for row in reader:
                if not unlimited and len(domains) >= max_domains:
                    break
                dom = (row.get("cruxDomain") or "").strip()
                if dom:
                    domains.append(dom)
        else:
            # Fallback: treat first column as domain
            f.seek(0)
            reader = csv.reader(f)
            for row in reader:
                if not unlimited and len(domains) >= max_domains:
                    break
                if not row:
                    continue
                dom = str(row[0]).strip()
                if dom and dom.lower() != "cruxdomain":
                    domains.append(dom)
    return domains


def _build_structured_output(
    *,
    domains: List[str],
    results: List[Dict[str, Any]],
    args: argparse.Namespace,
    dit_path: str | Path | None,
) -> Dict[str, Any]:
    structured: Dict[str, Any] = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "input": str(args.input),
        "count": len(domains),
        "ditPath": str(dit_path) if dit_path else None,
        "render": args.render,
        "timeout": args.timeout,
        "results": {},
    }

    for r in results:
        domain_key = r.get("domain") or ""
        entry = dict(r)
        stdout = entry.get("stdout")
        if isinstance(stdout, str) and stdout:
            try:
                entry["parsed_stdout"] = json.loads(stdout)
            except Exception:
                entry["parsed_stdout"] = None
        structured["results"][domain_key] = entry

    return structured


from urllib.parse import urlparse


def _ensure_url(domain: str) -> str:
    parsed = urlparse(domain)
    if parsed.scheme:
        return domain
    return f"https://{domain}"


def _run_dit(domain: str, dit_path: str, timeout: int, render: bool = False) -> Dict[str, Any]:
    """Run dit for a single domain and return structured output."""
    url = _ensure_url(domain)
    cmd = [dit_path, "run", url, "-s"]
    if render:
        cmd.append("--render")

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "domain": domain,
            "url": url,
            "success": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "domain": domain,
            "url": url,
            "success": False,
            "error": "timeout",
            "timeout": timeout,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except FileNotFoundError:
        raise


def _run_python_dit(domain: str, timeout: int) -> Dict[str, Any]:
    """A lightweight Python alternative to `dit` for basic domain checks.

    This is a best-effort fallback when `dit` is not installed.
    It performs a simple HTTPS request (then HTTP on failure) and reports status.
    """

    def _fetch(url: str) -> Dict[str, Any]:
        try:
            with urllib_request.urlopen(url, timeout=timeout) as resp:
                return {
                    "url": url,
                    "status": resp.getcode(),
                    "final_url": resp.geturl(),
                    "headers": {k: v for k, v in resp.getheaders()},
                    "content_length": resp.length,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
        except Exception as e:
            return {"error": str(e)}

    result: Dict[str, Any] = {"domain": domain, "source": "python-fallback"}

    https_url = f"https://{domain}"
    http_url = f"http://{domain}"

    https_res = _fetch(https_url)
    if https_res.get("error"):
        http_res = _fetch(http_url)
        if not http_res.get("error"):
            result.update({"result": http_res})
        else:
            result.update({"result": https_res, "fallback": http_res})
    else:
        result.update({"result": https_res})

    return result


def _stub_run(domain: str) -> Dict[str, Any]:
    """Return a placeholder result when `dit` is not available and network checks are disabled."""
    return {
        "domain": domain,
        "success": False,
        "error": "dit-not-installed",
        "message": "Install Go and run `go install github.com/happyhackingspace/dit/cmd/dit@latest` to enable real processing.",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _find_cached_dit_binary(cache_dir: Path) -> str | None:
    if not cache_dir.exists():
        return None
    for candidate in cache_dir.rglob("dit.exe"):
        if candidate.is_file():
            return str(candidate)
    for candidate in cache_dir.rglob("dit"):
        if candidate.is_file():
            return str(candidate)
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preprocess a domain list by running the 'dit' tool on each domain (if installed).",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("resources/tranco_crux_match/matched_domains.csv"),
        help="CSV file containing domains (expects 'cruxDomain' column).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("resources/tranco_crux_match/dit_output.json"),
        help="Output JSON file to store per-domain results.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=10,
        help="Number of domains to process (default: 10). Use 0 for all domains.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Timeout (seconds) for each dit invocation.",
    )
    parser.add_argument(
        "--render",
        action="store_true",
        help="Pass --render to dit (requires headless browser).",
    )
    parser.add_argument(
        "--no-sleep",
        action="store_true",
        help="Do not pause between domain checks (faster, but more aggressive).",
    )
    parser.add_argument(
        "--checkpoint-interval",
        type=int,
        default=50,
        help="Write intermediate output every N processed domains (default: 50).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of parallel workers for domain processing (default: 8).",
    )
    args = parser.parse_args(argv or sys.argv[1:])

    domains = _read_domains(args.input, args.count)

    dit_path = shutil.which("dit")
    if not dit_path:
        cache_dir = Path(__file__).resolve().parent / ".cache" / "dit"
        try:
            dit_path = _download_latest_dit_to_cache(cache_dir)
            print(f"[INFO] Downloaded dit to {dit_path}", file=sys.stderr)
        except Exception as exc:
            cached = _find_cached_dit_binary(cache_dir)
            if cached:
                dit_path = cached
                print(f"[WARN] Download/update failed ({exc}); using cached dit at {cached}", file=sys.stderr)
            else:
                print(
                    "[WARN] 'dit' not found on PATH and download failed; falling back to python-based check.",
                    file=sys.stderr,
                )
                print(f"[WARN] {exc}", file=sys.stderr)

    results: List[Dict[str, Any]] = []
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def _process_domain(domain: str) -> Dict[str, Any]:
        if dit_path:
            try:
                res = _run_dit(domain, dit_path, args.timeout, render=args.render)
            except FileNotFoundError:
                # Dit disappeared between check and run.
                res = _run_python_dit(domain, args.timeout)
        else:
            res = _run_python_dit(domain, args.timeout)
        if not args.no_sleep:
            time.sleep(0.01)
        return res

    # Preserve input order while processing in parallel.
    ordered_results: list[Dict[str, Any] | None] = [None] * len(domains)
    completed = 0
    max_workers = max(1, args.workers)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_process_domain, domain): idx
            for idx, domain in enumerate(domains)
        }

        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                ordered_results[idx] = future.result()
            except Exception as exc:  # defensive fallback
                ordered_results[idx] = {
                    "domain": domains[idx],
                    "url": _ensure_url(domains[idx]),
                    "success": False,
                    "error": f"worker-error: {exc}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

            completed += 1

            if args.checkpoint_interval > 0 and completed % args.checkpoint_interval == 0:
                snapshot = [r for r in ordered_results if r is not None]
                checkpoint = _build_structured_output(
                    domains=domains,
                    results=snapshot,
                    args=args,
                    dit_path=dit_path,
                )
                args.output.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
                print(f"[CHK] {completed}/{len(domains)} written to {args.output}")

    results = [r for r in ordered_results if r is not None]

    structured = _build_structured_output(
        domains=domains,
        results=results,
        args=args,
        dit_path=dit_path,
    )
    args.output.write_text(json.dumps(structured, indent=2), encoding="utf-8")

    print(f"Wrote {len(results)} entries to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
