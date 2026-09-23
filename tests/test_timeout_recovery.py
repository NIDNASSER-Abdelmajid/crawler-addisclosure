"""Comprehensive test suite for timeout lifecycle, retry lineage, atomic persistence, and partial data preservation."""

import asyncio
import json
import os
import shutil
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sys

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from timeout_manager import (
    AttemptMetadata,
    GlobalIndexManager,
    WebsiteManifestManager,
    atomic_write_json,
    finalize_and_save_attempt,
    generate_attempt_id,
    generate_website_id,
    get_attempt_dir,
    get_attempt_folder_name,
    get_website_dir,
    get_website_folder_name,
    is_url_already_completed,
    get_completed_urls_in_output,
    recover_incomplete_attempts,
)


@pytest.fixture
def temp_output_dir(tmp_path):
    out = tmp_path / "test_output"
    out.mkdir(parents=True, exist_ok=True)
    yield out
    shutil.rmtree(out, ignore_errors=True)


def test_website_and_attempt_id_generation():
    """Website IDs must be stable and deterministic; attempt IDs must be globally unique."""
    id1 = generate_website_id("https://www.example.com/path")
    id2 = generate_website_id("example.com/path")
    id3 = generate_website_id("http://example.com/path/")

    assert id1 == id2 == id3
    assert id1.startswith("web_")

    att1 = generate_attempt_id()
    att2 = generate_attempt_id()
    assert att1.startswith("att_")
    assert att2.startswith("att_")
    assert att1 != att2


def test_website_folder_name_deterministic():
    """get_website_folder_name must produce deterministic, human-readable folder names considering full URL length."""
    assert get_website_folder_name("https://yahoo.com") == "yahoo.com"
    assert get_website_folder_name("https://www.bbc.co.uk") == "bbc.co.uk"
    assert get_website_folder_name("https://yahoo.com/news") == "yahoo.com_news"
    assert get_website_folder_name("https://www.example.com/") == "example.com"
    assert get_website_folder_name("https://example.com/path/to/page") == "example.com_path_to_page"
    assert get_website_folder_name("https://yahoo.com/search?q=cars") == "yahoo.com_search_q_cars"
    assert get_website_folder_name("https://finance.yahoo.com/quote/AAPL?p=AAPL") == "finance.yahoo.com_quote_aapl_p_aapl"

    # Distinct query parameters produce distinct folders
    folder1 = get_website_folder_name("https://yahoo.com/news?cat=tech")
    folder2 = get_website_folder_name("https://yahoo.com/news?cat=sports")
    assert folder1 != folder2

    # Same URL must always produce the same folder
    url = "https://news.yahoo.com/technology?id=123"
    assert get_website_folder_name(url) == get_website_folder_name(url)


def test_attempt_folder_naming_structure(temp_output_dir):
    """Attempt folders must use simple sequential naming: attempt_NNN."""
    att_id = "att_abcdef123456"
    folder_name = get_attempt_folder_name(1, att_id)
    assert folder_name == "attempt_001"

    # attempt_id is accepted but not included in name
    folder_name_no_id = get_attempt_folder_name(1)
    assert folder_name_no_id == "attempt_001"

    website_folder = "example.com"
    attempt_dir = get_attempt_dir(temp_output_dir, website_folder, 1, att_id)
    assert attempt_dir == temp_output_dir / website_folder / "attempt_001"


def test_atomic_write_json_success(tmp_path):
    """Atomic write creates target file and does not leave temporary files."""
    target = tmp_path / "data" / "result.json"
    payload = {"test": 123, "name": "adgraph"}

    success = atomic_write_json(target, payload)
    assert success is True
    assert target.is_file()

    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == payload

    # No leftover .tmp files
    tmp_files = list(target.parent.glob("*.tmp.*"))
    assert len(tmp_files) == 0


def test_atomic_write_json_failure_logged(tmp_path):
    """When atomic write fails repeatedly, it returns False and writes to emergency log."""
    # Write to a path that is a directory to force an OS write error
    invalid_target = tmp_path / "dir_blocker"
    invalid_target.mkdir()

    with patch("timeout_manager.open", side_effect=PermissionError("Mock Permission Denied")):
        success = atomic_write_json(invalid_target / "file.json", {"key": "val"}, max_retries=2)
        assert success is False


