"""Download Tranco and match it against a CrUX list."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import sys
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_TRANCO_ZIP_URL = "https://tranco-list.eu/top-1m.csv.zip"
DEFAULT_CRUX_URL = "https://raw.githubusercontent.com/zakird/crux-top-lists/main/data/global/current.csv.gz"
DEFAULT_OUTPUT_ROOT = Path("resources")
DEFAULT_OUTPUT_NAME = "tranco_crux_match"
PREFERRED_CRUX_COLUMNS = ["origin", "url", "domain", "hostname", "host", "site"]


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _download_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "adgraph-tranco-helper/1.0",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def _normalize_domain(value: str) -> str | None:
    if not value:
        return None
    raw = value.strip().lower()
    if not raw:
        return None

    if raw.startswith(("http://", "https://")):
        hostname = urlparse(raw).hostname or ""
    else:
        candidate = raw.split(",", 1)[0].strip()
        candidate = re.sub(r"^\*\.", "", candidate)
        if "/" in candidate:
            hostname = urlparse(f"https://{candidate}").hostname or ""
        else:
            hostname = candidate

    hostname = hostname.strip(".")
    if not hostname:
        return None
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname or None


def _read_tranco_rows(zip_bytes: bytes) -> list[dict[str, object]]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = archive.namelist()
        if not names:
            raise ValueError("Downloaded Tranco archive is empty")
        with archive.open(names[0], "r") as handle:
            text_stream = io.TextIOWrapper(handle, encoding="utf-8", newline="")
            reader = csv.reader(text_stream)
            rows: list[dict[str, object]] = []
            for row in reader:
                if len(row) < 2:
                    continue
                rank_raw, domain_raw = row[0].strip(), row[1].strip()
                normalized_domain = _normalize_domain(domain_raw)
                if not rank_raw or not normalized_domain:
                    continue
                rows.append(
                    {
                        "rank": int(rank_raw),
                        "domain": normalized_domain,
                        "trancoDomain": domain_raw,
                    }
                )
            return rows


def _detect_crux_column(fieldnames: list[str]) -> str:
    lowered = {name.lower(): name for name in fieldnames}
    for candidate in PREFERRED_CRUX_COLUMNS:
        if candidate in lowered:
            return lowered[candidate]
    return fieldnames[0]


def _read_crux_rows(path: Path) -> list[dict[str, object]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return _read_crux_json(data)

    open_handle = None
    try:
        if suffix in {".txt", ".lst"}:
            open_handle = path.open("r", encoding="utf-8", newline="")
        elif suffix in {".gz", ".csv.gz", ".txt.gz"}:
            open_handle = io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8")
        else:
            open_handle = path.open("r", encoding="utf-8", newline="")

        sample = open_handle.read(4096)
        open_handle.seek(0)

        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(open_handle, dialect=dialect)
        if not reader.fieldnames:
            raise ValueError("CrUX file has no header row")
        column = _detect_crux_column(reader.fieldnames)

        rows = []
        for index, row in enumerate(reader, start=2):
            source_value = (row.get(column) or "").strip()
            normalized = _normalize_domain(source_value)
            if normalized:
                rows.append(
                    {
                        "cruxDomain": normalized,
                        "sourceValue": source_value,
                        "sourceLine": index,
                    }
                )
        return rows
    finally:
        if open_handle is not None:
            open_handle.close()


def _read_crux_json(data: object) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if isinstance(data, list):
        for index, item in enumerate(data, start=1):
            if isinstance(item, str):
                normalized = _normalize_domain(item)
                if normalized:
                    rows.append(
                        {
                            "cruxDomain": normalized,
                            "sourceValue": item,
                            "sourceLine": index,
                        }
                    )
            elif isinstance(item, dict):
                keys = list(item.keys())
                if not keys:
                    continue
                column = _detect_crux_column(keys)
                source_value = str(item.get(column, ""))
                normalized = _normalize_domain(source_value)
                if normalized:
                    rows.append(
                        {
                            "cruxDomain": normalized,
                            "sourceValue": source_value,
                            "sourceLine": index,
                        }
                    )
        return rows
    raise ValueError("CrUX JSON must contain a list of strings or objects")


def _download_crux_if_needed(crux_source: str, output_dir: Path) -> Path:
    candidate = Path(crux_source)
    if candidate.exists():
        return candidate

    if crux_source.startswith(("http://", "https://")):
        target = output_dir / f"crux_source_{_timestamp()}{Path(urlparse(crux_source).path).suffix or '.csv'}"
        target.write_bytes(_download_bytes(crux_source))
        return target

    raise FileNotFoundError(f"CrUX source not found: {crux_source}")


def _write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run_match(crux_source: str, output_root: Path, tranco_url: str) -> Path:
    output_dir = output_root / DEFAULT_OUTPUT_NAME
    output_dir.mkdir(parents=True, exist_ok=True)

    tranco_zip_bytes = _download_bytes(tranco_url)
    tranco_zip_path = output_dir / "tranco_top_1m.csv.zip"
    tranco_zip_path.write_bytes(tranco_zip_bytes)

    tranco_rows = _read_tranco_rows(tranco_zip_bytes)

    crux_path = _download_crux_if_needed(crux_source, output_dir)
    crux_rows = _read_crux_rows(crux_path)

    # Build a map from normalized CrUX domain -> list of CrUX rows for lookups.
    crux_map: dict[str, list[dict[str, object]]] = {}
    seen_crux_domains: set[str] = set()
    for row in crux_rows:
        crux_domain = row.get("cruxDomain")
        if not isinstance(crux_domain, str):
            continue
        seen_crux_domains.add(crux_domain)
        crux_map.setdefault(crux_domain, []).append(row)

    crux_url: str | None = crux_source if isinstance(crux_source, str) and crux_source.startswith(("http://", "https://")) else None

    # Produce matches in the order domains appear in the Tranco list.
    matched_rows: list[dict[str, object]] = []
    for tranco_row in tranco_rows:
        domain = tranco_row["domain"]
        crux_rows_for_domain = crux_map.get(domain)
        if not crux_rows_for_domain:
            continue
        for crux_row in crux_rows_for_domain:
            matched_rows.append(
                {
                    "cruxDomain": domain,
                    "trancoRank": tranco_row["rank"],
                    "trancoDomain": tranco_row["trancoDomain"],
                    "sourceValue": crux_row.get("sourceValue"),
                    "sourceLine": crux_row.get("sourceLine"),
                }
            )

    # Deduplicate on trancoRank (keep first match per rank).
    seen_ranks: set[int] = set()
    filtered_rows: list[dict[str, object]] = []
    for row in matched_rows:
        rank = row.get("trancoRank")
        try:
            rank_int = int(rank)
        except Exception:
            # If rank can't be parsed, keep the row.
            filtered_rows.append(row)
            continue
        if rank_int in seen_ranks:
            continue
        seen_ranks.add(rank_int)
        filtered_rows.append(row)

    matched_csv = output_dir / "matched_domains.csv"
    summary_json = output_dir / "summary.json"

    _write_csv(
        matched_csv,
        filtered_rows,
        ["cruxDomain", "trancoRank", "trancoDomain", "sourceValue", "sourceLine"],
    )

    # Record a run for audit/ledger purposes.
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    def _file_hash(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return h.hexdigest()

    matched_hash = _file_hash(matched_csv)
    matched_mtime = datetime.fromtimestamp(matched_csv.stat().st_mtime, UTC).isoformat()

    run_entry = {
        "runAt": now_iso,
        "trancoUrl": tranco_url,
        "trancoArchive": str(tranco_zip_path),
        "cruxUrl": crux_url,
        "cruxSource": str(crux_path),
        "nTrancoDomains": len(tranco_rows),
        "nCruxRows": len(crux_rows),
        "nUniqueCruxDomains": len(seen_crux_domains),
        "nMatchedDomains": len(filtered_rows),
        "matchedDomainsHash": matched_hash,
        "matchedDomainsMtime": matched_mtime,
    }

    existing = {}
    if summary_json.exists():
        try:
            existing = json.loads(summary_json.read_text(encoding="utf-8")) or {}
        except Exception:
            existing = {}

    runs = existing.get("runs", [])
    if not isinstance(runs, list):
        runs = []
    runs.append(run_entry)

    summary = {
        "generatedAt": now_iso,
        "outputDir": str(output_dir),
        "runs": runs,
    }

    summary_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return output_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download the latest Tranco list and a CrUX domain list, match them, and store the outputs in resources/tranco_crux_match/."
    )
    parser.add_argument(
        "--crux-source",
        default=DEFAULT_CRUX_URL,
        help=(
            "Path or URL to a CrUX list file (.csv, .tsv, .txt, or .json). "
            f"If omitted, downloads from {DEFAULT_CRUX_URL}."
        ),
    )
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="Directory where the tranco_crux_match folder will be created (default: resources).",
    )
    parser.add_argument(
        "--tranco-url",
        default=DEFAULT_TRANCO_ZIP_URL,
        help="Tranco archive URL to download (default: latest top-1m zip).",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        output_dir = run_match(
            crux_source=args.crux_source,
            output_root=Path(args.output_root),
            tranco_url=args.tranco_url,
        )
    except Exception as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        return 1

    print(f"[OK] Wrote Tranco/CrUX match outputs to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())