"""Timeout, Attempt Lineage, Atomic Persistence, and Recovery Manager.

Guarantees:
- Every crawl attempt is stored under a deterministic, human-readable website folder.
- Folder names are derived from the URL's domain (e.g. yahoo.com/).
- Attempts use sequential numbering: attempt_001/, attempt_002/.
- site_manifest.json maps URL → domain → folder and tracks all attempts.
- Data is saved atomically before browser closure and before any retry begins.
- A global index (crawl_attempts.jsonl) maintains complete chronological records.
- Crashes/interruptions are detected on restart and partial data is recovered.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import shutil
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import re
from urllib.parse import urlparse

from Helpers.hasher import get_folder_name, get_registrable_domain

logger = logging.getLogger(__name__)

_EMERGENCY_LOG_PATH = Path("resources/emergency_save_failures.log")
_GLOBAL_INDEX_LOCK = threading.Lock()
_MANIFEST_LOCK = threading.Lock()


def generate_website_id(url_or_domain: str) -> str:
    """Generate a stable identifier for a normalized input website considering the full URL.

    Format: web_<sha256(canonical_full_url)[:16]>
    """
    raw = (url_or_domain or "").strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = (parsed.path or "").rstrip("/")
        query = f"?{parsed.query}" if parsed.query else ""
        canonical = f"https://{host}{path}{query}"
    except Exception:
        canonical = raw.lower()

    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"web_{digest}"


def generate_attempt_id() -> str:
    """Generate a globally unique attempt identifier.

    Format: att_<uuid4[:12]>
    """
    return f"att_{uuid.uuid4().hex[:12]}"


def get_website_folder_name(url: str) -> str:
    """Return a deterministic, filesystem-safe, human-readable folder name for the full URL.

    Considers the full length of the URL including full hostname (with subdomains),
    full path hierarchy, and query parameters so distinct depth URLs map to distinct folders.

    Examples:
        https://yahoo.com                        -> yahoo.com
        https://finance.yahoo.com/news/art-123   -> finance.yahoo.com_news_art_123
        https://yahoo.com/search?q=cars          -> yahoo.com_search_q_cars
    """
    raw = (url or "").strip()
    if not raw:
        return "site"
    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        host_clean = re.sub(r"[^a-z0-9.\-]", "_", host) or "site"
        path_part = (parsed.path or "").strip("/")
        query_part = (parsed.query or "").strip()
    except Exception:
        host_clean = "site"
        path_part = ""
        query_part = ""

    parts = [host_clean]
    if path_part and path_part != "/":
        clean_path = re.sub(r"[^a-z0-9]+", "_", path_part.lower()).strip("_")
        if clean_path:
            parts.append(clean_path)

    if query_part:
        clean_query = re.sub(r"[^a-z0-9]+", "_", query_part.lower()).strip("_")
        if clean_query:
            parts.append(clean_query)

    slug = "_".join(parts)
    # Ensure Windows MAX_PATH safety by capping slug length while maintaining uniqueness
    if len(slug) > 100:
        url_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
        slug = f"{slug[:85]}_{url_hash}"

    return slug


def get_attempt_folder_name(
    attempt_number: int,
    attempt_id: str | None = None,
    profile_name: str | None = None,
) -> str:
    """Format attempt folder name as attempt_profile_<name> when profile_name is provided,
    or attempt_NNN otherwise.
    """
    if profile_name:
        norm = profile_name.strip().lower()
        if not norm.startswith("profile_"):
            norm = f"profile_{norm}"
        return f"attempt_{norm}"
    return f"attempt_{attempt_number:03d}"


def get_website_dir(base_output_dir: Path | str, website_folder: str) -> Path:
    """Return <base_output_dir>/<website_folder>/ path."""
    return Path(base_output_dir) / website_folder


def get_attempt_dir(
    base_output_dir: Path | str,
    website_folder: str,
    attempt_number: int,
    attempt_id: str | None = None,
    profile_name: str | None = None,
) -> Path:
    """Return <base_output_dir>/<website_folder>/attempt_... path."""
    return get_website_dir(base_output_dir, website_folder) / get_attempt_folder_name(
        attempt_number, attempt_id, profile_name=profile_name
    )


def is_url_already_completed(
    base_output_dir: Path | str,
    url: str,
    profile_name: str | None = None,
) -> bool:
    """Check if a URL has already been crawled and completed in the output directory.

    If profile_name is specified, checks specifically if that profile's attempt completed.
    Otherwise checks if any attempt completed.
    """
    if not url or not base_output_dir:
        return False

    folder_name = get_website_folder_name(url)
    web_dir = get_website_dir(base_output_dir, folder_name)
    if not web_dir.is_dir():
        return False

    if profile_name:
        norm = profile_name.strip().lower()
        if not norm.startswith("profile_"):
            norm = f"profile_{norm}"
        expected_folder = f"attempt_{norm}"
        prof_attempt_dir = web_dir / expected_folder
        if prof_attempt_dir.is_dir() and (prof_attempt_dir / ".completed").is_file():
            return True
        for child in web_dir.iterdir():
            if child.is_dir() and child.name.startswith(expected_folder) and (child / ".completed").is_file():
                return True
        return False

    manifest_path = web_dir / WebsiteManifestManager.MANIFEST_FILENAME
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("has_successful_attempt") or manifest.get("final_status") in {
                "completed",
                "completed_with_partial_data",
            }:
                return True
        except Exception:
            pass

    for child in web_dir.iterdir():
        if child.is_dir() and child.name.startswith("attempt_") and (child / ".completed").is_file():
            return True

    return False


def get_completed_urls_in_output(base_output_dir: Path | str) -> set[str]:
    """Scan base_output_dir and return a set of all URLs that have completed attempts."""
    completed: set[str] = set()
    base_dir = Path(base_output_dir)
    if not base_dir.is_dir():
        return completed

    for web_dir in base_dir.iterdir():
        if not web_dir.is_dir():
            continue
        manifest_path = web_dir / WebsiteManifestManager.MANIFEST_FILENAME
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                url = manifest.get("url")
                if url and (
                    manifest.get("has_successful_attempt")
                    or manifest.get("final_status") in {"completed", "completed_with_partial_data"}
                ):
                    completed.add(url)
            except Exception:
                pass
    return completed


def atomic_write_json(target_path: Path, data: Any, max_retries: int = 3) -> bool:
    """Atomically write JSON data to disk using temporary file rename with retry.

    1. Write to target_path.tmp.<uuid>
    2. Flush and sync file descriptors.
    3. Atomically replace target_path with temp file.
    """
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.parent / f"{target_path.name}.tmp.{uuid.uuid4().hex[:8]}"

    for attempt in range(1, max_retries + 1):
        try:
            with open(temp_path, "w", encoding="utf-8") as fp:
                json.dump(data, fp, indent=2, ensure_ascii=False)
                fp.flush()
                os.fsync(fp.fileno())

            # Atomic replace (supported natively on POSIX and modern Windows Python 3.3+)
            temp_path.replace(target_path)
            return True
        except Exception as exc:
            logger.warning(
                f"[AtomicWrite] Attempt {attempt}/{max_retries} failed writing {target_path}: {exc}"
            )
            time.sleep(0.05 * (2 ** (attempt - 1)))
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass

    # Log emergency failure
    _log_emergency_save_failure(target_path, "Max atomic write retries exceeded")
    return False


def _log_emergency_save_failure(target_path: Path, reason: str) -> None:
    """Record an unrecoverable save failure in the emergency log."""
    try:
        _EMERGENCY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "target_path": str(target_path),
            "reason": reason,
        }
        with open(_EMERGENCY_LOG_PATH, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(record) + "\n")
    except Exception as log_exc:
        print(f"[CRITICAL] Failed to write emergency save log: {log_exc}", file=sys.stderr)


@dataclass
class AttemptMetadata:
    website_id: str
    normalized_url: str
    publisher_domain: str
    crawl_id: str
    attempt_id: str
    attempt_number: int
    worker_id: str
    started_at: str
    ended_at: str
    status: str  # completed, completed_with_partial_data, timed_out, failed, interrupted, save_failed
    failure_reason: str = ""
    timeout_stage: str = ""
    last_completed_stage: str = ""
    retry_of_attempt_id: Optional[str] = None
    root_attempt_id: Optional[str] = None
    input_index: Optional[int] = None
    output_folder: str = ""
    website_folder: str = ""  # deterministic domain-based folder name
    partial_data: bool = False
    collection_complete: bool = False
    retry_scheduled: bool = False
    retry_delay_sec: float = 0.0
    final_attempt: bool = False
    ad_timeout_no_retry: bool = False  # True when timeout was at/after ad collection
    # Counts
    ads_count: int = 0
    disclosures_count: Any = 0
    cookies_count: int = 0
    requests_count: int = 0
    fingerprints_count: int = 0
    screenshots_saved: bool = False
    # Extra diagnostic metrics
    configured_timeout_sec: float = 0.0
    actual_duration_sec: float = 0.0
    error_type: str = ""
    depth_level: int = 0
    parent_url: Optional[str] = None
    profile_name: Optional[str] = None
    schema_version: str = "2.0.0"
    result_json_sha256: str = ""
    integrity_status: str = ""



    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WebsiteManifestManager:
    """Manages <website_folder>/site_manifest.json with full URL ↔ folder mapping."""

    MANIFEST_FILENAME = "site_manifest.json"

    @staticmethod
    def get_manifest_path(base_output_dir: Path | str, website_folder: str) -> Path:
        return get_website_dir(base_output_dir, website_folder) / WebsiteManifestManager.MANIFEST_FILENAME

    @classmethod
    def load_manifest(cls, base_output_dir: Path | str, website_folder: str) -> dict[str, Any]:
        path = cls.get_manifest_path(base_output_dir, website_folder)
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "url": "",
            "normalized_domain": "",
            "folder_name": website_folder,
            "website_id": "",
            "crawl_id": "",
            "attempts": [],
            "final_status": None,
            "final_attempt_id": None,
            "has_successful_attempt": False,
            "total_attempts": 0,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def record_attempt(
        cls,
        base_output_dir: Path | str,
        metadata: AttemptMetadata,
    ) -> bool:
        """Add or update an attempt in the website manifest."""
        website_folder = metadata.website_folder or get_website_folder_name(metadata.normalized_url)
        with _MANIFEST_LOCK:
            manifest = cls.load_manifest(base_output_dir, website_folder)
            manifest["url"] = metadata.normalized_url
            manifest["normalized_domain"] = metadata.publisher_domain
            manifest["folder_name"] = website_folder
            manifest["website_id"] = metadata.website_id
            manifest["crawl_id"] = metadata.crawl_id
            manifest["depth_level"] = metadata.depth_level
            manifest["parent_url"] = metadata.parent_url or metadata.normalized_url

            current_folder = get_attempt_folder_name(metadata.attempt_number, profile_name=metadata.profile_name)
            # Remove existing attempt with same ID or folder if updating
            attempts = [
                a for a in manifest["attempts"]
                if a.get("attempt_id") != metadata.attempt_id and a.get("folder") != current_folder
            ]
            attempt_summary = {
                "attempt_number": metadata.attempt_number,
                "attempt_id": metadata.attempt_id,
                "status": metadata.status,
                "timeout_stage": metadata.timeout_stage,
                "ad_timeout_no_retry": metadata.ad_timeout_no_retry,
                "folder": current_folder,
                "profile_name": metadata.profile_name,
                "depth_level": metadata.depth_level,
                "parent_url": metadata.parent_url or metadata.normalized_url,
                "started_at": metadata.started_at,
                "ended_at": metadata.ended_at,
            }
            attempts.append(attempt_summary)
            attempts.sort(key=lambda a: (a.get("attempt_number", 0), a.get("started_at", "")))

            manifest["attempts"] = attempts
            manifest["total_attempts"] = len(attempts)

            # Determine final attempt and status
            successful_attempts = [a for a in attempts if a.get("status") in {"completed", "completed_with_partial_data"}]
            if successful_attempts:
                manifest["has_successful_attempt"] = True
                manifest["final_attempt_id"] = successful_attempts[-1]["attempt_id"]
                manifest["final_status"] = successful_attempts[-1]["status"]
            elif attempts:
                manifest["final_attempt_id"] = attempts[-1]["attempt_id"]
                manifest["final_status"] = attempts[-1]["status"]

            manifest["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            manifest_path = cls.get_manifest_path(base_output_dir, website_folder)
            return atomic_write_json(manifest_path, manifest)


class GlobalIndexManager:
    """Manages concurrency-safe append to processed/crawl_attempts.jsonl."""

    @staticmethod
    def get_index_path(base_output_dir: Path | str) -> Path:
        return Path(base_output_dir) / "crawl_attempts.jsonl"

    @classmethod
    def append_record(
        cls,
        base_output_dir: Path | str,
        metadata: AttemptMetadata,
    ) -> bool:
        path = cls.get_index_path(base_output_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = metadata.to_dict()

        with _GLOBAL_INDEX_LOCK:
            try:
                with open(path, "a", encoding="utf-8") as fp:
                    fp.write(json.dumps(record, ensure_ascii=False) + "\n")
                    fp.flush()
                return True
            except Exception as exc:
                logger.error(f"[GlobalIndex] Failed appending attempt {metadata.attempt_id}: {exc}")
                _log_emergency_save_failure(path, f"Failed appending attempt: {exc}")
                return False


def finalize_and_save_attempt(
    base_output_dir: Path | str,
    attempt_dir: Path,
    result: dict[str, Any],
    metadata: AttemptMetadata,
) -> bool:
    """Save all attempt outputs atomically, write manifest, update global index, and set .completed marker.

    Guarantees:
    - result.json and attempt_metadata.json are atomically committed.
    - Manifest and global index are updated before returning.
    - If saving fails, retries are attempted and emergency log is written.
    - .completed file is placed inside attempt_dir to confirm write success.
    """
    attempt_dir = Path(attempt_dir)
    attempt_dir.mkdir(parents=True, exist_ok=True)

    # 1. Write result.json
    result_path = attempt_dir / "result.json"
    result_saved = atomic_write_json(result_path, result)
    if not result_saved:
        metadata.status = "save_failed"
        metadata.integrity_status = "save_failed"
        metadata.failure_reason = "Failed atomic write for result.json"
        _log_emergency_save_failure(result_path, "Could not write result.json")
    else:
        try:
            with open(result_path, "rb") as rf:
                metadata.result_json_sha256 = hashlib.sha256(rf.read()).hexdigest()
            metadata.integrity_status = "partial" if (metadata.partial_data or metadata.status == "completed_with_partial_data") else "verified"
        except Exception as hash_exc:
            logger.warning(f"Failed computing sha256 for {result_path}: {hash_exc}")

    # 2. Write attempt_metadata.json
    metadata_path = attempt_dir / "attempt_metadata.json"
    meta_saved = atomic_write_json(metadata_path, metadata.to_dict())
    if not meta_saved:
        metadata.status = "save_failed"
        metadata.integrity_status = "save_failed"
        _log_emergency_save_failure(metadata_path, "Could not write attempt_metadata.json")

    # 3. Update website manifest
    manifest_saved = WebsiteManifestManager.record_attempt(base_output_dir, metadata)

    # 4. Update global index
    index_saved = GlobalIndexManager.append_record(base_output_dir, metadata)

    # 5. Place completion marker
    if result_saved and meta_saved:
        marker_path = attempt_dir / ".completed"
        try:
            marker_path.write_text(
                json.dumps({
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "status": metadata.status,
                    "attempt_id": metadata.attempt_id,
                }),
                encoding="utf-8",
            )
            return True
        except Exception as exc:
            logger.warning(f"Failed writing .completed marker: {exc}")

    return result_saved and meta_saved


def recover_incomplete_attempts(base_output_dir: Path | str) -> list[str]:
    """Scan base_output_dir for attempt folders lacking .completed markers and recover them.

    Scans all subdirectories that contain attempt_NNN/ folders (domain-named or
    legacy web_* folders).
    """
    base_dir = Path(base_output_dir)
    if not base_dir.is_dir():
        return []

    recovered: list[str] = []
    for web_dir in base_dir.iterdir():
        if not web_dir.is_dir():
            continue
        # Skip non-website directories (files, depth CSVs, etc.)
        if web_dir.suffix in {".jsonl", ".csv", ".json", ".log", ".txt"}:
            continue

        for att_dir in web_dir.iterdir():
            if not att_dir.is_dir() or not att_dir.name.startswith("attempt_"):
                continue

            marker = att_dir / ".completed"
            if marker.exists():
                continue

            # Incomplete attempt found
            result_path = att_dir / "result.json"
            meta_path = att_dir / "attempt_metadata.json"

            if result_path.is_file() and meta_path.is_file():
                try:
                    meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
                    meta_data["status"] = "recovered_partial"
                    meta_data["partial_data"] = True
                    atomic_write_json(meta_path, meta_data)
                    recovered_marker = att_dir / ".recovered_partial"
                    recovered_marker.write_text(json.dumps({"recovered_at": datetime.now(timezone.utc).isoformat(), "status": "recovered_partial"}), encoding="utf-8")
                    recovered.append(str(att_dir))
                except Exception:
                    pass
            elif result_path.is_file():
                try:
                    recovered_marker = att_dir / ".recovered_partial"
                    recovered_marker.write_text(json.dumps({"recovered_raw_result": True, "status": "recovered_partial"}), encoding="utf-8")
                    recovered.append(str(att_dir))
                except Exception:
                    pass

    return recovered
