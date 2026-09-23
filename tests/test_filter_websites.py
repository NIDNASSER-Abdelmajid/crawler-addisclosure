"""tests/test_filter_websites.py
----------------------------
Unit tests for the filter_websites script.
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from filter_websites import (
    filter_2xx_websites,
    is_2xx_status,
    find_status_column,
    find_url_column,
    extract_url,
)


def test_is_2xx_status():
    assert is_2xx_status(200) is True
    assert is_2xx_status("200") is True
    assert is_2xx_status("204") is True
    assert is_2xx_status("299") is True
    assert is_2xx_status("200.0") is True

    assert is_2xx_status(301) is False
    assert is_2xx_status("404") is False
    assert is_2xx_status("500") is False
    assert is_2xx_status("") is False
    assert is_2xx_status(None) is False
    assert is_2xx_status("invalid") is False


def test_find_status_column():
    assert find_status_column(["index", "domain", "statusCode", "url"]) == "statusCode"
    assert find_status_column(["index", "domain", "status_code", "url"]) == "status_code"
    assert find_status_column(["index", "domain", "status"]) == "status"
    assert find_status_column(["index", "domain", "unknown"]) is None


def test_find_url_column_and_extract_url():
    assert find_url_column(["index", "inputDomain", "finalUrl", "statusCode"]) == "finalUrl"
    assert find_url_column(["index", "url", "statusCode"]) == "url"
    assert find_url_column(["index", "domain", "statusCode"]) == "domain"

    row1 = {"finalUrl": "https://example.com", "inputDomain": "example.com"}
    assert extract_url(row1) == "https://example.com"

    row2 = {"inputDomain": "example.org"}
    assert extract_url(row2) == "https://example.org"


def test_filter_2xx_websites_single_url_column(tmp_path):
    input_file = tmp_path / "sample.csv"
    output_file = tmp_path / "output.csv"

    rows = [
        ["index", "inputDomain", "finalUrl", "statusCode"],
        ["1", "ok1.com", "https://ok1.com", "200"],
        ["2", "fail.com", "https://fail.com", "404"],
        ["3", "ok2.com", "https://ok2.com/home", "202"],
        ["4", "empty.com", "https://empty.com", ""],
        ["5", "ok3.com", "https://ok3.com", "204"],
        ["6", "redirect.com", "https://redirect.com", "302"],
        ["7", "ok4.com", "", "200"],
    ]

    with input_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)

    # Test limit=2 with single URL column
    count = filter_2xx_websites(input_file, output_file, limit=2, only_url_column=True)
    assert count == 2

    with output_file.open("r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        data = list(reader)

    assert header == ["url"]
    assert len(data) == 2
    assert data[0] == ["https://ok1.com"]
    assert data[1] == ["https://ok2.com/home"]

    # Test fallback to inputDomain if finalUrl is empty
    output_all = tmp_path / "output_all.csv"
    count_all = filter_2xx_websites(input_file, output_all, limit=None, only_url_column=True)
    assert count_all == 4

    with output_all.open("r", encoding="utf-8") as f:
        reader_all = csv.reader(f)
        next(reader_all)
        data_all = [r[0] for r in reader_all]

    assert data_all == [
        "https://ok1.com",
        "https://ok2.com/home",
        "https://ok3.com",
        "https://ok4.com",
    ]


def test_filter_2xx_websites_all_columns(tmp_path):
    input_file = tmp_path / "sample.csv"
    output_file = tmp_path / "output_all_cols.csv"

    rows = [
        ["index", "inputDomain", "statusCode"],
        ["1", "ok1.com", "200"],
        ["2", "fail.com", "404"],
    ]
    with input_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)

    count = filter_2xx_websites(input_file, output_file, only_url_column=False)
    assert count == 1

    with output_file.open("r", encoding="utf-8") as f:
        rows_out = list(csv.DictReader(f))

    assert len(rows_out) == 1
    assert rows_out[0]["index"] == "1"
    assert rows_out[0]["inputDomain"] == "ok1.com"
    assert rows_out[0]["statusCode"] == "200"
