import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from Helpers.easylist_selectors import load_selectors
from Helpers.easylist_updater import _fetch_and_parse, _save_selectors, _load_skip_rules


def test_easylist_json_has_skip_rules():
    """Verify that resources/easylist_selectors.json contains the skip_rules list."""
    json_path = Path(__file__).parent.parent / "resources" / "easylist_selectors.json"
    assert json_path.is_file(), "resources/easylist_selectors.json must exist"

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert "skip_rules" in data, "easylist_selectors.json must contain 'skip_rules' field"
    assert isinstance(data["skip_rules"], list)
    assert 'a[href][target*="blank"]' in data["skip_rules"]
    assert 'a[href][target*="blank"]' not in data["selectors"]


def test_load_selectors_filters_out_skip_rules():
    """Verify that load_selectors() excludes all skip_rules."""
    selectors = load_selectors()
    assert 'a[href][target*="blank"]' not in selectors
    assert len(selectors) > 1000


def test_fetch_and_parse_skips_skip_rules():
    """Verify that _fetch_and_parse filters out selectors in skip_rules."""
    mock_easylist_text = """
! EasyList test
##.advert-banner
##a[href][target*="blank"]
##div[class^="ad-slot"]
||adserver.example.com^
    """.strip().encode("utf-8")

    mock_resp = MagicMock()
    mock_resp.read.return_value = mock_easylist_text
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        selectors, net_rules = _fetch_and_parse(
            "https://fake.url/easylist.txt",
            skip_rules=['a[href][target*="blank"]'],
        )

    assert ".advert-banner" in selectors
    assert 'div[class^="ad-slot"]' in selectors
    assert 'a[href][target*="blank"]' not in selectors
    assert "||adserver.example.com^" in net_rules


def test_save_selectors_preserves_skip_rules(tmp_path):
    """Verify that _save_selectors preserves skip_rules in JSON output."""
    target_file = tmp_path / "easylist_selectors.json"
    with patch("Helpers.easylist_updater._SELECTORS_FILE", target_file), \
         patch("Helpers.easylist_updater._RESOURCES_DIR", tmp_path):
        _save_selectors(
            selectors=[".test-ad", "#banner"],
            source_url="https://test.url",
            skip_rules=['a[href][target*="blank"]', '.spurious-rule'],
        )

    assert target_file.is_file()
    saved = json.loads(target_file.read_text(encoding="utf-8"))
    assert saved["skip_rules"] == ['a[href][target*="blank"]', '.spurious-rule']
    assert saved["selectors"] == [".test-ad", "#banner"]
    assert saved["count"] == 2
