"""CAPTCHA and automation challenge detection for crawler safeguards.

Detection signals are configurable via safeguard_config.py.

Limitations documented inline:
- False positives: pages with "captcha" in text but no actual challenge.
- False negatives: novel challenge systems not covered by the signal list.
- No CAPTCHA solving, bypassing, or evasion is performed.
"""

from __future__ import annotations

from safeguard_config import (
    CAPTCHA_DOM_SELECTORS,
    CAPTCHA_TITLE_SIGNALS,
    CAPTCHA_URL_SIGNALS,
)


class CaptchaDetectionResult:
    """Holds the outcome of a CAPTCHA detection check."""

    __slots__ = ("detected", "signal_type", "signal_value")

    def __init__(self, detected: bool, signal_type: str = "", signal_value: str = "") -> None:
        self.detected = detected
        self.signal_type = signal_type
        self.signal_value = signal_value

    def __bool__(self) -> bool:
        return self.detected

    def __repr__(self) -> str:
        return f"CaptchaDetectionResult(detected={self.detected}, signal_type={self.signal_type!r}, signal_value={self.signal_value!r})"


async def detect_captcha(page) -> CaptchaDetectionResult:
    """Run all configured CAPTCHA detection checks against a Playwright page.

    Checks in order:
    1. Page title text — case-insensitive substring match against CAPTCHA_TITLE_SIGNALS.
    2. Page URL — substring match against CAPTCHA_URL_SIGNALS.
    3. DOM elements — CSS selector presence via CAPTCHA_DOM_SELECTORS.

    Possible false positives:
    - Pages that legitimately contain words like "captcha" in their content.
    - Privacy policy pages that mention CAPTCHA providers.

    Possible false negatives:
    - Novel or custom challenge systems not in the signal lists.
    - Challenges rendered entirely in iframes from unlisted domains.
    - JavaScript-only challenges that don't create detectable DOM elements.

    What happens after detection (handled by safeguard_engine.py):
    - Interaction with the page stops immediately.
    - The visit is closed without solving or bypassing the challenge.
    - Queued visits for the domain are cancelled.
    - The domain is added to a persistent exclusion list.
    - The detection evidence type is recorded in the audit log.
    """
    # 1. Title check
    try:
        title = (await page.title()).lower()
        for signal in CAPTCHA_TITLE_SIGNALS:
            if signal in title:
                return CaptchaDetectionResult(True, "title", signal)
    except Exception:
        pass

    # 2. URL check
    try:
        current_url = page.url.lower()
        for signal in CAPTCHA_URL_SIGNALS:
            if signal in current_url:
                return CaptchaDetectionResult(True, "url", signal)
    except Exception:
        pass

    # 3. DOM element check
    for selector in CAPTCHA_DOM_SELECTORS:
        try:
            element = await page.query_selector(selector)
            if element:
                return CaptchaDetectionResult(True, "dom_selector", selector)
        except Exception:
            continue

    return CaptchaDetectionResult(False)


def detect_captcha_in_response(status: int, url: str, headers: dict | None = None) -> CaptchaDetectionResult:
    """Synchronous check for CAPTCHA indicators in an HTTP response.

    Checks:
    - HTTP 403/503 with challenge URL patterns.
    - Response URL containing known CAPTCHA provider domains.
    """
    url_lower = url.lower() if url else ""

    # Check URL for challenge patterns
    for signal in CAPTCHA_URL_SIGNALS:
        if signal in url_lower:
            return CaptchaDetectionResult(True, "response_url", signal)

    # 403/503 combined with challenge indicators in URL
    if status in (403, 503):
        challenge_indicators = ["challenge", "captcha", "cdn-cgi/challenge"]
        for indicator in challenge_indicators:
            if indicator in url_lower:
                return CaptchaDetectionResult(True, "response_status_url", f"{status}+{indicator}")

    return CaptchaDetectionResult(False)
