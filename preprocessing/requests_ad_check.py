import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
import urllib3

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from Helpers.easylist_selectors import load_selectors
from Helpers.website_categorization import WebsiteCategorizer


_SIMPLE_ID_RE = re.compile(r"^#([A-Za-z0-9_-]+)$")
_SIMPLE_CLASS_RE = re.compile(r"^\.([A-Za-z0-9_-]+)$")
_ATTR_SELECTOR_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9_-]*)?\[([A-Za-z0-9_:-]+)\s*([\^\$\*\|~]?=)\s*['\"]?([^'\"\]]+)['\"]?\]$"
)

_ID_ATTR_RE = re.compile(r"id\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_CLASS_ATTR_RE = re.compile(r"class\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_ALL_ATTRS_RE = re.compile(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*['\"]([^'\"]+)['\"]")


@dataclass
class PageSignals:
    ids: set[str]
    classes: set[str]
    attrs: dict[str, list[str]]


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        return f"https://{url}"
    return url


def reduce_to_origin(url: str) -> str:
    parsed = urlparse(url or "")
    scheme = parsed.scheme or "https"
    host = (parsed.netloc or parsed.path).split("/")[0]
    if not host:
        return ""
    return f"{scheme}://{host.lower()}"


def _default_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 6.0) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/45.0.2454.85 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
    }


def fetch_html(url: str, timeout: float, verify_ssl: bool) -> requests.Response:
    headers = _default_headers()
    response = requests.get(
        url,
        headers=headers,
        timeout=timeout,
        allow_redirects=True,
        verify=verify_ssl,
    )

    if response.status_code >= 400 and not response.text.strip():
        response.raise_for_status()
    return response


def extract_signals(html: str) -> PageSignals:
    ids = {value.strip().lower() for value in _ID_ATTR_RE.findall(html) if value.strip()}

    classes: set[str] = set()
    for class_value in _CLASS_ATTR_RE.findall(html):
        for cls in class_value.split():
            cls = cls.strip().lower()
            if cls:
                classes.add(cls)

    attrs: dict[str, list[str]] = {}
    for name, value in _ALL_ATTRS_RE.findall(html):
        key = name.lower()
        attrs.setdefault(key, []).append(value.lower())

    return PageSignals(ids=ids, classes=classes, attrs=attrs)


def _attr_match(values: Iterable[str], operator: str, needle: str) -> bool:
    needle = needle.lower()
    for value in values:
        if operator == "=" and value == needle:
            return True
        if operator == "*=" and needle in value:
            return True
        if operator == "^=" and value.startswith(needle):
            return True
        if operator == "$=" and value.endswith(needle):
            return True
        if operator == "~=" and needle in value.split():
            return True
        if operator == "|=" and (value == needle or value.startswith(f"{needle}-")):
            return True
    return False


def selector_matches(selector: str, signals: PageSignals) -> bool:
    selector = selector.strip()
    if not selector:
        return False

    id_match = _SIMPLE_ID_RE.match(selector)
    if id_match:
        return id_match.group(1).lower() in signals.ids

    class_match = _SIMPLE_CLASS_RE.match(selector)
    if class_match:
        return class_match.group(1).lower() in signals.classes

    attr_match = _ATTR_SELECTOR_RE.match(selector)
    if attr_match:
        attr_name, operator, needle = attr_match.groups()
        values = signals.attrs.get(attr_name.lower(), [])
        if not values:
            return False
        return _attr_match(values, operator, needle)

    # Fallback for complex selectors: check direct occurrence in HTML attributes.
    # This keeps the script requests-only while still catching many useful patterns.
    token = re.sub(r"[^a-z0-9_-]+", "", selector.lower())
    if len(token) >= 4:
        for values in signals.attrs.values():
            if any(token in value for value in values):
                return True

    return False


def _check_ads_txt(url: str, timeout: float, verify_ssl: bool) -> int:
    """Return 1 if <url>/ads.txt exists and contains valid uncommented entries.
    Return -1 if it exists but is empty/has no valid uncommented entries.
    Return 0 if it doesn't exist or returns non-200/429.

    Some hosts return 200 for the domain root or other pages even when you request /ads.txt.
    This function ensures the final request URL ends in "/ads.txt".
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return 0
        ads_url = f"{parsed.scheme}://{parsed.netloc}/ads.txt"
        # Use a browser-like UA to reduce the chance of being blocked/rate-limited.
        resp = requests.get(
            ads_url,
            headers=_default_headers(),
            timeout=timeout,
            verify=verify_ssl,
            allow_redirects=True,
        )

        # Treat 429 as a reasonable indicator that /ads.txt exists but the host is rate-limiting us.
        if resp.status_code == 429:
            return 1

        if resp.status_code != 200:
            return 0

        # Ensure the response is actually text and not an HTML error/catch-all page disguised as 200
        content_type = resp.headers.get("Content-Type", "").lower()
        if "text/html" in content_type:
            return 0

        text = resp.text.strip()
        if not text:
            return -1

        # Ensure the final URL looks like an ads.txt resource (accept query params and trailing slash).
        final = urlparse(resp.url)
        final_path = final.path.rstrip("/")
        if not final_path.lower().endswith("/ads.txt"):
            return 0

        # Some sites return a 200 OK with HTML content but missing the content-type header.
        # Check if the text starts with obvious HTML tags.
        if text.startswith("<!DOCTYPE") or text.startswith("<html") or text.startswith("<head"):
            return 0

        import re
        lines = text.splitlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            
            # Match strictly: domain, ID, connection [, optional certification authority ID]
            # e.g., saambaa.com, 72000661, DIRECT
            # e.g., themediagrid.com, 9UX1JV, DIRECT, 35d5010d7789b49d
            if re.match(r'^[^,]+,\s*[^,]+,\s*(DIRECT|RESELLER)(?:,\s*[^,]+)?', line, re.IGNORECASE):
                return 1

        return -1
    except Exception:
        return 0


def detect_ads(
    url: str,
    selectors: list[str],
    timeout: float,
    verify_ssl: bool,
    max_evidence: int = 25,
) -> dict:
    normalized = normalize_url(url)
    response = fetch_html(normalized, timeout, verify_ssl=verify_ssl)
    signals = extract_signals(response.text)

    matched: list[str] = []
    for selector in selectors:
        if selector_matches(selector, signals):
            matched.append(selector)
            break

    final_origin = reduce_to_origin(response.url)

    result = {
        "url": normalized,
        "finalUrl": final_origin,
        "statusCode": response.status_code,
        "adsTxt": 0,
        "adLikely": len(matched) > 0,
        "matchedSelectorCount": len(matched),
        "matchedSelectors": matched,
        "signals": {
            "idCount": len(signals.ids),
            "classCount": len(signals.classes),
            "attrNameCount": len(signals.attrs),
        },
    }

    # Always check ads.txt (even when selector evidence already exists).
    # It is recorded separately via the "adsTxt" signal.
    checked_urls = []

    final_url = result.get("finalUrl")
    if final_url:
        checked_urls.append(final_url)

    if normalized and normalized != final_url:
        checked_urls.append(normalized)

    for candidate in checked_urls:
        status: int = _check_ads_txt(candidate, timeout=timeout, verify_ssl=verify_ssl)
        if status != 0:
            result["adsTxt"] = status
            # Keep adLikely independent; it reflects selector-based evidence only.
            break

    return result


def _pick_domain_column(fieldnames: list[str], requested: str | None) -> str:
    if requested:
        if requested not in fieldnames:
            raise ValueError(
                f"Requested domain column '{requested}' not found. Available: {fieldnames}"
            )
        return requested

    for candidate in ["trancoDomain", "domain", "url", "host", "cruxDomain"]:
        if candidate in fieldnames:
            return candidate

    raise ValueError(f"Could not auto-detect domain column. Available: {fieldnames}")


def _ensure_output_schema(output_csv: Path, out_fields: list[str]) -> bool:
    """Ensure output CSV has the expected header.

    Returns True when caller should append, False when caller should write a new file.
    """
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    if not output_csv.is_file() or output_csv.stat().st_size == 0:
        return False

    try:
        with output_csv.open("r", encoding="utf-8", newline="") as infile:
            reader = csv.DictReader(infile)
            existing_fields = [str(f).strip() for f in (reader.fieldnames or [])]
            rows = list(reader)

        if existing_fields == out_fields:
            return True

        # Migrate older files (without category column) into the new schema.
        with output_csv.open("w", encoding="utf-8", newline="") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=out_fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in out_fields})
            outfile.flush()

        return True
    except Exception:
        # If schema check fails, fall back to writing a fresh file.
        return False


def _process_reader(
    workers: int,
    reader: csv.DictReader,
    selectors: list[str],
    timeout: float,
    verify_ssl: bool,
    resolved_domain_col: str,
    limit: int,
    target: int,
    target2: int,
    output_csv: Path,
    existing_domains: set[str],
    start_index: int,
    category_resolver: WebsiteCategorizer | None,
    categorize_all: bool,
) -> tuple[int, int]:
    processed = 0
    failed = 0
    next_index = start_index

    # Collect domains to process until we reach the desired limit.
    candidates: list[str] = []
    for row in reader:
        if limit > 0 and len(candidates) >= limit:
            break
        domain = (row.get(resolved_domain_col) or "").strip()
        if domain and domain not in existing_domains:
            candidates.append(domain)

    print(
        f"Queued {len(candidates)} domains for concurrent check (workers={workers}).",
        flush=True,
    )
    print(f"Output CSV file: {output_csv}", flush=True)

    # Threaded processing
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _process_one(domain: str) -> dict:
        try:
            return detect_ads(
                url=domain,
                selectors=selectors,
                timeout=timeout,
                verify_ssl=verify_ssl,
            )
        except Exception as exc:
            return {
                "url": domain,
                "finalUrl": "",
                "statusCode": "",
                "adLikely": False,
                "matchedSelectorCount": 0,
                "error": str(exc),
            }

    # Write results incrementally to final CSV (preserving input order as soon as possible).
    out_fields = [
        "index",
        "inputDomain",
        "category",
        "finalUrl",
        "statusCode",
        "adsTxt",
        "adLikely",
        "matchedSelectorCount",
        "matchedSelectors",
    ]
    append_mode = _ensure_output_schema(output_csv, out_fields)
    mode = "a" if append_mode else "w"

    with output_csv.open(mode, encoding="utf-8", newline="") as outfile:
        writer = csv.DictWriter(outfile, fieldnames=out_fields)
        if not append_mode:
            writer.writeheader()
            outfile.flush()

        ad_likely_count = 0
        target2_count = 0

        results_by_position: dict[int, dict] = {}
        next_to_write = 0

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_pos = {
                executor.submit(_process_one, domain): idx
                for idx, domain in enumerate(candidates)
            }
            completed_futures = 0
            total_futures = len(future_to_pos)
            early_stop = False

            for future in as_completed(future_to_pos):
                pos = future_to_pos[future]
                dom = candidates[pos]
                try:
                    results_by_position[pos] = future.result()
                except Exception as exc:
                    results_by_position[pos] = {
                        "url": dom,
                        "finalUrl": "",
                        "statusCode": "",
                        "adsTxt": 0,
                        "adLikely": False,
                        "matchedSelectorCount": 0,
                        "matchedSelectors": [],
                        "error": str(exc),
                    }

                completed_futures += 1
                if completed_futures % 25 == 0 or completed_futures == total_futures:
                    print(
                        f"Completed network checks: {completed_futures}/{total_futures}",
                        flush=True,
                    )

                while next_to_write in results_by_position:
                    next_result = results_by_position.pop(next_to_write)
                    dom_to_write = candidates[next_to_write]
                    category = ""
                    if category_resolver:
                        needs_category = categorize_all or bool(next_result.get("adLikely")) or int(next_result.get("adsTxt", 0)) == 1
                        if needs_category:
                            category = category_resolver.get_category(dom_to_write)

                    writer.writerow(
                        {
                            "index": next_index,
                            "inputDomain": dom_to_write,
                            "category": category,
                            "finalUrl": next_result.get("finalUrl", ""),
                            "statusCode": next_result.get("statusCode", ""),
                            "adsTxt": next_result.get("adsTxt", 0),
                            "adLikely": next_result.get("adLikely", False),
                            "matchedSelectorCount": next_result.get("matchedSelectorCount", 0),
                            "matchedSelectors": json.dumps(next_result.get("matchedSelectors", []), ensure_ascii=False),
                        }
                    )
                    outfile.flush()

                    if next_result.get("error"):
                        failed += 1
                    if next_result.get("adLikely"):
                        ad_likely_count += 1
                    if next_result.get("adLikely") or next_result.get("adsTxt") == 1:
                        target2_count += 1

                    next_index += 1
                    next_to_write += 1
                    processed += 1

                    if processed % 5 == 0:
                        print(f"Processed {processed} new rows...", flush=True)

                    if target > 0 and ad_likely_count >= target:
                        print(f"Reached target of {target} adLikely=true rows; stopping.", flush=True)
                        early_stop = True
                        break
                    if target2 > 0 and target2_count >= target2:
                        print(f"Reached target2 of {target2} (adLikely=True OR adsTxt=1) rows; stopping.", flush=True)
                        early_stop = True
                        break

                if early_stop:
                    break

            if early_stop:
                for f in future_to_pos:
                    if not f.done():
                        f.cancel()

    return processed, failed


def _process_reader_no_header(
    workers: int,
    reader: csv.reader,
    selectors: list[str],
    timeout: float,
    verify_ssl: bool,
    limit: int,
    target: int,
    target2: int,
    output_csv: Path,
    existing_domains: set[str],
    start_index: int,
    category_resolver: WebsiteCategorizer | None,
    categorize_all: bool,
) -> tuple[int, int]:
    processed = 0
    failed = 0
    next_index = start_index

    # Collect candidates first so we can process with threads and show queue progress.
    candidates: list[str] = []
    for row in reader:
        if limit > 0 and len(candidates) >= limit:
            break

        domain = ""
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            domain = str(row[1]).strip()
        elif isinstance(row, (list, tuple)) and len(row) == 1:
            domain = str(row[0]).strip()

        if not domain:
            failed += 1
            continue

        if domain in existing_domains:
            continue

        candidates.append(domain)

    print(
        f"Queued {len(candidates)} domains for concurrent check (workers={workers}).",
        flush=True,
    )
    print(f"Output CSV file: {output_csv}", flush=True)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _process_one(domain: str) -> dict:
        try:
            return detect_ads(
                url=domain,
                selectors=selectors,
                timeout=timeout,
                verify_ssl=verify_ssl,
            )
        except Exception as exc:
            return {
                "url": domain,
                "finalUrl": "",
                "statusCode": "",
                "adsTxt": 0,
                "adLikely": False,
                "matchedSelectorCount": 0,
                "matchedSelectors": [],
                "error": str(exc),
            }

    out_fields = [
        "index",
        "inputDomain",
        "category",
        "finalUrl",
        "statusCode",
        "adsTxt",
        "adLikely",
        "matchedSelectorCount",
        "matchedSelectors",
    ]
    append_mode = _ensure_output_schema(output_csv, out_fields)
    mode = "a" if append_mode else "w"
    with output_csv.open(mode, encoding="utf-8", newline="") as outfile:
        writer = csv.DictWriter(outfile, fieldnames=out_fields)
        if not append_mode:
            writer.writeheader()
            outfile.flush()

        ad_likely_count = 0
        target2_count = 0

        results_by_position: dict[int, dict] = {}
        next_to_write = 0

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_pos = {
                executor.submit(_process_one, domain): idx
                for idx, domain in enumerate(candidates)
            }
            completed_futures = 0
            total_futures = len(future_to_pos)
            early_stop = False

            for future in as_completed(future_to_pos):
                pos = future_to_pos[future]
                dom = candidates[pos]
                try:
                    results_by_position[pos] = future.result()
                except Exception as exc:
                    results_by_position[pos] = {
                        "url": dom,
                        "finalUrl": "",
                        "statusCode": "",
                        "adsTxt": 0,
                        "adLikely": False,
                        "matchedSelectorCount": 0,
                        "matchedSelectors": [],
                        "error": str(exc),
                    }

                completed_futures += 1
                if completed_futures % 25 == 0 or completed_futures == total_futures:
                    print(
                        f"Completed network checks: {completed_futures}/{total_futures}",
                        flush=True,
                    )

                while next_to_write in results_by_position:
                    next_result = results_by_position.pop(next_to_write)
                    dom_to_write = candidates[next_to_write]
                    category = ""
                    if category_resolver:
                        needs_category = categorize_all or bool(next_result.get("adLikely")) or int(next_result.get("adsTxt", 0)) == 1
                        if needs_category:
                            category = category_resolver.get_category(dom_to_write)

                    writer.writerow(
                        {
                            "index": next_index,
                            "inputDomain": dom_to_write,
                            "category": category,
                            "finalUrl": next_result.get("finalUrl", ""),
                            "statusCode": next_result.get("statusCode", ""),
                            "adsTxt": next_result.get("adsTxt", 0),
                            "adLikely": next_result.get("adLikely", False),
                            "matchedSelectorCount": next_result.get("matchedSelectorCount", 0),
                            "matchedSelectors": json.dumps(next_result.get("matchedSelectors", []), ensure_ascii=False),
                        }
                    )
                    outfile.flush()

                    if next_result.get("error"):
                        failed += 1
                    if next_result.get("adLikely"):
                        ad_likely_count += 1
                    if next_result.get("adLikely") or next_result.get("adsTxt") == 1:
                        target2_count += 1

                    next_index += 1
                    next_to_write += 1
                    processed += 1

                    if processed % 5 == 0:
                        print(f"Processed {processed} new rows...", flush=True)

                    if target > 0 and ad_likely_count >= target:
                        print(f"Reached target of {target} adLikely=true rows; stopping.", flush=True)
                        early_stop = True
                        break
                    if target2 > 0 and target2_count >= target2:
                        print(f"Reached target2 of {target2} (adLikely=True OR adsTxt=1) rows; stopping.", flush=True)
                        early_stop = True
                        break

                if early_stop:
                    break

            if early_stop:
                for f in future_to_pos:
                    if not f.done():
                        f.cancel()

    return processed, failed


def run_csv_batch(
    input_csv: Path,
    output_csv: Path,
    selectors: list[str],
    timeout: float,
    verify_ssl: bool,
    domain_column: str | None,
    limit: int,
    target: int,
    target2: int,
    workers: int,
    category_resolver: WebsiteCategorizer | None,
    categorize_all: bool,
) -> tuple[int, int]:
    if not input_csv.is_file():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")

    # If the output file already exists, treat it as a checkpoint and append new rows.
    existing_domains: set[str] = set()
    next_index = 1
    if output_csv.is_file():
        try:
            import csv as _csv
            with output_csv.open("r", encoding="utf-8", newline="") as f:
                reader = _csv.DictReader(f)
                max_idx = 0
                for row in reader:
                    dom = (row.get("inputDomain") or "").strip()
                    if dom:
                        existing_domains.add(dom)
                    try:
                        idx = int(row.get("index") or 0)
                        if idx > max_idx:
                            max_idx = idx
                    except Exception:
                        pass
                next_index = max_idx + 1
        except Exception:
            # If the file can't be read, just start fresh
            existing_domains = set()
            next_index = 1

    if input_csv.suffix.lower() == ".zip":
        import io
        import zipfile

        with zipfile.ZipFile(input_csv, "r") as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise ValueError(f"No .csv file found in zip: {input_csv}")
            # Use the first CSV entry by default
            with z.open(names[0], "r") as f:
                infile = io.TextIOWrapper(f, encoding="utf-8")
                reader = csv.DictReader(infile)
                if not reader.fieldnames:
                    raise ValueError(f"CSV has no header: {names[0]}")
                resolved_domain_col = _pick_domain_column(reader.fieldnames, domain_column)
                return _process_reader(
                    workers,
                    reader,
                    selectors,
                    timeout,
                    verify_ssl,
                    resolved_domain_col,
                    limit,
                    target,
                    target2,
                    output_csv,
                    existing_domains,
                    next_index,
                    category_resolver,
                    categorize_all,
                )

    with input_csv.open("r", encoding="utf-8", newline="") as infile:
        # Peek at the first line to decide if this is a headered CSV or a plain rank+domain list.
        first_line = infile.readline()
        infile.seek(0)

        # If the first line looks like a numeric rank followed by a domain, treat it as a no-header list.
        first_row = next(csv.reader([first_line])) if first_line else []
        if len(first_row) >= 2 and first_row[0].isdigit() and "." in first_row[1]:
            reader = csv.reader(infile)
            return _process_reader_no_header(
                workers,
                reader,
                selectors,
                timeout,
                verify_ssl,
                limit,
                target,
                target2,
                output_csv,
                existing_domains,
                next_index,
                category_resolver,
                categorize_all,
            )

        # Otherwise, treat it as a normal headered CSV and use DictReader.
        reader = csv.DictReader(infile)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header: {input_csv}")
        resolved_domain_col = _pick_domain_column(reader.fieldnames, domain_column)
        return _process_reader(
            workers,
            reader,
            selectors,
            timeout,
            verify_ssl,
            resolved_domain_col,
            limit,
            target,
            target2,
            output_csv,
            existing_domains,
            next_index,
            category_resolver,
            categorize_all,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check whether a website is ad-likely using EasyList selectors and requests only."
    )
    parser.add_argument("url", nargs="?", help="Website URL or domain (e.g., example.com)")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("resources/adLikelyUrls.csv"),
        help="Optional JSON output path. Defaults to resources/adLikelyUrls.csv.",
    )
    parser.add_argument(
        "--max-selectors",
        type=int,
        default=0,
        help="Optional cap on selectors loaded from easylist (0 means all).",
    )
    parser.add_argument("--input-csv", type=Path, default=None, help="Input CSV path")
    parser.add_argument(
        "--domain-column",
        type=str,
        default=None,
        help="Domain column name in input CSV (auto-detected when omitted).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max number of rows to process in CSV mode (0 means all).",
    )
    parser.add_argument(
        "--target",
        type=int,
        default=0,
        help="Stop early when this many rows have adLikely=True (0 disables).",
    )
    parser.add_argument(
        "--target2",
        type=int,
        default=0,
        help="Stop early when this many rows have adLikely=True OR adsTxt=1 (0 disables).",
    )
    parser.add_argument(
        "--csv-output",
        type=Path,
        default=None,
        help="Output CSV path for CSV batch mode.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=50,
        help="Number of concurrent threads.",
    )
    parser.add_argument(
        "--disable-category",
        action="store_true",
        help="Disable browser category lookup for CSV rows.",
    )
    parser.add_argument(
        "--categorize-all",
        action="store_true",
        help="Lookup category for every processed domain (default only adLikely=True or adsTxt=1 rows).",
    )
    parser.add_argument(
        "--category-timeout-ms",
        type=int,
        default=8000,
        help="Per-domain timeout for category lookup in milliseconds.",
    )
    parser.add_argument(
        "--category-cache",
        type=Path,
        default=Path("resources/domain_categories_cache.json"),
        help="Cache file for domain-to-category mappings.",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Show browser window during category lookup (headless by default).",
    )
    args = parser.parse_args()

    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    selectors = load_selectors()
    if args.max_selectors and args.max_selectors > 0:
        selectors = selectors[: args.max_selectors]

    if args.input_csv:
        output_csv = args.csv_output or Path("resources/tranco_crux_match/requests_ad_check_results.csv")
        print(f"Using input CSV: {args.input_csv}", flush=True)
        print(f"Using output CSV: {output_csv}", flush=True)
        category_resolver: WebsiteCategorizer | None = None
        if not args.disable_category:
            category_resolver = WebsiteCategorizer(
                enabled=True,
                headless=not args.show_browser,
                timeout_ms=max(1000, int(args.category_timeout_ms or 8000)),
                cache_path=args.category_cache,
            )
        try:
            processed, failed = run_csv_batch(
                input_csv=args.input_csv,
                output_csv=output_csv,
                selectors=selectors,
                timeout=args.timeout,
                verify_ssl=not args.insecure,
                domain_column=args.domain_column,
                limit=args.limit,
                target=args.target,
                target2=args.target2,
                workers=max(1, int(args.workers or 1)),
                category_resolver=category_resolver,
                categorize_all=bool(args.categorize_all),
            )
        finally:
            if category_resolver:
                category_resolver.close()
        print(
            f"CSV run complete. processed={processed}, failed={failed}, output={output_csv}",
            flush=True,
        )
        return

    def run_once(url: str) -> str:
        try:
            result = detect_ads(
                url=url,
                selectors=selectors,
                timeout=args.timeout,
                verify_ssl=not args.insecure,
            )
        except requests.RequestException as exc:
            result = {
                "url": normalize_url(url),
                "adLikely": False,
                "error": f"request_failed: {exc}",
            }
        return json.dumps(result, indent=2)

    if args.url:
        payload = run_once(args.url)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
            print(f"Wrote result to {args.output}")
        else:
            print(payload)
        return

    print("Interactive mode. Enter a URL/domain and press Enter.")
    print("Type 'exit', 'quit', or 'q' to stop.")

    while True:
        raw = input("url> ").strip()
        if not raw:
            continue
        if raw.lower() in {"exit", "quit", "q"}:
            print("Exiting.")
            break

        payload = run_once(raw)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
            print(f"Wrote result to {args.output}")
        else:
            print(payload)


if __name__ == "__main__":
    main()