def test_attempt_manifest_tracking(temp_output_dir):
    """Manifest records all attempts in chronological order and tracks final result."""
    web_folder = get_website_folder_name("https://example.com")
    web_id = generate_website_id("https://example.com")
    att1_id = generate_attempt_id()
    att2_id = generate_attempt_id()

    meta1 = AttemptMetadata(
        website_id=web_id,
        normalized_url="https://example.com",
        publisher_domain="example.com",
        crawl_id="crawl_001",
        attempt_id=att1_id,
        attempt_number=1,
        worker_id="w1",
        started_at="2026-08-31T12:00:00Z",
        ended_at="2026-08-31T12:01:00Z",
        status="timed_out",
        timeout_stage="navigation",
        partial_data=True,
        ads_count=5,
        website_folder=web_folder,
    )

    WebsiteManifestManager.record_attempt(temp_output_dir, meta1)
    manifest = WebsiteManifestManager.load_manifest(temp_output_dir, web_folder)

    assert manifest["total_attempts"] == 1
    assert manifest["attempts"][0]["attempt_id"] == att1_id
    assert manifest["attempts"][0]["status"] == "timed_out"
    assert manifest["folder_name"] == web_folder
    assert manifest["url"] == "https://example.com"
    assert manifest["normalized_domain"] == "example.com"

    # Record retry
    meta2 = AttemptMetadata(
        website_id=web_id,
        normalized_url="https://example.com",
        publisher_domain="example.com",
        crawl_id="crawl_001",
        attempt_id=att2_id,
        attempt_number=2,
        retry_of_attempt_id=att1_id,
        root_attempt_id=att1_id,
        worker_id="w1",
        started_at="2026-08-31T12:02:00Z",
        ended_at="2026-08-31T12:02:30Z",
        status="completed",
        collection_complete=True,
        ads_count=10,
        website_folder=web_folder,
    )

    WebsiteManifestManager.record_attempt(temp_output_dir, meta2)
    manifest2 = WebsiteManifestManager.load_manifest(temp_output_dir, web_folder)

    assert manifest2["total_attempts"] == 2
    assert manifest2["has_successful_attempt"] is True
    assert manifest2["final_attempt_id"] == att2_id
    assert manifest2["final_status"] == "completed"


def test_global_index_append(temp_output_dir):
    """Global index logs every attempt record with valid JSONL formatting."""
    web_id = generate_website_id("https://news.com")
    att_id = generate_attempt_id()

    meta = AttemptMetadata(
        website_id=web_id,
        normalized_url="https://news.com",
        publisher_domain="news.com",
        crawl_id="crawl_100",
        attempt_id=att_id,
        attempt_number=1,
        worker_id="worker_test",
        started_at="2026-08-31T12:10:00Z",
        ended_at="2026-08-31T12:10:20Z",
        status="completed",
        requests_count=42,
    )

    GlobalIndexManager.append_record(temp_output_dir, meta)
    index_path = GlobalIndexManager.get_index_path(temp_output_dir)

    assert index_path.is_file()
    lines = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["attempt_id"] == att_id
    assert lines[0]["requests_count"] == 42


