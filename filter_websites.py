#!/usr/bin/env python3
"""filter_websites.py

Filters a website CSV file (such as urls/c-websites.csv) to only include rows
with HTTP 2XX status codes (200-299), with a configurable limit on how many
websites to store in the output CSV.

By default, the output CSV contains only a single 'url' column, ready for crawlers.

Usage examples:
    python filter_websites.py urls/c-websites.csv -o urls/run.csv --limit 1000
    python filter_websites.py -i urls/c-websites.csv -o urls/c-websites-2xx.csv -l 50
    python filter_websites.py --help
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any


def is_2xx_status(value: Any) -> bool:
    """Return True if the value represents an HTTP 2XX status code (200-299)."""
    if value is None:
        return False
    val_str = str(value).strip()
    if not val_str:
        return False
    try:
        # Handles "200", "204", "200.0", etc.
        code = int(float(val_str))
        return 200 <= code <= 299
    except (ValueError, TypeError):
        return False


def find_status_column(fieldnames: list[str]) -> str | None:
    """Detect the column name containing the HTTP status code."""
    candidates = [
        "statusCode",
        "status_code",
        "status",
        "http_status",
        "http_status_code",
        "code",
    ]
    name_map = {name.lower(): name for name in fieldnames}
    for cand in candidates:
        if cand.lower() in name_map:
            return name_map[cand.lower()]
    return None


def find_url_column(fieldnames: list[str]) -> str | None:
    """Detect the column name containing the website URL or domain."""
    candidates = [
        "finalUrl",
        "final_url",
        "url",
        "inputDomain",
        "domain",
        "website",
        "site",
    ]
    name_map = {name.lower(): name for name in fieldnames}
    for cand in candidates:
        if cand.lower() in name_map:
            return name_map[cand.lower()]
    return None


def extract_url(row: dict[str, Any], url_col: str | None = None) -> str:
    """Extract a clean, valid URL from a row."""
    val = ""
    if url_col and row.get(url_col):
        val = str(row.get(url_col) or "").strip()

    if not val:
        for fallback in ["finalUrl", "final_url", "url", "inputDomain", "domain", "website", "site"]:
            if row.get(fallback):
                candidate = str(row.get(fallback) or "").strip()
                if candidate:
                    val = candidate
                    break

    if not val:
        return ""

    if not (val.startswith("http://") or val.startswith("https://")):
        val = f"https://{val}"

    return val


def filter_2xx_websites(
    input_path: Path | str,
    output_path: Path | str,
    limit: int | None = None,
    status_col: str | None = None,
    url_col: str | None = None,
    only_url_column: bool = True,
) -> int:
    """Filter rows with 2XX status codes from input_path and write up to `limit` rows to output_path.

    If `only_url_column` is True (default), output has only a single 'url' column.
    Returns the count of matching rows written.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows_written = 0
    total_rows = 0

    with input_path.open("r", encoding="utf-8-sig", newline="") as infile:
        reader = csv.DictReader(infile)
        if not reader.fieldnames:
            raise ValueError(f"Input CSV '{input_path}' has no header columns.")

        col_name = status_col or find_status_column(reader.fieldnames)
        if not col_name or col_name not in reader.fieldnames:
            raise KeyError(
                f"Could not find status code column in {reader.fieldnames}. "
                f"Please specify using --status-col."
            )

        detected_url_col = url_col or find_url_column(reader.fieldnames)

        with output_path.open("w", encoding="utf-8", newline="") as outfile:
            if only_url_column:
                writer = csv.writer(outfile)
                writer.writerow(["url"])

                for row in reader:
                    total_rows += 1
                    status_val = row.get(col_name)

                    if is_2xx_status(status_val):
                        target_url = extract_url(row, detected_url_col)
                        if target_url:
                            writer.writerow([target_url])
                            rows_written += 1

                            if limit is not None and limit > 0 and rows_written >= limit:
                                break
            else:
                dict_writer = csv.DictWriter(outfile, fieldnames=reader.fieldnames)
                dict_writer.writeheader()

                for row in reader:
                    total_rows += 1
                    status_val = row.get(col_name)

                    if is_2xx_status(status_val):
                        dict_writer.writerow(row)
                        rows_written += 1

                        if limit is not None and limit > 0 and rows_written >= limit:
                            break

    return rows_written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Filter websites CSV for HTTP 2XX status codes with an optional limit and single URL column output.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "input_csv",
        nargs="?",
        default="urls/c-websites.csv",
        help="Path to input CSV file.",
    )
    parser.add_argument(
        "-i",
        "--input",
        dest="input_opt",
        default=None,
        help="Alternative flag to specify input CSV file.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Path to output CSV file (defaults to <input_stem>_2xx.csv).",
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=None,
        help="Maximum number of 2XX websites to store in the output CSV (None for all).",
    )
    parser.add_argument(
        "--status-col",
        default=None,
        help="Name of the status code column (auto-detected by default).",
    )
    parser.add_argument(
        "--url-col",
        default=None,
        help="Name of the URL/domain column (auto-detected from finalUrl, url, inputDomain, domain by default).",
    )
    parser.add_argument(
        "--all-columns",
        action="store_true",
        default=False,
        help="Retain all original columns instead of outputting only the single 'url' column.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_file = Path(args.input_opt or args.input_csv)

    if args.output:
        output_file = Path(args.output)
    else:
        # Generate default output name e.g. urls/c-websites_2xx.csv
        suffix = f"_2xx{f'_limit_{args.limit}' if args.limit else ''}.csv"
        output_file = input_file.parent / f"{input_file.stem}{suffix}"

    print(f"Reading:      {input_file}")
    print(f"Output:       {output_file}")
    print(f"Format:       {'Only url column' if not args.all_columns else 'All columns'}")
    if args.limit is not None and args.limit > 0:
        print(f"Limit:        {args.limit} websites")
    else:
        print("Limit:        None (all 2XX websites)")

    try:
        count = filter_2xx_websites(
            input_path=input_file,
            output_path=output_file,
            limit=args.limit,
            status_col=args.status_col,
            url_col=args.url_col,
            only_url_column=not args.all_columns,
        )
        print(f"\n[SUCCESS] Extracted {count} websites with 2XX status code into '{output_file}'.")
    except Exception as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
