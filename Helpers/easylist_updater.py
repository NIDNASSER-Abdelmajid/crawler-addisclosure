"""
Helpers/easylist_updater.py
---------------------------
Fetches ALL rules from the full EasyList (easylist.txt) and saves them to
two JSON files under ``resources/``:

  * ``resources/easylist_selectors.json``    -- CSS element-hiding selectors
  * ``resources/easylist_network_rules.json`` -- URL/domain blocking patterns

Run directly to trigger a one-off update:

    python -m Helpers.easylist_updater
    python Helpers/easylist_updater.py

JSON file structures::

    easylist_selectors.json:
    {
      "last_updated": "2026-03-10T13:30:00.123456",
      "source": "https://easylist.to/easylist/easylist.txt",
      "count": 45321,
      "selectors": [ "#AC_ad", ".ad-banner", ... ]
    }

    easylist_network_rules.json:
    {
      "last_updated": "2026-03-10T13:30:00.123456",
      "source": "https://easylist.to/easylist/easylist.txt",
      "count": 28741,
      "rules": [ "||ads.example.com^", "/banner*.gif", ... ]
    }
"""

import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

# Full EasyList URL -- the complete rule set, not just the general-hide subset
EASYLIST_URL = "https://easylist.to/easylist/easylist.txt"

_RESOURCES_DIR = Path(__file__).parent.parent / "resources"
_SELECTORS_FILE = _RESOURCES_DIR / "easylist_selectors.json"
_NETWORK_FILE   = _RESOURCES_DIR / "easylist_network_rules.json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_and_parse(url: str, timeout: int = 60) -> tuple[list[str], list[str]]:
    """
    Download the full EasyList file and split it into two rule sets.

    EasyList rule syntax handled::

        ! comment                    --> skip
        [Adblock Plus ...] header    --> skip
        @@...                        --> whitelist/exception rule  --> skip
        #@#selector                  --> element-hiding exception  --> skip
        ##selector                   --> generic CSS element-hiding
        domain[,domain]##selector    --> domain-specific CSS element-hiding
                                        (selector part extracted and kept)
        ##^...                       --> uBlock procedural filter  --> skip
        ||domain.com^                --> network domain-block rule
        /pattern                     --> network path-block rule
        other network rules          --> kept as network_rules

    Returns ``(selectors, network_rules)`` -- both as deduplicated lists.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "adgraph-updater/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace")

    selectors: list[str] = []
    network_rules: list[str] = []
    seen_sel: set[str] = set()
    seen_net: set[str] = set()

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("!") or line.startswith("["):
            continue  # blank, comment, or file header

        # Whitelist / exception rules -- skip entirely
        if line.startswith("@@"):
            continue

        # Element-hiding rules: generic (##sel) or domain-specific (dom##sel)
        if "##" in line:
            # Element-hiding *exception* (#@# prefix) -- skip
            if "#@#" in line:
                continue

            idx = line.find("##")
            sel = line[idx + 2:].strip()

            # Skip procedural/extended filters (uBlock-only syntax)
            if not sel or sel.startswith("^") or sel.startswith(":has(") or \
               sel.startswith(":not(:has") or sel.startswith(":matches-css"):
                continue
            # Skip bare structural element type selectors (html, body, head, etc.)
            _STRUCTURAL = frozenset({'html', 'head', 'body', 'script', 'style', 'meta', 'link', 'title', 'noscript'})
            if sel.lower() in _STRUCTURAL:
                continue

            if sel not in seen_sel:
                seen_sel.add(sel)
                selectors.append(sel)
            continue

        # Everything else without '#' is a network filtering rule.
        # Keep only lines that look like actionable URL patterns.
        if "||" in line or line.startswith("/") or (
            not line.startswith("#") and "." in line and len(line) < 200
        ):
            if line not in seen_net:
                seen_net.add(line)
                network_rules.append(line)

    return selectors, network_rules


def _save_selectors(selectors: list[str], source_url: str) -> None:
    """Persist CSS selectors to resources/easylist_selectors.json."""
    _RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_updated": datetime.now().isoformat(),
        "source":       source_url,
        "count":        len(selectors),
        "selectors":    selectors,
    }
    _SELECTORS_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _save_network_rules(rules: list[str], source_url: str) -> None:
    """Persist network blocking rules to resources/easylist_network_rules.json."""
    _RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_updated": datetime.now().isoformat(),
        "source":       source_url,
        "count":        len(rules),
        "rules":        rules,
    }
    _NETWORK_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def update_selectors(
    url: str = EASYLIST_URL,
    verbose: bool = True,
    timeout: int = 60,
) -> dict:
    """
    Fetch the full EasyList, parse all rule types, and save to resources/.

    Saves:
      * resources/easylist_selectors.json    -- CSS element-hiding selectors
      * resources/easylist_network_rules.json -- URL blocking patterns

    Returns a stats dict::

        {
            "selectors":     <int>,   # unique CSS selectors saved
            "network_rules": <int>,   # unique network rules saved
        }
    """
    if verbose:
        print(f"[EasyList] Fetching full EasyList from:\n  {url}", flush=True)

    try:
        selectors, network_rules = _fetch_and_parse(url, timeout=timeout)
    except Exception as exc:
        print(f"[EasyList] ERROR -- fetch failed: {exc}", file=sys.stderr)
        return {"selectors": 0, "network_rules": 0, "error": str(exc)}

    _save_selectors(selectors, url)
    _save_network_rules(network_rules, url)

    stats = {
        "selectors":     len(selectors),
        "network_rules": len(network_rules),
    }

    if verbose:
        print(f"[EasyList] CSS selectors:    {stats['selectors']:,}")
        print(f"[EasyList] Network rules:    {stats['network_rules']:,}")
        print(f"[EasyList] Saved selectors  -> {_SELECTORS_FILE}")
        print(f"[EasyList] Saved network    -> {_NETWORK_FILE}")

    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys as _sys
    _project_root = str(Path(__file__).parent.parent)
    if _project_root not in _sys.path:
        _sys.path.insert(0, _project_root)

    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch and update the EasyList rule stores"
    )
    parser.add_argument(
        "--url",
        default=EASYLIST_URL,
        help="EasyList source URL (default: full easylist.txt)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        metavar="SECS",
        help="HTTP request timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress progress output",
    )
    args = parser.parse_args()
    result = update_selectors(url=args.url, verbose=not args.quiet, timeout=args.timeout)
    _sys.exit(0 if "error" not in result else 1)