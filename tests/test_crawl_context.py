import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
import threading
import uuid
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Helpers.crawl_context import (
    SCHEMA_VERSION,
    CrawlContext,
    EventCounter,
    generate_document_id,
)


def test_event_counter_sequential():
    """EventCounter returns 1-based sequential integers."""
    counter = EventCounter()
    assert counter.current() == 0
    assert counter.next() == 1
    assert counter.next() == 2
    assert counter.next() == 3
    assert counter.current() == 3


def test_event_counter_thread_safety():
    """EventCounter produces strictly unique IDs across concurrent threads."""
    counter = EventCounter()
    results = []
    thread_count = 100
    lock = threading.Lock()

    def _worker():
        val = counter.next()
        with lock:
            results.append(val)

    threads = [threading.Thread(target=_worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == thread_count
    assert len(set(results)) == thread_count
    assert min(results) == 1
    assert max(results) == thread_count


def test_generate_document_id():
    """generate_document_id creates valid UUID strings."""
    doc_id1 = generate_document_id()
    doc_id2 = generate_document_id()

    assert isinstance(doc_id1, str)
    assert doc_id1 != doc_id2

    # Must be parsable as valid UUID
    parsed1 = uuid.UUID(doc_id1)
    parsed2 = uuid.UUID(doc_id2)
    assert str(parsed1) == doc_id1
    assert str(parsed2) == doc_id2


def test_crawl_context_immutability():
    """CrawlContext is frozen and cannot be mutated."""
    counter = EventCounter()
    context = CrawlContext(
        schema_version=SCHEMA_VERSION,
        crawl_id="crawl_001",
        website_id="web_123",
        attempt_id="att_abc",
        attempt_number=1,
        document_id=str(uuid.uuid4()),
        publisher_domain="example.com",
        initial_url="https://example.com",
        event_counter=counter,
    )

    assert context.schema_version == "2.0.0"
    assert context.publisher_domain == "example.com"

    with pytest.raises(FrozenInstanceError):
        context.attempt_number = 2  # type: ignore

    with pytest.raises(FrozenInstanceError):
        context.publisher_domain = "other.com"  # type: ignore
