"""Helpers/crawl_context.py — Stable identifiers, phase tracking, and monotonic event counter.

Provides a frozen CrawlContext dataclass that is passed to all collectors
during ``init()``, giving every event a globally unique ``event_seq`` within
an attempt, stable identifiers for post-hoc correlation, and keyed-HMAC
capabilities for sensitive values.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "2.0.0"


class Phase:
    PAGE_LOAD = "page_load"
    PASSIVE_AD_DELIVERY = "passive_ad_delivery"
    DISCLOSURE_INTERACTION = "disclosure_interaction"
    ALL = {PAGE_LOAD, PASSIVE_AD_DELIVERY, DISCLOSURE_INTERACTION}


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


class PhaseTracker:
    """Thread-safe tracker for the current crawl lifecycle phase."""

    def __init__(self, initial_phase: str = Phase.PAGE_LOAD) -> None:
        self._lock = threading.Lock()
        self._phase = initial_phase

    @property
    def current(self) -> str:
        with self._lock:
            return self._phase

    def set(self, phase: str) -> None:
        with self._lock:
            self._phase = phase


class IdGenerator:
    """Thread-safe sequential identifier generator."""

    def __init__(self, prefix: str) -> None:
        self._lock = threading.Lock()
        self._counter = 0
        self._prefix = prefix

    def next(self) -> str:
        with self._lock:
            self._counter += 1
            return f"{self._prefix}_{self._counter:03d}"

    def current(self) -> int:
        with self._lock:
            return self._counter


def generate_document_id() -> str:
    """Generate a UUID for the current page-load document."""
    return str(uuid.uuid4())


def compute_keyed_hmac(key: bytes, value: str | bytes | None) -> str:
    """Compute a deterministic, privacy-safe HMAC-SHA256 hex digest for sensitive values."""
    if value is None:
        return ""
    if isinstance(value, str):
        payload = value.encode("utf-8", errors="replace")
    else:
        payload = bytes(value)
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


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
    hmac_key : bytes
        Secret key for privacy-safe keyed-HMAC hashing in this attempt.
    phase_tracker : PhaseTracker
        Lifecycle phase state tracker.
    candidate_id_gen : IdGenerator
        Sequential generator for ad candidate IDs.
    impression_id_gen : IdGenerator
        Sequential generator for ad impression IDs.
    disclosure_id_gen : IdGenerator
        Sequential generator for disclosure attempt IDs.
    """

    schema_version: str = SCHEMA_VERSION
    crawl_id: str = "crawl_default"
    website_id: str = "web_default"
    attempt_id: str = "att_001"
    attempt_number: int = 1
    retry_of_attempt_id: str | None = None
    root_attempt_id: str | None = None
    document_id: str = field(default_factory=generate_document_id)
    publisher_domain: str = "example.com"
    initial_url: str = "https://example.com"
    event_counter: EventCounter = field(default_factory=EventCounter, compare=False, hash=False)
    hmac_key: bytes = field(default_factory=lambda: os.urandom(32), compare=False, hash=False)
    phase_tracker: PhaseTracker = field(default_factory=PhaseTracker, compare=False, hash=False)
    candidate_id_gen: IdGenerator = field(default_factory=lambda: IdGenerator("cand"), compare=False, hash=False)
    impression_id_gen: IdGenerator = field(default_factory=lambda: IdGenerator("ad"), compare=False, hash=False)
    disclosure_id_gen: IdGenerator = field(default_factory=lambda: IdGenerator("disc_att"), compare=False, hash=False)
    api_event_id_gen: IdGenerator = field(default_factory=lambda: IdGenerator("api_evt"), compare=False, hash=False)
    phase_transitions: list[dict[str, Any]] = field(default_factory=list, compare=False, hash=False)
    url: str = ""

    def __post_init__(self) -> None:
        if self.url and (not self.initial_url or self.initial_url == "https://example.com"):
            object.__setattr__(self, "initial_url", self.url)
        elif self.initial_url and not self.url:
            object.__setattr__(self, "url", self.initial_url)
        # Record initial phase transition
        self.phase_transitions.append({
            "from_phase": None,
            "to_phase": self.phase_tracker.current,
            "timestamp_ms": int(time.time() * 1000),
        })

    def get_phase(self) -> str:
        return self.phase_tracker.current

    def set_phase(self, phase: str) -> None:
        old_phase = self.phase_tracker.current
        if old_phase != phase:
            self.phase_tracker.set(phase)
            self.phase_transitions.append({
                "from_phase": old_phase,
                "to_phase": phase,
                "timestamp_ms": int(time.time() * 1000),
            })

    def next_candidate_id(self) -> str:
        return self.candidate_id_gen.next()

    def next_impression_id(self) -> str:
        return self.impression_id_gen.next()

    def next_disclosure_attempt_id(self) -> str:
        return self.disclosure_id_gen.next()

    def next_api_event_id(self) -> str:
        return self.api_event_id_gen.next()

    def hmac_value(self, value: str | bytes | None) -> str:
        return compute_keyed_hmac(self.hmac_key, value)

    def get_phase_at_time(self, timestamp_ms: int | float | None) -> str:
        """Determine the crawl lifecycle phase active at a specific timestamp based on recorded transitions."""
        if timestamp_ms is None or not self.phase_transitions:
            return self.get_phase()
        matched_phase = self.phase_transitions[0].get("to_phase") or Phase.PAGE_LOAD
        for trans in self.phase_transitions:
            t_trans = trans.get("timestamp_ms", 0)
            if t_trans <= timestamp_ms:
                matched_phase = trans.get("to_phase", matched_phase)
            else:
                break
        return matched_phase

    def enrich_event(self, event_dict: dict[str, Any], timestamp_ms: int | None = None) -> dict[str, Any]:
        """Stamp an event with standard envelope fields: phase, timestamp_ms, event_seq, document_id, attempt_id."""
        if "event_seq" not in event_dict or event_dict["event_seq"] is None:
            event_dict["event_seq"] = self.event_counter.next()
        if "timestamp_ms" not in event_dict or event_dict["timestamp_ms"] is None:
            event_dict["timestamp_ms"] = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        # Always derive phase from the event-time state, not the final collector state
        if "phase" not in event_dict or not event_dict["phase"]:
            event_dict["phase"] = self.get_phase_at_time(event_dict["timestamp_ms"])
        if "document_id" not in event_dict:
            event_dict["document_id"] = self.document_id
        if "attempt_id" not in event_dict:
            event_dict["attempt_id"] = self.attempt_id
        return event_dict

