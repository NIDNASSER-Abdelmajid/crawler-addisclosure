"""Structured append-only JSONL audit log for safeguard events.

Every safeguard decision — visit allowed, delayed, rejected, CAPTCHA detected,
429 backoff, emergency stop, etc. — is recorded as a single JSON line with
UTC timestamps and all required context fields.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from safeguard_config import AUDIT_LOG_PATH, CONFIG_VERSION, SOFTWARE_VERSION

# ---------------------------------------------------------------------------
# Sensitive parameter names that must be redacted from URLs
# ---------------------------------------------------------------------------
_SENSITIVE_QUERY_KEYS: frozenset[str] = frozenset({
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "access_token", "auth", "authorization", "session", "sessionid",
    "cookie", "csrf", "nonce", "key", "private",
})


# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------
class EventType:
    """All required audit-log event categories."""

    VISIT_ALLOWED = "visit_allowed"
    VISIT_DELAYED = "visit_delayed"
    VISIT_REJECTED = "visit_rejected"
    DOMAIN_INTERVAL_ENFORCED = "domain_interval_enforced"
    DAILY_LIMIT_REACHED = "daily_domain_limit_reached"
    GLOBAL_CONCURRENCY_LIMIT = "global_concurrency_limit_reached"
    PER_DOMAIN_CONCURRENCY_LIMIT = "per_domain_concurrency_limit_reached"
    CAPTCHA_DETECTED = "captcha_detected"
    DOMAIN_EXCLUDED_CHALLENGE = "domain_excluded_after_automation_challenge"
    HTTP_429_RECEIVED = "429_received"
    DOMAIN_BACKOFF_SCHEDULED = "domain_backoff_scheduled"
    HTTP_5XX_RECEIVED = "5xx_received"
    CONSECUTIVE_5XX_REACHED = "three_consecutive_5xx_reached"
    RETRY_SCHEDULED = "retry_scheduled"
    RETRY_REJECTED = "retry_rejected"
    RETRY_LIMIT_REACHED = "retry_limit_reached"
    PAGE_TIMEOUT = "page_timeout"
    PARTIAL_TIMEOUT_PRESERVED = "partial_timeout_result_preserved"
    TRAFFIC_THRESHOLD_EXCEEDED = "traffic_threshold_exceeded"
    DOMAIN_PAUSED = "domain_paused"
    DOMAIN_MANUAL_REVIEW = "domain_sent_to_manual_review"
    EMERGENCY_STOP_ACTIVATED = "emergency_stop_activated"
    EMERGENCY_STOP_CLEARED = "emergency_stop_cleared"
    ACTIVE_VISIT_CANCELLED = "active_visit_cancelled"
    INTERNAL_SAFEGUARD_ERROR = "internal_safeguard_error"


def _redact_url(url: str | None) -> str | None:
    """Strip sensitive query parameters from a URL for safe logging."""
    if not url:
        return url
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    parsed = urlparse(url)
    if not parsed.query:
        return url
    params = parse_qs(parsed.query, keep_blank_values=True)
    redacted_parts: list[str] = []
    for key, values in params.items():
        if key.lower() in _SENSITIVE_QUERY_KEYS:
            redacted_parts.append(f"{key}=[REDACTED]")
        else:
            for v in values:
                redacted_parts.append(f"{key}={v}")
    new_query = "&".join(redacted_parts)
    return urlunparse(parsed._replace(query=new_query))


class SafeguardAuditLogger:
    """Thread-safe, append-only JSONL audit logger."""

    def __init__(self, log_path: Path | None = None, study_run_id: str | None = None) -> None:
        self._log_path = Path(log_path) if log_path else AUDIT_LOG_PATH
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._study_run_id = study_run_id or ""
        self._lock = threading.Lock()

    def log_event(
        self,
        event_type: str,
        *,
        worker_id: str = "",
        visit_id: str = "",
        domain: str = "",
        url: str | None = None,
        safeguard: str = "",
        action: str = "",
        reason_code: str = "",
        http_status: int | None = None,
        attempt_number: int | None = None,
        retry_number: int | None = None,
        daily_domain_count: int | None = None,
        time_since_last_visit_sec: float | None = None,
        active_global_count: int | None = None,
        active_domain_count: int | None = None,
        request_count: int | None = None,
        downloaded_bytes: int | None = None,
        backoff_duration_sec: float | None = None,
        error_category: str = "",
        queued_visits_cancelled: bool | None = None,
        domain_paused: bool | None = None,
        domain_excluded: bool | None = None,
        domain_manual_review: bool | None = None,
        partial_data_preserved: bool | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Write a single audit record and return its event_id."""
        event_id = uuid.uuid4().hex
        record: dict[str, Any] = {
            "event_id": event_id,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "study_run_id": self._study_run_id,
            "worker_id": worker_id,
            "visit_id": visit_id,
            "domain": domain,
            "url": _redact_url(url),
            "event_type": event_type,
            "safeguard": safeguard,
            "action": action,
            "reason_code": reason_code,
            "http_status": http_status,
            "attempt_number": attempt_number,
            "retry_number": retry_number,
            "daily_domain_count": daily_domain_count,
            "time_since_last_visit_sec": time_since_last_visit_sec,
            "active_global_count": active_global_count,
            "active_domain_count": active_domain_count,
            "request_count": request_count,
            "downloaded_bytes": downloaded_bytes,
            "backoff_duration_sec": backoff_duration_sec,
            "error_category": error_category,
            "queued_visits_cancelled": queued_visits_cancelled,
            "domain_paused": domain_paused,
            "domain_excluded": domain_excluded,
            "domain_manual_review": domain_manual_review,
            "partial_data_preserved": partial_data_preserved,
            "software_version": SOFTWARE_VERSION,
            "config_version": CONFIG_VERSION,
        }
        if extra:
            record["extra"] = extra

        # Remove None values for compactness
        record = {k: v for k, v in record.items() if v is not None}

        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

        return event_id

    def read_events(self, max_events: int = 0) -> list[dict]:
        """Read audit events from the log file. For testing and inspection."""
        if not self._log_path.is_file():
            return []
        events: list[dict] = []
        with open(self._log_path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    events.append(json.loads(stripped))
                    if max_events and len(events) >= max_events:
                        break
        return events