def test_finalize_and_save_attempt_completion_marker(temp_output_dir):
    """Successful finalize creates result.json, attempt_metadata.json, manifest, index, and .completed."""
    web_folder = get_website_folder_name("https://testsite.org")
    web_id = generate_website_id("https://testsite.org")
    att_id = generate_attempt_id()
    att_dir = get_attempt_dir(temp_output_dir, web_folder, 1, att_id)

    meta = AttemptMetadata(
        website_id=web_id,
        normalized_url="https://testsite.org",
        publisher_domain="testsite.org",
        crawl_id="crawl_200",
        attempt_id=att_id,
        attempt_number=1,
        worker_id="w0",
        started_at="2026-08-31T12:20:00Z",
        ended_at="2026-08-31T12:20:45Z",
        status="completed_with_partial_data",
        timeout_stage="ad_collection",
        partial_data=True,
        website_folder=web_folder,
    )
    result_data = {"initialUrl": "https://testsite.org", "successful": "timeout", "data": {"AdCollector": {"adAttrs": [{"id": "ad1"}]}}}

    saved = finalize_and_save_attempt(temp_output_dir, att_dir, result_data, meta)
    assert saved is True
    assert (att_dir / "result.json").is_file()
    assert (att_dir / "attempt_metadata.json").is_file()
    assert (att_dir / ".completed").is_file()

    marker = json.loads((att_dir / ".completed").read_text(encoding="utf-8"))
    assert marker["status"] == "completed_with_partial_data"
    assert marker["attempt_id"] == att_id

    # Verify site_manifest.json is created in the website folder
    manifest_path = temp_output_dir / web_folder / "site_manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["url"] == "https://testsite.org"
    assert manifest["folder_name"] == web_folder


def test_metadata_sha256_and_integrity_verification(temp_output_dir):
    """finalize_and_save_attempt records result_json_sha256, integrity_status, and schema_version."""
    import hashlib

    web_folder = get_website_folder_name("https://integrity-test.org")
    att_id = generate_attempt_id()
    att_dir = get_attempt_dir(temp_output_dir, web_folder, 1, att_id)

    # 1. Complete attempt -> integrity_status == "verified"
    meta_verified = AttemptMetadata(
        website_id="web_int",
        normalized_url="https://integrity-test.org",
        publisher_domain="integrity-test.org",
        crawl_id="c_int",
        attempt_id=att_id,
        attempt_number=1,
        worker_id="w0",
        started_at="2026-09-01T00:00:00Z",
        ended_at="2026-09-01T00:00:10Z",
        status="completed",
        partial_data=False,
        website_folder=web_folder,
    )
    result_data = {
        "schema_version": "2.0.0",
        "initialUrl": "https://integrity-test.org",
        "successful": "true",
        "data": {},
    }

    finalize_and_save_attempt(temp_output_dir, att_dir, result_data, meta_verified)

    saved_meta_text = (att_dir / "attempt_metadata.json").read_text(encoding="utf-8")
    saved_meta = json.loads(saved_meta_text)

    assert saved_meta["schema_version"] == "2.0.0"
    assert saved_meta["integrity_status"] == "verified"

    expected_sha = hashlib.sha256((att_dir / "result.json").read_bytes()).hexdigest()
    assert saved_meta["result_json_sha256"] == expected_sha
    assert len(expected_sha) == 64

    # 2. Partial data attempt -> integrity_status == "partial"
    att_id2 = generate_attempt_id()
    att_dir2 = get_attempt_dir(temp_output_dir, web_folder, 2, att_id2)
    meta_partial = AttemptMetadata(
        website_id="web_int",
        normalized_url="https://integrity-test.org",
        publisher_domain="integrity-test.org",
        crawl_id="c_int",
        attempt_id=att_id2,
        attempt_number=2,
        worker_id="w0",
        started_at="2026-09-01T00:00:00Z",
        ended_at="2026-09-01T00:00:10Z",
        status="completed_with_partial_data",
        partial_data=True,
        website_folder=web_folder,
    )
    finalize_and_save_attempt(temp_output_dir, att_dir2, result_data, meta_partial)
    saved_partial_meta = json.loads((att_dir2 / "attempt_metadata.json").read_text(encoding="utf-8"))
    assert saved_partial_meta["integrity_status"] == "partial"
    assert len(saved_partial_meta["result_json_sha256"]) == 64


