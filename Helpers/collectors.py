"""
Helpers/collectors.py
----------------------
Central registry mapping user-friendly short names to canonical collector
class names.  Import this wherever collector names need to be resolved.
"""

# Short alias → canonical COLLECTOR_NAME
ALIASES: dict[str, str] = {
    "ads":        "AdCollector",
    "cookies":    "CookieCollector",
    "cookiepopup": "CookiePopupsCollector",
    "cookiepopups": "CookiePopupsCollector",
    "requests":   "RequestCollector",
    "screenshot": "ScreenshotCollector",
    "cmp":        "CookiePopupsCollector",
    "api":        "APICallCollector",
    "apis":       "APICallCollector",
    "apicall":    "APICallCollector",
    "apicalls":   "APICallCollector",
    "fingerprint": "FingerprintCollector",
    "fingerprints": "FingerprintCollector",
    "target":      "TargetCollector",
    "targets":     "TargetCollector",
    "inclusiontree": "InclusionTreeCollector",
}

# Every accepted name (canonical + aliases), for argparse choices validation
ALL_CHOICES: list[str] = list(ALIASES.values()) + list(ALIASES.keys())

# Canonical names only (preserves insertion order)
ALL_COLLECTORS: list[str] = [
    "AdCollector",
    "RequestCollector",
    "CookieCollector",
    "CookiePopupsCollector",
    "ScreenshotCollector",
    "APICallCollector",
    "FingerprintCollector",
    "TargetCollector",
    "InclusionTreeCollector",
]


def resolve(name: str) -> str:
    """Return the canonical collector name, resolving any short alias."""
    return ALIASES.get(name.lower(), name)


def resolve_all(names: list[str]) -> list[str]:
    """Resolve a list of names (aliases or canonical), deduplicating while preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for n in names:
        canonical = resolve(n)
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result
