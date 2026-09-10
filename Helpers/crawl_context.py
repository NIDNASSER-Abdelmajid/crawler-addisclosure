"""Helpers/crawl_context.py — Stable identifiers and monotonic event counter.

Provides a frozen CrawlContext dataclass that is passed to all collectors
during ``init()``, giving every event a globally unique ``event_seq`` within
an attempt and stable identifiers for post-hoc correlation.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

SCHEMA_VERSION = "2.0.0"


class EventCounter:
    """Thread-safe monotonic counter for globally ordering events within an attempt."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: int = 0

    def next(self) -> int:
        """Return the next event sequence number (1-based)."""
        with self._lock:
            self._value += 1
            return self._value

    def current(self) -> int:
        """Return the current counter value without incrementing."""
        with self._lock:
            return self._value


def generate_document_id() -> str:
    """Generate a UUID for the current page-load document."""
    return str(uuid.uuid4())


@dataclass(frozen=True)
class CrawlContext:
    """Immutable context shared across all collectors during a single crawl attempt.

    Attributes
    ----------
    schema_version : str
        Output schema version (e.g. ``"2.0.0"``).
    crawl_id : str
        Identifier for the overall crawl batch.
    website_id : str
        Deterministic identifier for the target website.
    attempt_id : str
        Unique identifier for this specific attempt.
    attempt_number : int
        Sequential attempt number (1-based).
    document_id : str
        UUID identifying the specific page-load document.
    publisher_domain : str
        Registrable domain of the publisher (e.g. ``"yahoo.com"``).
    initial_url : str
        The seed URL that was requested.
    event_counter : EventCounter
        Shared monotonic counter for event ordering.
    """

    schema_version: str
    crawl_id: str
    website_id: str
    attempt_id: str
    attempt_number: int
    document_id: str
    publisher_domain: str
    initial_url: str
    event_counter: EventCounter = field(compare=False, hash=False)