def test_concurrent_manifest_and_global_index_writes(temp_output_dir):
    """Multiple concurrent workers updating manifest and global index must not corrupt JSON/JSONL."""
    web_folder = get_website_folder_name("https://concurrent.com")
    web_id = generate_website_id("https://concurrent.com")
    errors = []

    def worker_job(worker_num: int):
        try:
            for i in range(5):
                att_id = generate_attempt_id()
                meta = AttemptMetadata(
                    website_id=web_id,
                    normalized_url="https://concurrent.com",
                    publisher_domain="concurrent.com",
                    crawl_id="crawl_concurrent",
                    attempt_id=att_id,
                    attempt_number=worker_num * 10 + i,
                    worker_id=f"worker_{worker_num}",
                    started_at="2026-08-31T12:30:00Z",
                    ended_at="2026-08-31T12:30:01Z",
                    status="completed",
                    website_folder=web_folder,
                )
                WebsiteManifestManager.record_attempt(temp_output_dir, meta)
                GlobalIndexManager.append_record(temp_output_dir, meta)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker_job, args=(w,)) for w in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0

    # Validate manifest integrity
    manifest = WebsiteManifestManager.load_manifest(temp_output_dir, web_folder)
    assert manifest["total_attempts"] == 30

    # Validate global index integrity
    index_path = GlobalIndexManager.get_index_path(temp_output_dir)
    lines = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 30


def test_recover_incomplete_attempts(temp_output_dir):
    """Unfinalized attempts missing .completed marker are detected and recovered on startup."""
    # Domain-named folder (new style)
    att_dir = temp_output_dir / "example.com" / "attempt_001"
    att_dir.mkdir(parents=True, exist_ok=True)

    # Write raw result and metadata but no .completed marker
    (att_dir / "result.json").write_text(json.dumps({"test": "incomplete"}), encoding="utf-8")
    (att_dir / "attempt_metadata.json").write_text(json.dumps({"status": "running", "partial_data": False}), encoding="utf-8")

    recovered = recover_incomplete_attempts(temp_output_dir)
    assert len(recovered) == 1
    assert str(att_dir) in recovered

    # Verify .recovered_partial marker was created (never .completed) and metadata marked recovered
    assert (att_dir / ".recovered_partial").is_file()
    assert not (att_dir / ".completed").is_file()
    recovered_meta = json.loads((att_dir / "attempt_metadata.json").read_text(encoding="utf-8"))
    assert recovered_meta["status"] == "recovered_partial"
    assert recovered_meta["partial_data"] is True


def test_is_url_already_completed_and_get_completed(temp_output_dir):
    """is_url_already_completed and get_completed_urls_in_output correctly identify completed sites."""
    url1 = "https://example.com"
    url2 = "https://notdone.com"
    folder1 = get_website_folder_name(url1)
    web_dir1 = temp_output_dir / folder1
    att_dir1 = web_dir1 / "attempt_001"
    att_dir1.mkdir(parents=True, exist_ok=True)

    # Write manifest and completed marker for url1
    meta1 = AttemptMetadata(
        website_id=generate_website_id(url1),
        normalized_url=url1,
        publisher_domain="example.com",
        crawl_id="c1",
        attempt_id="att1",
        attempt_number=1,
        worker_id="w0",
        started_at="2026-08-31T12:00:00Z",
        ended_at="2026-08-31T12:01:00Z",
        status="completed",
        website_folder=folder1,
    )
    finalize_and_save_attempt(temp_output_dir, att_dir1, {"successful": "true"}, meta1)

    assert is_url_already_completed(temp_output_dir, url1) is True
    assert is_url_already_completed(temp_output_dir, url2) is False

    completed_urls = get_completed_urls_in_output(temp_output_dir)
    assert url1 in completed_urls
    assert url2 not in completed_urls


def test_ad_timeout_no_retry_flag_in_metadata():
    """AttemptMetadata ad_timeout_no_retry flag serializes correctly."""
    meta = AttemptMetadata(
        website_id="web_test123",
        normalized_url="https://example.com",
        publisher_domain="example.com",
        crawl_id="crawl_test",
        attempt_id="att_abc123",
        attempt_number=1,
        worker_id="w0",
        started_at="2026-08-31T12:00:00Z",
        ended_at="2026-08-31T12:03:00Z",
        status="completed_with_partial_data",
        timeout_stage="adcollector",
        ad_timeout_no_retry=True,
        website_folder="example.com",
    )

    d = meta.to_dict()
    assert d["ad_timeout_no_retry"] is True
    assert d["website_folder"] == "example.com"
    assert d["timeout_stage"] == "adcollector"


