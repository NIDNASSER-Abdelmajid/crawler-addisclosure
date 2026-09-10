"""Centralized configuration for all crawler ethical safeguards.

Every threshold, path, and tunable parameter lives here so that
configuration changes require editing exactly one file.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Domain Visit Rate
# ---------------------------------------------------------------------------
DOMAIN_VISIT_INTERVAL_SECONDS: int = 60
"""Minimum seconds between two page visits to the same registrable domain."""

# ---------------------------------------------------------------------------
# 2. Daily Domain Limit
# ---------------------------------------------------------------------------
MAX_DOMAIN_VISITS_PER_UTC_DAY: int = 20
"""Maximum page visits per domain per UTC calendar day."""

# ---------------------------------------------------------------------------
# 3. Global Crawler Concurrency
# ---------------------------------------------------------------------------
MAX_SIMULTANEOUS_CRAWLERS: int = 6
"""Maximum active page visits across the entire crawler system."""

# ---------------------------------------------------------------------------
# 4. Per-Domain Concurrency
# ---------------------------------------------------------------------------
MAX_ACTIVE_CRAWLERS_PER_DOMAIN: int = 1
"""Maximum simultaneous active page visits per registrable domain."""

# ---------------------------------------------------------------------------
# 5. CAPTCHA / Automation Challenge
# ---------------------------------------------------------------------------
CAPTCHA_REASON_CODE: str = "CAPTCHA_OR_AUTOMATION_CHALLENGE"
"""Reason code written to the audit log when a CAPTCHA is detected."""

# CAPTCHA page-title signals (case-insensitive substring match)
CAPTCHA_TITLE_SIGNALS: list[str] = [
    "access is temporarily restricted",
    "access denied",
    "just a moment",
    "checking your browser",
    "enable javascript and cookies",
    "why do i have to complete a captcha",
    "please enable cookies",
    "attention required",
    "are you a robot",
    "captcha",
    "security check",
    "verify you are human",
    "bot verification",
    "please verify",
    "one more step",
]

# CAPTCHA challenge URL substrings
CAPTCHA_URL_SIGNALS: list[str] = [
    "challenges.cloudflare.com",
    "geo.captcha-delivery.com",
    "arkoselabs.com",
    "recaptcha/api",
    "hcaptcha.com",
    "captcha-api",
    "/cdn-cgi/challenge-platform/",
]

# CAPTCHA DOM selectors (CSS)
CAPTCHA_DOM_SELECTORS: list[str] = [
    "#captcha",
    ".g-recaptcha",
    ".h-captcha",
    "#cf-challenge-running",
    "#challenge-running",
    'iframe[src*="hcaptcha"]',
    'iframe[src*="recaptcha"]',
    "#px-captcha",
    ".captcha-container",
    "#challenge-form",
    "#cf-please-wait",
]

# ---------------------------------------------------------------------------
# 6. HTTP 429 Backoff
# ---------------------------------------------------------------------------
MAX_429_BACKOFF_SECONDS: int | None = None
"""Maximum backoff after a 429 response.  RESEARCH_TEAM_DECISION — must be
set to an integer before a production run.  The crawler validates this at
startup when --production is specified."""

# ---------------------------------------------------------------------------
# 7. Repeated Server Errors
# ---------------------------------------------------------------------------
MAX_CONSECUTIVE_DOMAIN_5XX: int = 3
"""Stop crawling a domain after this many consecutive main-page 5xx responses."""

# ---------------------------------------------------------------------------
# 8. Retry Limit
# ---------------------------------------------------------------------------
MAX_RETRIES_PER_PAGE: int = 2
"""Maximum retry attempts per failed page (original attempt + this many retries)."""

# ---------------------------------------------------------------------------
# 9. Page-Visit Timeout
# ---------------------------------------------------------------------------
MAX_PAGE_VISIT_SECONDS: int = 180
"""Hard upper bound for a single page visit (navigation + collection + cleanup)."""

# ---------------------------------------------------------------------------
# 10. Unexpected Traffic Limits
# ---------------------------------------------------------------------------
MAX_REQUESTS_PER_VISIT: int | None = None
"""Maximum network requests per page visit.  PILOT_DEFINED — must be set
before a production run."""

MAX_DOWNLOADED_BYTES_PER_VISIT: int | None = None
"""Maximum downloaded bytes per page visit.  PILOT_DEFINED — must be set
before a production run."""

# ---------------------------------------------------------------------------
# Paths & metadata
# ---------------------------------------------------------------------------
SAFEGUARD_STATE_DB_PATH: Path = Path("resources/safeguard_state.db")
"""SQLite database that persists counters, exclusions, and emergency state."""

AUDIT_LOG_PATH: Path = Path("resources/safeguard_audit.jsonl")
"""Append-only structured JSONL audit log for safeguard events."""

SOFTWARE_VERSION: str = "1.0.0"
"""Crawler software version recorded in every audit entry."""

CONFIG_VERSION: str = "1.0.0"
"""Safeguard configuration version recorded in every audit entry."""

# ---------------------------------------------------------------------------
# Non-retryable failure reasons
# ---------------------------------------------------------------------------
NON_RETRYABLE_REASONS: frozenset[str] = frozenset({
    CAPTCHA_REASON_CODE,
    "DOMAIN_EXCLUDED",
    "DOMAIN_PAUSED",
    "DOMAIN_MANUAL_REVIEW",
    "EMERGENCY_STOP",
    "DAILY_LIMIT_EXHAUSTED",
    "CONSECUTIVE_5XX_LIMIT",
    "TRAFFIC_THRESHOLD_EXCEEDED",
    "PERMANENT_FAILURE",
})
"""Stop reasons that must never trigger an automatic retry."""

# ---------------------------------------------------------------------------
# Jitter bounds for 429 backoff
# ---------------------------------------------------------------------------
BACKOFF_JITTER_MIN_SECONDS: float = 1.0
BACKOFF_JITTER_MAX_SECONDS: float = 10.0
"""Bounded randomized jitter added to each exponential backoff delay."""
