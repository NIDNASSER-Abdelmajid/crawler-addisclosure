import json
import sys
from pathlib import Path
import uuid
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Helpers.schema_validator import SchemaValidationError, validate_result


@pytest.fixture
def valid_v2_result():
    return {
        "schema_version": "2.0.0",
        "document_id": str(uuid.uuid4()),
        "initialUrl": "https://example.com",
        "finalUrl": "https://example.com/home",
        "successful": "true",
        "testStarted": 1700000000,
        "testFinished": 1700000030,
        "data": {
            "RequestCollector": [
                {
                    "url": "https://example.com/app.js",
                    "method": "GET",
                    "type": "Script",
                    "event_seq": 1,
                    "document_id": str(uuid.uuid4()),
                }
            ],
            "APICallCollector": {
                "savedCalls": [
                    {
                        "source": "https://example.com/app.js",
                        "description": "window.localStorage",
                        "event_seq": 2,
                        "timestamp_ms": 1700000005000,
                    }
                ],
                "callStats": {},
            },
            "FingerprintCollector": {
                "savedCalls": [
                    {
                        "source": "https://example.com/app.js",
                        "description": "CanvasRenderingContext2D.toDataURL",
                        "event_seq": 3,
                        "timestamp_ms": 1700000006000,
                    }
                ],
                "callStats": {},
            },
            "CookieCollector": [
                {
                    "name": "session_id",
                    "domain": "example.com",
                    "first_party": True,
                    "event_seq": 4,
                }
            ],
            "TargetCollector": [
                {
                    "type": "page",
                    "url": "https://example.com",
                    "event_seq": 5,
                    "discovered_at_ms": 1700000001000,
                }
            ],
            "ScreenshotCollector": [
                {
                    "screenshot": "screenshot_abc.jpg",
                    "filename": "screenshot_abc.jpg",
                    "event_seq": 6,
                }
            ],
        },
    }


def test_valid_v2_result_passes(valid_v2_result):
    res = validate_result(valid_v2_result, raise_on_error=True)
    assert res["valid"] is True
    assert len(res["errors"]) == 0
    assert res["event_count"] == 6


def test_valid_v2_result_file_passes(valid_v2_result, tmp_path):
    file_path = tmp_path / "result.json"
    file_path.write_text(json.dumps(valid_v2_result), encoding="utf-8")

    res = validate_result(file_path, raise_on_error=True)
    assert res["valid"] is True
    assert res["event_count"] == 6


def test_missing_schema_version_fails(valid_v2_result):
    del valid_v2_result["schema_version"]
    res = validate_result(valid_v2_result)
    assert res["valid"] is False
    assert any("schema_version" in e for e in res["errors"])

    with pytest.raises(SchemaValidationError):
        validate_result(valid_v2_result, raise_on_error=True)


def test_wrong_schema_version_fails(valid_v2_result):
    valid_v2_result["schema_version"] = "1.0.0"
    res = validate_result(valid_v2_result)
    assert res["valid"] is False
    assert any("schema_version" in e for e in res["errors"])


def test_invalid_document_id_fails(valid_v2_result):
    valid_v2_result["document_id"] = "not-a-valid-uuid"
    res = validate_result(valid_v2_result)
    assert res["valid"] is False
    assert any("document_id" in e for e in res["errors"])


def test_duplicate_event_seq_fails(valid_v2_result):
    # Set APICallCollector event_seq to 1 (same as RequestCollector)
    valid_v2_result["data"]["APICallCollector"]["savedCalls"][0]["event_seq"] = 1
    res = validate_result(valid_v2_result)
    assert res["valid"] is False
    assert any("Duplicate event_seq 1" in e for e in res["errors"])


def test_invalid_first_party_flag_fails(valid_v2_result):
    valid_v2_result["data"]["CookieCollector"][0]["first_party"] = "yes"  # Not a boolean
    res = validate_result(valid_v2_result)
    assert res["valid"] is False
    assert any("first_party must be bool" in e for e in res["errors"])