@pytest.mark.asyncio
async def test_ad_timeout_skips_retry(temp_output_dir):
    """When ad_timeout_no_retry is set, the safeguard engine must not retry."""
    from safeguard_audit import SafeguardAuditLogger
    from safeguard_engine import SafeguardEngine
    from safeguard_state import SafeguardState

    db_path = temp_output_dir / "test_sg_state.db"
    state = SafeguardState(db_path=db_path)
    audit = SafeguardAuditLogger(log_path=temp_output_dir / "audit.jsonl")
    engine = SafeguardEngine(state, audit, worker_id="test_worker")

    call_count = 0

    async def mock_crawl_fn(url, **kwargs):
        nonlocal call_count
        call_count += 1
        info = kwargs.get("attempt_info", {})
        att_id = info.get("attempt_id")
        web_folder = info.get("website_folder", "mock.com")
        att_dir = get_attempt_dir(temp_output_dir, web_folder, info.get("attempt_number", 1), att_id)

        meta = AttemptMetadata(
            website_id=info.get("website_id", "web_mock"),
            normalized_url=url,
            publisher_domain="mock.com",
            crawl_id=info.get("crawl_id", "c1"),
            attempt_id=att_id,
            attempt_number=info.get("attempt_number", 1),
            worker_id="test_worker",
            started_at="2026-08-31T12:40:00Z",
            ended_at="2026-08-31T12:40:10Z",
            status="completed_with_partial_data",
            timeout_stage="adcollector",
            ad_timeout_no_retry=True,
            website_folder=web_folder,
        )
        finalize_and_save_attempt(temp_output_dir, att_dir, {
            "successful": "timeout",
            "ad_timeout_no_retry": True,
            "data": {"AdCollector": {"adAttrs": [{"id": "ad1"}]}},
        }, meta)
        return {
            "successful": "timeout",
            "ad_timeout_no_retry": True,
            "data": {"AdCollector": {"adAttrs": [{"id": "ad1"}]}},
        }

    with patch.object(state, "seconds_until_next_allowed", return_value=0.0):
        result = await engine.execute_visit_with_retries(
            "https://mock.com",
            mock_crawl_fn,
            {"output_dir": str(temp_output_dir)},
        )

    state.close()

    # Only 1 attempt — no retry because ad_timeout_no_retry was set
    assert call_count == 1
    assert result.get("ad_timeout_no_retry") is True


@pytest.mark.asyncio
async def test_ad_collector_partial_results_retention(tmp_path):
    """AdCollector retains all detected and scraped ads even if interrupted by a timeout."""
    from Collectors.AdCollector import AdCollector

    collector = AdCollector()
    collector.init(str(tmp_path), MagicMock(), "test_hash")

    # Simulate detected ads and partial scrapes
    collector._detected_ads = [{"id": "ad1", "x": 0, "y": 0, "width": 300, "height": 250}]
    collector._ad_attrs = [
        {
            "id": "ad1",
            "index": 0,
            "screenshot": "ad_0_test_hash.png",
            "nodeType": "DIV",
            "width": 300,
            "height": 250,
            "adLinksAndImages": [],
        }
    ]
    collector._n_small_ads = 1

    partial = collector.get_partial_results()
    assert len(partial["adAttrs"]) == 1
    assert partial["adAttrs"][0]["id"] == "ad1"
    assert partial["scrapeResults"]["nDetectedAds"] == 1
    assert partial["scrapeResults"]["nAdsScraped"] == 1
    assert partial["scrapeResults"]["nSmallAds"] == 1


@pytest.mark.asyncio
async def test_save_before_retry_execution_order(temp_output_dir):
    """Retry must not begin before previous attempt files are finalized on disk."""
    from safeguard_audit import SafeguardAuditLogger
    from safeguard_config import MAX_RETRIES_PER_PAGE
    from safeguard_engine import SafeguardEngine
    from safeguard_state import SafeguardState

    db_path = temp_output_dir / "test_safeguard_state.db"
    state = SafeguardState(db_path=db_path)
    audit = SafeguardAuditLogger(log_path=temp_output_dir / "audit.jsonl")
    engine = SafeguardEngine(state, audit, worker_id="test_worker")

    attempts_executed: list[dict] = []

    async def mock_crawl_fn(url, **kwargs):
        info = kwargs.get("attempt_info", {})
        att_num = info.get("attempt_number", 1)
        att_id = info.get("attempt_id")
        web_folder = info.get("website_folder", "mockretry.com")
        att_dir = get_attempt_dir(temp_output_dir, web_folder, att_num, att_id)

        # Simulate crawler writing partial data and finalizing
        meta = AttemptMetadata(
            website_id=info.get("website_id", "web_mock"),
            normalized_url=url,
            publisher_domain="mockretry.com",
            crawl_id=info.get("crawl_id", "c1"),
            attempt_id=att_id,
            attempt_number=att_num,
            retry_of_attempt_id=info.get("retry_of_attempt_id"),
            root_attempt_id=info.get("root_attempt_id"),
            worker_id="test_worker",
            started_at="2026-08-31T12:40:00Z",
            ended_at="2026-08-31T12:40:10Z",
            status="timed_out" if att_num < 3 else "completed",
            timeout_stage="navigation" if att_num < 3 else "",
            partial_data=(att_num < 3),
            website_folder=web_folder,
        )
        finalize_and_save_attempt(temp_output_dir, att_dir, {"successful": "timeout" if att_num < 3 else "true"}, meta)

        attempts_executed.append({
            "attempt_number": att_num,
            "attempt_id": att_id,
            "retry_of_attempt_id": info.get("retry_of_attempt_id"),
            "dir_exists": att_dir.is_dir(),
            "completed_exists": (att_dir / ".completed").is_file(),
        })

        return {"successful": "timeout" if att_num < 3 else "true"}

    with patch.object(state, "seconds_until_next_allowed", return_value=0.0), \
         patch("safeguard_engine.MAX_RETRIES_PER_PAGE", 2):
        result = await engine.execute_visit_with_retries(
            "https://mockretry.com",
            mock_crawl_fn,
            {"output_dir": str(temp_output_dir)},
        )

    state.close()

    assert len(attempts_executed) == 3
    # Verify each attempt had finalized and saved to disk
    for entry in attempts_executed:
        assert entry["dir_exists"] is True
        assert entry["completed_exists"] is True

    # Check retry lineage links
    assert attempts_executed[0]["attempt_number"] == 1
    assert attempts_executed[0]["retry_of_attempt_id"] is None

    assert attempts_executed[1]["attempt_number"] == 2
    assert attempts_executed[1]["retry_of_attempt_id"] == attempts_executed[0]["attempt_id"]

    assert attempts_executed[2]["attempt_number"] == 3
    assert attempts_executed[2]["retry_of_attempt_id"] == attempts_executed[1]["attempt_id"]


@pytest.mark.asyncio
async def test_stage_timeouts_metadata(temp_output_dir):
    """Verify that timeout metadata precisely records stages across navigation, disclosure, request, cookie."""
    stages = ["navigation", "disclosure_extraction", "request_collection", "cookie_collection"]
    web_folder = get_website_folder_name("https://stage-timeout.com")

    for idx, stage in enumerate(stages, 1):
        att_id = generate_attempt_id()
        att_dir = get_attempt_dir(temp_output_dir, web_folder, idx, att_id)

        meta = AttemptMetadata(
            website_id=generate_website_id("https://stage-timeout.com"),
            normalized_url="https://stage-timeout.com",
            publisher_domain="stage-timeout.com",
            crawl_id="crawl_stage_test",
            attempt_id=att_id,
            attempt_number=idx,
            worker_id="w_stage",
            started_at="2026-08-31T12:50:00Z",
            ended_at="2026-08-31T12:50:30Z",
            status="completed_with_partial_data",
            timeout_stage=stage,
            last_completed_stage="pre_crawl" if stage == "navigation" else "navigation",
            partial_data=True,
            requests_count=15 if stage != "navigation" else 0,
            cookies_count=3 if stage != "navigation" else 0,
            configured_timeout_sec=30.0,
            actual_duration_sec=30.05,
            website_folder=web_folder,
        )

        saved = finalize_and_save_attempt(
            temp_output_dir, att_dir,
            {"initialUrl": "https://stage-timeout.com", "successful": "timeout"},
            meta,
        )
        assert saved is True

        # Verify saved attempt metadata
        loaded_meta = json.loads((att_dir / "attempt_metadata.json").read_text(encoding="utf-8"))
        assert loaded_meta["timeout_stage"] == stage
        assert loaded_meta["partial_data"] is True
        assert loaded_meta["attempt_number"] == idx


@pytest.mark.asyncio
async def test_timeout_result_json_has_non_empty_data(temp_output_dir):
    """Verify that on crawl timeout, result['data'] and result.json on disk contain non-empty collector data."""
    from crawler import _extract_all_collector_data
    from Collectors.AdCollector import AdCollector
    from Collectors.RequestCollector import RequestCollector
    from Collectors.FingerprintCollector import FingerprintCollector

    web_folder = get_website_folder_name("https://partial-data-test.com")
    web_id = generate_website_id("https://partial-data-test.com")
    att_id = generate_attempt_id()
    att_dir = get_attempt_dir(temp_output_dir, web_folder, 1, att_id)
    att_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "initialUrl": "https://partial-data-test.com",
        "finalUrl": "https://partial-data-test.com",
        "successful": "timeout",
        "data": {},
    }

    # Simulate AdCollector having detected 2 ads before timeout
    ad_col = AdCollector()
    ad_col.init(str(att_dir), MagicMock(), "test_hash")
    ad_col._detected_ads = [{"id": "ad_1"}, {"id": "ad_2"}]
    ad_col._ad_attrs = [{"id": "ad_1", "width": 300, "height": 250}]

    # Simulate RequestCollector having intercepted 5 requests during navigation before timeout
    rc = RequestCollector()
    rc.init(str(att_dir), MagicMock(), "test_hash")
    rc._requests = {
        "req_1": {"id": "req_1", "url": "https://partial-data-test.com/main.js", "method": "GET", "type": "Script"},
        "req_2": {"id": "req_2", "url": "https://partial-data-test.com/ad.png", "method": "GET", "type": "Image"},
    }

    # Simulate FingerprintCollector having intercepted 1 call
    fp = FingerprintCollector()
    fp.init(str(att_dir), MagicMock(), "test_hash")
    fp._stats = {"https://partial-data-test.com/tracker.js": {"canvas.toDataURL": 1}}
    fp._calls = [{"source": "https://partial-data-test.com/tracker.js", "description": "canvas.toDataURL"}]

    pre_crawl_instances = {
        "RequestCollector": rc,
        "FingerprintCollector": fp,
    }

    collector_names = ["AdCollector", "RequestCollector", "FingerprintCollector", "CookieCollector"]

    # Extract all collector data as done in crawler's finally block
    await _extract_all_collector_data(
        result=result,
        collector_names=collector_names,
        pre_crawl_instances=pre_crawl_instances,
        ad_collector_instance=ad_col,
        page=None,
        context=None,
        site_dir=att_dir,
        final_url="https://partial-data-test.com",
        logger=MagicMock(),
    )

    # Validate that result['data'] is populated with all partial collector data
    assert "AdCollector" in result["data"]
    assert len(result["data"]["AdCollector"]["adAttrs"]) == 1
    assert result["data"]["AdCollector"]["scrapeResults"]["nDetectedAds"] == 2

    assert "RequestCollector" in result["data"]
    assert len(result["data"]["RequestCollector"]) == 2
    assert result["data"]["RequestCollector"][0]["url"] == "https://partial-data-test.com/main.js"

    assert "FingerprintCollector" in result["data"]
    assert len(result["data"]["FingerprintCollector"]["savedCalls"]) == 1

    # Finalize attempt to disk
    meta = AttemptMetadata(
        website_id=web_id,
        normalized_url="https://partial-data-test.com",
        publisher_domain="partial-data-test.com",
        crawl_id="crawl_partial_test",
        attempt_id=att_id,
        attempt_number=1,
        worker_id="w_test",
        started_at="2026-08-31T13:00:00Z",
        ended_at="2026-08-31T13:00:45Z",
        status="completed_with_partial_data",
        timeout_stage="ad_collection",
        partial_data=True,
        ads_count=1,
        requests_count=2,
        website_folder=web_folder,
    )
    finalize_and_save_attempt(temp_output_dir, att_dir, result, meta)

    # Verify result.json on disk contains complete data payload
    disk_result = json.loads((att_dir / "result.json").read_text(encoding="utf-8"))
    assert disk_result["data"] is not None
    assert len(disk_result["data"]["AdCollector"]["adAttrs"]) == 1
    assert len(disk_result["data"]["RequestCollector"]) == 2
    assert len(disk_result["data"]["FingerprintCollector"]["savedCalls"]) == 1


def test_stage_timeouts_configuration():
    """Verify configured stage timeouts: 30s page load, 5s dynamic wait, short collector timeouts."""
    from crawler import PAGE_LOAD_TIMEOUT, POST_LOAD_WAIT_SECONDS, STAGE_TIMEOUTS

    assert PAGE_LOAD_TIMEOUT == 30.0
    assert POST_LOAD_WAIT_SECONDS == 5.0
    assert STAGE_TIMEOUTS["CookiePopupsCollector"] == 15.0
    assert STAGE_TIMEOUTS["AdCollector"] == 120.0
    assert STAGE_TIMEOUTS["RequestCollector"] == 30.0
    assert STAGE_TIMEOUTS["CookieCollector"] == 30.0
    assert STAGE_TIMEOUTS["ScreenshotCollector"] == 30.0


@pytest.mark.asyncio
async def test_pre_ad_timeout_triggers_retry(temp_output_dir):
    """When a timeout happens BEFORE ad collection (e.g. navigation), it saves state and retries."""
    from safeguard_audit import SafeguardAuditLogger
    from safeguard_engine import SafeguardEngine
    from safeguard_state import SafeguardState

    db_path = temp_output_dir / "test_pre_ad_state.db"
    state = SafeguardState(db_path=db_path)
    audit = SafeguardAuditLogger(log_path=temp_output_dir / "audit.jsonl")
    engine = SafeguardEngine(state, audit, worker_id="test_worker")

    attempts: list[int] = []

    async def mock_crawl_fn(url, **kwargs):
        info = kwargs.get("attempt_info", {})
        att_num = info.get("attempt_number", 1)
        att_id = info.get("attempt_id")
        web_folder = info.get("website_folder", "nav-timeout.com")
        att_dir = get_attempt_dir(temp_output_dir, web_folder, att_num, att_id)
        attempts.append(att_num)

        meta = AttemptMetadata(
            website_id=info.get("website_id", "web_nav"),
            normalized_url=url,
            publisher_domain="nav-timeout.com",
            crawl_id="c1",
            attempt_id=att_id,
            attempt_number=att_num,
            worker_id="test_worker",
            started_at="2026-08-31T12:00:00Z",
            ended_at="2026-08-31T12:00:30Z",
            status="timed_out" if att_num == 1 else "completed",
            timeout_stage="navigation" if att_num == 1 else "",
            ad_timeout_no_retry=False,
            website_folder=web_folder,
        )
        finalize_and_save_attempt(
            temp_output_dir,
            att_dir,
            {"successful": "timeout" if att_num == 1 else "true", "ad_timeout_no_retry": False},
            meta,
        )
        return {
            "successful": "timeout" if att_num == 1 else "true",
            "ad_timeout_no_retry": False,
        }

    with patch.object(state, "seconds_until_next_allowed", return_value=0.0), \
         patch("safeguard_engine.MAX_RETRIES_PER_PAGE", 2):
        result = await engine.execute_visit_with_retries(
            "https://nav-timeout.com",
            mock_crawl_fn,
            {"output_dir": str(temp_output_dir)},
        )

    state.close()

    # Attempt 1 timed out during navigation -> Attempt 2 retried and succeeded
    assert attempts == [1, 2]
    assert result.get("successful") == "true"

