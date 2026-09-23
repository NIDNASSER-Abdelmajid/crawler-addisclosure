import json
import logging
import time

from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from playwright.async_api import Page


# ============================================================
# Configuration
# ============================================================

_CONFIG_PATH = (
    Path(__file__).parent.parent
    / "resources"
    / "ad_disclosure.json"
)

DISCLOSURE_SETTLE_MS = 300
DISCLOSURE_POLL_MS = 50
DISCLOSURE_STABILITY_MS = 150
DISCLOSURE_CLICK_TIMEOUT_MS = 1000
DISCLOSURE_CONTENT_STABILITY_TIMEOUT_MS = 1500
DISCLOSURE_CONTENT_POLL_MS = 100
DISCLOSURE_STABLE_SAMPLES = 2


def _load_ad_disclosure_config() -> tuple[list[str], list[str]]:
    """Load disclosure link texts and recognized hosts."""

    if not _CONFIG_PATH.is_file():
        return [], []

    try:
        data = json.loads(
            _CONFIG_PATH.read_text(encoding="utf-8")
        )

        links = data.get(
            "adDisclosureOutLinkTexts",
            [],
        )

        hosts = data.get(
            "adDisclosureHosts",
            [],
        )

        if not isinstance(links, list):
            links = []

        if not isinstance(hosts, list):
            hosts = []

        normalized_links = [
            str(item).strip()
            for item in links
            if str(item).strip()
        ]

        normalized_hosts = [
            str(item)
            .strip()
            .lower()
            .lstrip(".")
            .rstrip(".")
            for item in hosts
            if str(item).strip()
        ]

        return normalized_links, normalized_hosts

    except Exception:
        return [], []


AD_DISC_LINKS_TO_COLLECT, AD_DISCLOSURE_LINKS = (
    _load_ad_disclosure_config()
)


# ============================================================
# Disclosure sections
# ============================================================

DISCLOSURE_SECTION_LABELS: dict[str, list[str]] = {
    "why_this_ad": [
        "why you're seeing this ad",
        "why you are seeing this ad",
        "why this ad",
        "why you see this ad",
    ],
    "about_advertiser": [
        "about this advertiser",
        "about the advertiser",
        "advertiser information",
    ],
}


# ============================================================
# Results / diagnostics
# ============================================================


@dataclass
class DisclosureSectionResult:
    section: str
    detected: bool = False
    expanded: bool = False
    already_expanded: bool = False
    method: str | None = None
    label: str | None = None
    error: str | None = None


@dataclass
class DisclosureDiagnostics:
    ready: bool = False
    ready_latency_ms: int | None = None
    ready_timeout: bool = False

    sections_detected: int = 0
    sections_expanded: int = 0

    content_stable: bool = False
    content_stability_latency_ms: int | None = None

    page_text_length: int = 0

    screenshot_success: bool = False
    extraction_success: bool = False


# ============================================================
# URL helpers
# ============================================================


def _is_disclosure_host(
    hostname: str,
) -> bool:
    """
    Check whether a hostname belongs to a configured disclosure domain.

    Allows:
        google.com
        ads.google.com

    Rejects:
        evilgoogle.com
    """

    host = (
        hostname
        or ""
    ).strip().lower().rstrip(".")

    if not host:
        return False

    for allowed in AD_DISCLOSURE_LINKS:

        allowed = (
            str(allowed)
            .strip()
            .lower()
            .lstrip(".")
            .rstrip(".")
        )

        if not allowed:
            continue

        if host == allowed:
            return True

        if host.endswith("." + allowed):
            return True

    return False


def _matches_expected_href(
    page_url: str,
    expected_href: str | None,
) -> bool:
    """Validate the captured disclosure URL."""

    if not expected_href:
        return False

    try:
        expected = urlparse(expected_href)
        current = urlparse(page_url)

    except Exception:
        return False

    expected_host = (
        expected.hostname or ""
    ).lower()

    current_host = (
        current.hostname or ""
    ).lower()

    if not expected_host or not current_host:
        return False

    # --------------------------------------------------------
    # Host
    # --------------------------------------------------------

    if expected_host != current_host:

        if not (
            _is_disclosure_host(expected_host)
            and _is_disclosure_host(current_host)
        ):
            return False

    # --------------------------------------------------------
    # Path
    # --------------------------------------------------------

    expected_path = (
        (expected.path or "/").rstrip("/")
        or "/"
    )

    current_path = (
        (current.path or "/").rstrip("/")
        or "/"
    )

    path_matches = (
        current_path == expected_path
        or current_path.startswith(
            expected_path + "/"
        )
    )

    if not path_matches:

        both_disclosure = (
            _is_disclosure_host(expected_host)
            and _is_disclosure_host(current_host)
        )

        if not both_disclosure:
            return False

        disclosure_keywords = {
            "privacy",
            "adinfo",
            "adchoice",
            "adchoices",
            "whythisad",
            "why-this-ad",
            "advertiser",
            "aboutourads",
            "about-our-ads",
            "aboutads",
            "optout",
            "transparency",
            "myadcenter",
        }

        current_lower = (
            current_path.lower()
        )

        expected_lower = (
            expected_path.lower()
        )

        current_has_keyword = any(
            keyword in current_lower
            for keyword in disclosure_keywords
        )

        expected_has_keyword = any(
            keyword in expected_lower
            for keyword in disclosure_keywords
        )

        if not (
            current_has_keyword
            and (
                expected_has_keyword
                or expected_lower in {"/", ""}
            )
        ):
            return False

    # --------------------------------------------------------
    # Query
    # --------------------------------------------------------

    if expected.query:

        expected_query = parse_qsl(
            expected.query,
            keep_blank_values=True,
        )

        current_query = parse_qsl(
            current.query,
            keep_blank_values=True,
        )

        current_values: dict[
            str,
            set[str],
        ] = {}

        for key, value in current_query:

            current_values.setdefault(
                key,
                set(),
            ).add(value)

        for key, value in expected_query:

            if (
                key in current_values
                and value
                not in current_values[key]
            ):
                return False

    return True


# ============================================================
# Page helpers
# ============================================================


async def _page_is_closed(
    page: Page,
) -> bool:

    try:
        return page.is_closed()

    except Exception:
        return True


# ============================================================
# Disclosure out-links
# ============================================================


async def _get_ad_disclosure_out_links(
    disclosure_page: Page,
) -> list[dict]:
    """
    Extract configured disclosure links.

    Matching is normalized and case-insensitive.
    """

    try:

        result = await disclosure_page.evaluate(
            """
            (selectors) => {

                const normalize = (value) =>
                    (value || '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .toLowerCase();

                const expected = new Set(
                    (selectors || [])
                        .map(normalize)
                        .filter(Boolean)
                );

                const results = [];
                const seen = new Set();

                for (
                    const link
                    of document.querySelectorAll('a[href]')
                ) {

                    const text = (
                        link.innerText ||
                        link.textContent ||
                        ''
                    ).trim();

                    const normalizedText =
                        normalize(text);

                    if (!normalizedText) {
                        continue;
                    }

                    if (
                        !expected.has(
                            normalizedText
                        )
                    ) {
                        continue;
                    }

                    const href = link.href;

                    if (!href) {
                        continue;
                    }

                    const key =
                        normalizedText +
                        '|' +
                        href;

                    if (seen.has(key)) {
                        continue;
                    }

                    seen.add(key);

                    results.push({
                        text,
                        href
                    });
                }

                return results;
            }
            """,
            AD_DISC_LINKS_TO_COLLECT,
        )

        if isinstance(result, list):
            return result

    except Exception:
        pass

    return []


# ============================================================
# Semantic DOM detection
# ============================================================


async def _detect_disclosure_sections(
    page: Page,
) -> dict[str, dict]:
    """
    Detect disclosure sections using:

        - aria-label
        - aria-expanded
        - button
        - role="button"

    No XPath.
    No c-wiz.
    No positional DOM assumptions.
    """

    try:

        result = await page.evaluate(
            """
            (sectionLabels) => {

                const normalize = (value) =>
                    (value || '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .toLowerCase();

                const isVisible = (el) => {

                    if (
                        !el ||
                        !el.isConnected
                    ) {
                        return false;
                    }

                    const rect =
                        el.getBoundingClientRect();

                    if (
                        rect.width <= 0 ||
                        rect.height <= 0
                    ) {
                        return false;
                    }

                    const style =
                        window.getComputedStyle(el);

                    return (
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        Number(style.opacity) !== 0 &&
                        style.pointerEvents !== 'none'
                    );
                };

                const results = {};

                for (
                    const section
                    of Object.keys(sectionLabels)
                ) {

                    results[section] = {
                        found: false,
                        expanded: false,
                        label: null
                    };
                }

                const candidates =
                    document.querySelectorAll(
                        [
                            'button',
                            '[role="button"]',
                            '[aria-label][aria-expanded]'
                        ].join(',')
                    );

                const processed =
                    new Set();

                for (
                    const candidate
                    of candidates
                ) {

                    if (
                        processed.has(candidate) ||
                        !isVisible(candidate)
                    ) {
                        continue;
                    }

                    processed.add(candidate);

                    /*
                     * Prefer aria-label because it describes
                     * the control itself.
                     */
                    const rawLabel =
                        candidate.getAttribute(
                            'aria-label'
                        ) ||
                        candidate.innerText ||
                        candidate.textContent ||
                        '';

                    const normalized =
                        normalize(rawLabel);

                    if (!normalized) {
                        continue;
                    }

                    for (
                        const [
                            section,
                            labels
                        ]
                        of Object.entries(
                            sectionLabels
                        )
                    ) {

                        if (
                            results[section].found
                        ) {
                            continue;
                        }

                        const matched =
                            labels.some(
                                expected =>
                                    normalized.includes(
                                        normalize(
                                            expected
                                        )
                                    )
                            );

                        if (!matched) {
                            continue;
                        }

                        results[section] = {
                            found: true,

                            expanded:
                                candidate
                                    .getAttribute(
                                        'aria-expanded'
                                    )
                                === 'true',

                            label:
                                rawLabel.trim()
                        };
                    }
                }

                return results;
            }
            """,
            DISCLOSURE_SECTION_LABELS,
        )

        if isinstance(result, dict):
            return result

    except Exception:
        pass

    return {}


# ============================================================
# Find disclosure control
# ============================================================


async def _find_disclosure_button(
    page: Page,
    section_name: str,
):
    """
    Find one disclosure control.

    Priority:

        1. aria-label + aria-expanded
        2. button[aria-label]
        3. role=button[aria-label]
        4. Playwright accessible button name
        5. role=button + text

    No XPath.
    """

    labels = (
        DISCLOSURE_SECTION_LABELS.get(
            section_name,
            [],
        )
    )

    for label in labels:

        label_lower = (
            label
            .strip()
            .lower()
        )

        # ====================================================
        # 1. aria-label + aria-expanded
        # ====================================================

        locator = page.locator(
            '[aria-label][aria-expanded]'
        )

        count = await locator.count()

        for index in range(
            min(count, 30)
        ):

            candidate = (
                locator.nth(index)
            )

            try:

                if not await candidate.is_visible():
                    continue

                aria_label = (
                    await candidate.get_attribute(
                        "aria-label"
                    )
                    or ""
                )

                if (
                    label_lower
                    in aria_label.lower()
                ):

                    return (
                        candidate,
                        aria_label,
                        "aria_label_expanded",
                    )

            except Exception:
                continue

        # ====================================================
        # 2. button[aria-label]
        # ====================================================

        locator = page.locator(
            'button[aria-label]'
        )

        count = await locator.count()

        for index in range(
            min(count, 30)
        ):

            candidate = (
                locator.nth(index)
            )

            try:

                if not await candidate.is_visible():
                    continue

                aria_label = (
                    await candidate.get_attribute(
                        "aria-label"
                    )
                    or ""
                )

                if (
                    label_lower
                    in aria_label.lower()
                ):

                    return (
                        candidate,
                        aria_label,
                        "button_aria_label",
                    )

            except Exception:
                continue

        # ====================================================
        # 3. role=button + aria-label
        # ====================================================

        locator = page.locator(
            '[role="button"][aria-label]'
        )

        count = await locator.count()

        for index in range(
            min(count, 30)
        ):

            candidate = (
                locator.nth(index)
            )

            try:

                if not await candidate.is_visible():
                    continue

                aria_label = (
                    await candidate.get_attribute(
                        "aria-label"
                    )
                    or ""
                )

                if (
                    label_lower
                    in aria_label.lower()
                ):

                    return (
                        candidate,
                        aria_label,
                        "role_button_aria_label",
                    )

            except Exception:
                continue

        # ====================================================
        # 4. Playwright accessible button name
        # ====================================================

        try:

            locator = (
                page.get_by_role(
                    "button",
                    name=label,
                    exact=False,
                )
            )

            count = await locator.count()

            for index in range(
                min(count, 10)
            ):

                candidate = (
                    locator.nth(index)
                )

                try:

                    if await candidate.is_visible():

                        return (
                            candidate,
                            label,
                            "accessible_button",
                        )

                except Exception:
                    continue

        except Exception:
            pass

        # ====================================================
        # 5. role=button + text
        # ====================================================

        try:

            locator = (
                page.locator(
                    '[role="button"]'
                )
                .filter(
                    has_text=label
                )
            )

            count = await locator.count()

            for index in range(
                min(count, 10)
            ):

                candidate = (
                    locator.nth(index)
                )

                try:

                    if await candidate.is_visible():

                        return (
                            candidate,
                            label,
                            "role_button_text",
                        )

                except Exception:
                    continue

        except Exception:
            pass

    return None, None, None


# ============================================================
# Readiness
# ============================================================


async def wait_for_disclosure_ready(
    page: Page,
    logger: logging.Logger | None = None,
    timeout_ms: int = 3000,
) -> tuple[bool, int]:
    """
    Wait until at least one disclosure section is detected and
    the detected set remains stable briefly.

    No window.stop().
    """

    start = time.monotonic()

    previous_signature: (
        tuple[str, ...] | None
    ) = None

    stable_since: (
        float | None
    ) = None

    while True:

        elapsed_ms = int(
            (
                time.monotonic()
                - start
            )
            * 1000
        )

        if elapsed_ms >= timeout_ms:

            if logger:
                logger.info(
                    "[AdDisclosure] Readiness "
                    "timeout after %dms.",
                    elapsed_ms,
                )

            return False, elapsed_ms

        if await _page_is_closed(page):
            return False, elapsed_ms

        sections = (
            await _detect_disclosure_sections(
                page
            )
        )

        signature = tuple(
            sorted(
                section
                for section, data
                in sections.items()
                if data.get("found")
            )
        )

        if signature:

            if (
                signature
                == previous_signature
            ):

                if stable_since is None:
                    stable_since = (
                        time.monotonic()
                    )

                stable_ms = (
                    time.monotonic()
                    - stable_since
                ) * 1000

                if (
                    stable_ms
                    >= DISCLOSURE_STABILITY_MS
                ):

                    elapsed_ms = int(
                        (
                            time.monotonic()
                            - start
                        )
                        * 1000
                    )

                    if logger:
                        logger.info(
                            "[AdDisclosure] UI ready "
                            "after %dms. Sections: %s",
                            elapsed_ms,
                            ", ".join(
                                signature
                            ),
                        )

                    return (
                        True,
                        elapsed_ms,
                    )

            else:

                previous_signature = (
                    signature
                )

                stable_since = (
                    time.monotonic()
                )

        else:

            previous_signature = None
            stable_since = None

        try:

            await page.wait_for_timeout(
                DISCLOSURE_POLL_MS
            )

        except Exception:

            return (
                False,
                elapsed_ms,
            )


wait_until_buttons_clickable_and_stop_loading = wait_for_disclosure_ready


# ============================================================
# JavaScript semantic click fallback
# ============================================================


async def _click_section_with_js(
    page: Page,
    section_name: str,
) -> DisclosureSectionResult:
    """
    Semantic JS fallback.

    Only searches interactive semantic controls.

    No XPath.
    No c-wiz.
    """

    result = DisclosureSectionResult(
        section=section_name
    )

    labels = (
        DISCLOSURE_SECTION_LABELS.get(
            section_name,
            [],
        )
    )

    try:

        data = await page.evaluate(
            """
            (labels) => {

                const normalize = (value) =>
                    (value || '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .toLowerCase();

                const expected =
                    labels.map(normalize);

                const isVisible = (el) => {

                    if (
                        !el ||
                        !el.isConnected
                    ) {
                        return false;
                    }

                    const rect =
                        el.getBoundingClientRect();

                    if (
                        rect.width <= 0 ||
                        rect.height <= 0
                    ) {
                        return false;
                    }

                    const style =
                        window.getComputedStyle(el);

                    return (
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        Number(style.opacity) !== 0 &&
                        style.pointerEvents !== 'none'
                    );
                };

                const candidates =
                    document.querySelectorAll(
                        [
                            '[aria-label][aria-expanded]',
                            'button[aria-label]',
                            '[role="button"][aria-label]',
                            'button',
                            '[role="button"]'
                        ].join(',')
                    );

                const processed =
                    new Set();

                for (
                    const el
                    of candidates
                ) {

                    if (
                        processed.has(el) ||
                        !isVisible(el)
                    ) {
                        continue;
                    }

                    processed.add(el);

                    const rawLabel =
                        el.getAttribute(
                            'aria-label'
                        ) ||
                        el.innerText ||
                        el.textContent ||
                        '';

                    const normalized =
                        normalize(rawLabel);

                    if (!normalized) {
                        continue;
                    }

                    const matched =
                        expected.some(
                            label =>
                                normalized.includes(
                                    label
                                )
                        );

                    if (!matched) {
                        continue;
                    }

                    const state =
                        el.getAttribute(
                            'aria-expanded'
                        );

                    if (
                        state === 'true'
                    ) {

                        return {
                            detected: true,
                            expanded: true,
                            alreadyExpanded: true,
                            label:
                                rawLabel.trim()
                        };
                    }

                    try {

                        el.scrollIntoView({
                            block: 'center',
                            inline: 'center'
                        });

                    } catch (_) {}

                    try {

                        el.click();

                    } catch (error) {

                        return {
                            detected: true,
                            expanded: false,
                            alreadyExpanded: false,
                            label:
                                rawLabel.trim(),
                            error:
                                String(error)
                        };
                    }

                    const newState =
                        el.getAttribute(
                            'aria-expanded'
                        );

                    return {
                        detected: true,

                        /*
                         * If aria-expanded doesn't exist,
                         * successful click is considered enough.
                         */
                        expanded:
                            newState === null ||
                            newState === 'true',

                        alreadyExpanded:
                            false,

                        label:
                            rawLabel.trim()
                    };
                }

                return {
                    detected: false,
                    expanded: false,
                    alreadyExpanded: false,
                    label: null
                };
            }
            """,
            labels,
        )

        if not isinstance(
            data,
            dict,
        ):
            return result

        result.detected = bool(
            data.get(
                "detected"
            )
        )

        result.expanded = bool(
            data.get(
                "expanded"
            )
        )

        result.already_expanded = bool(
            data.get(
                "alreadyExpanded"
            )
        )

        result.label = (
            data.get("label")
        )

        result.error = (
            data.get("error")
        )

        if result.detected:
            result.method = (
                "javascript_semantic"
            )

    except Exception as exc:

        result.error = str(exc)

    return result


# ============================================================
# Expand one section
# ============================================================


async def _expand_single_disclosure_section(
    page: Page,
    section_name: str,
    logger: logging.Logger | None = None,
) -> DisclosureSectionResult:
    """
    Expand one disclosure section.

    Priority:

        aria-label + aria-expanded
        button aria-label
        role button aria-label
        accessible button name
        role button text
        semantic JS fallback

    No XPath.
    """

    result = DisclosureSectionResult(
        section=section_name
    )

    # ========================================================
    # Playwright
    # ========================================================

    try:

        (
            locator,
            label,
            method,
        ) = await _find_disclosure_button(
            page,
            section_name,
        )

        if locator is not None:

            result.detected = True
            result.label = label
            result.method = method

            state = (
                await locator.get_attribute(
                    "aria-expanded"
                )
            )

            # Already open
            if state == "true":

                result.expanded = True
                result.already_expanded = True

                if logger:
                    logger.info(
                        "[AdDisclosure] %s already "
                        "expanded via %s.",
                        section_name,
                        method,
                    )

                return result

            # Playwright performs actionability checks.
            await locator.click(
                timeout=(
                    DISCLOSURE_CLICK_TIMEOUT_MS
                )
            )

            # Allow aria-expanded / DOM state to update.
            try:

                await page.wait_for_timeout(
                    50
                )

                new_state = (
                    await locator.get_attribute(
                        "aria-expanded"
                    )
                )

                if new_state is None:

                    # No aria-expanded available.
                    # Successful Playwright click counts.
                    result.expanded = True

                else:

                    result.expanded = (
                        new_state == "true"
                    )

            except Exception:

                result.expanded = True

            if result.expanded:

                if logger:
                    logger.info(
                        "[AdDisclosure] %s "
                        "expanded via %s.",
                        section_name,
                        method,
                    )

                return result

    except Exception as exc:

        result.error = str(exc)

        if logger:
            logger.debug(
                "[AdDisclosure] Semantic "
                "Playwright click failed "
                "for %s: %s",
                section_name,
                exc,
            )

    # ========================================================
    # Semantic JS fallback
    # ========================================================

    js_result = (
        await _click_section_with_js(
            page,
            section_name,
        )
    )

    if js_result.detected:
        result = js_result

    if logger:

        if result.expanded:

            logger.info(
                "[AdDisclosure] %s "
                "expanded via %s.",
                section_name,
                result.method,
            )

        else:

            logger.debug(
                "[AdDisclosure] %s "
                "not found or not expanded.",
                section_name,
            )

    return result


# ============================================================
# Expand all disclosure sections
# ============================================================


async def expand_disclosure_dropdowns(
    page: Page,
    logger: logging.Logger | None = None,
    timeout_ms: int = 3000,
) -> list[DisclosureSectionResult]:
    """Expand all known disclosure sections."""

    if await _page_is_closed(page):
        return []

    ready, _ = (
        await wait_for_disclosure_ready(
            page,
            logger=logger,
            timeout_ms=timeout_ms,
        )
    )

    if (
        not ready
        or await _page_is_closed(page)
    ):
        return []

    results: list[
        DisclosureSectionResult
    ] = []

    for section_name in (
        DISCLOSURE_SECTION_LABELS
    ):

        if await _page_is_closed(page):
            break

        try:

            result = (
                await _expand_single_disclosure_section(
                    page,
                    section_name,
                    logger,
                )
            )

        except Exception as exc:

            result = (
                DisclosureSectionResult(
                    section=section_name,
                    error=str(exc),
                )
            )

        results.append(result)

    return results


# ============================================================
# Content stability
# ============================================================


async def wait_for_disclosure_content_stable(
    page: Page,
    logger: logging.Logger | None = None,
    timeout_ms: int = (
        DISCLOSURE_CONTENT_STABILITY_TIMEOUT_MS
    ),
) -> tuple[bool, int]:
    """
    Wait until body text stops changing.

    This is more reliable than a fixed 500ms sleep.
    """

    start = time.monotonic()

    previous_text: (
        str | None
    ) = None

    stable_samples = 0

    while True:

        elapsed_ms = int(
            (
                time.monotonic()
                - start
            )
            * 1000
        )

        if elapsed_ms >= timeout_ms:

            if logger:
                logger.debug(
                    "[AdDisclosure] Content "
                    "stability timeout after %dms.",
                    elapsed_ms,
                )

            return (
                False,
                elapsed_ms,
            )

        if await _page_is_closed(page):

            return (
                False,
                elapsed_ms,
            )

        try:

            text = (
                await page.locator(
                    "body"
                ).inner_text(
                    timeout=500
                )
            )

        except Exception:

            text = None

        if text is not None:

            if text == previous_text:

                stable_samples += 1

            else:

                previous_text = text
                stable_samples = 0

            if (
                stable_samples
                >= DISCLOSURE_STABLE_SAMPLES
            ):

                elapsed_ms = int(
                    (
                        time.monotonic()
                        - start
                    )
                    * 1000
                )

                if logger:
                    logger.debug(
                        "[AdDisclosure] Content "
                        "stabilized after %dms.",
                        elapsed_ms,
                    )

                return (
                    True,
                    elapsed_ms,
                )

        try:

            await page.wait_for_timeout(
                DISCLOSURE_CONTENT_POLL_MS
            )

        except Exception:

            return (
                False,
                elapsed_ms,
            )


# ============================================================
# Screenshot
# ============================================================


async def _take_disclosure_screenshot(
    page: Page,
    screenshot_path: Path,
    logger: logging.Logger | None = None,
) -> bool:
    """Take disclosure screenshot."""

    try:

        await page.screenshot(
            path=str(
                screenshot_path
            ),
            full_page=True,
        )

        return True

    except Exception as exc:

        if logger:
            logger.debug(
                "[AdDisclosure] Full-page "
                "screenshot failed: %s",
                exc,
            )

    try:

        await page.screenshot(
            path=str(
                screenshot_path
            ),
            full_page=False,
        )

        return True

    except Exception as exc:

        if logger:
            logger.debug(
                "[AdDisclosure] Viewport "
                "screenshot failed: %s",
                exc,
            )

    return False


# ============================================================
# Main processing
# ============================================================


async def process_ad_disclosure_page(
    disclosure_page: Page,
    ad_screenshot_name: str,
    output_dir: Path,
    logger: logging.Logger,
    expected_href: str | None = None,
    button_timeout_ms: int = 3000,
) -> dict | None:
    """
    Process an ad disclosure tab.

    Pipeline:

        validate page
        wait for semantic disclosure controls
        expand disclosure sections
        wait for text stability
        screenshot
        extract text
        extract disclosure links
        save diagnostics
        close tab

    No XPath.
    No c-wiz assumptions.
    No window.stop().
    """

    diagnostics = (
        DisclosureDiagnostics()
    )

    try:

        # ====================================================
        # 1. Validate page
        # ====================================================

        if await _page_is_closed(
            disclosure_page
        ):
            return None

        try:

            await disclosure_page.wait_for_timeout(
                DISCLOSURE_SETTLE_MS
            )

        except Exception:
            pass

        if await _page_is_closed(
            disclosure_page
        ):
            return None

        page_url = getattr(
            disclosure_page,
            "url",
            "",
        )

        page_hostname = (
            urlparse(
                page_url
            ).hostname
            or ""
        ).lower()

        logger.debug(
            "[AdDisclosure] Captured "
            "potential disclosure page: %s",
            page_url[:200],
        )

        # ====================================================
        # 2. Google internal disclosure URL
        # ====================================================

        match_url = page_url
        ad_disc_url = page_url

        try:

            af_service_url = (
                await disclosure_page.evaluate(
                    """
                    () =>
                        window.AF_dataServiceRequests
                            ?.['ds:0']
                            ?.request
                            ?.[5]
                        || ''
                    """
                )
            )

            if af_service_url:

                ad_disc_url = str(
                    af_service_url
                )

                if expected_href:

                    match_url = (
                        ad_disc_url
                    )

        except Exception as exc:

            logger.debug(
                "[AdDisclosure] "
                "AF_dataServiceRequests "
                "not available: %s",
                exc,
            )

        # ====================================================
        # 3. Validate identity
        # ====================================================

        if expected_href:

            if not _matches_expected_href(
                match_url,
                expected_href,
            ):

                logger.debug(
                    "[AdDisclosure] URL mismatch. "
                    "Expected=%s Current=%s",
                    expected_href[:200],
                    match_url[:200],
                )

                return None

        elif not _is_disclosure_host(
            page_hostname
        ):

            logger.debug(
                "[AdDisclosure] Rejected "
                "unknown disclosure host: %s",
                page_hostname,
            )

            return None

        # ====================================================
        # 4. Screenshot path
        # ====================================================

        if ad_screenshot_name.startswith(
            "disclosure_"
        ):

            disclosure_screenshot_name = (
                ad_screenshot_name
            )

        else:

            disclosure_screenshot_name = (
                f"disclosure_"
                f"{ad_screenshot_name}"
            )

        screenshot_path = (
            output_dir
            / "ad_disclosures"
            / disclosure_screenshot_name
        )

        screenshot_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        # ====================================================
        # 5. Wait for disclosure controls
        # ====================================================

        (
            ready,
            ready_latency_ms,
        ) = await wait_for_disclosure_ready(
            disclosure_page,
            logger=logger,
            timeout_ms=button_timeout_ms,
        )

        diagnostics.ready = ready

        diagnostics.ready_latency_ms = (
            ready_latency_ms
        )

        diagnostics.ready_timeout = (
            not ready
        )

        # ====================================================
        # 6. Expand disclosure sections
        # ====================================================

        section_results: list[
            DisclosureSectionResult
        ] = []

        if (
            ready
            and not await _page_is_closed(
                disclosure_page
            )
        ):

            for section_name in (
                DISCLOSURE_SECTION_LABELS
            ):

                try:

                    result = (
                        await _expand_single_disclosure_section(
                            disclosure_page,
                            section_name,
                            logger,
                        )
                    )

                except Exception as exc:

                    result = (
                        DisclosureSectionResult(
                            section=section_name,
                            error=str(exc),
                        )
                    )

                section_results.append(
                    result
                )

        diagnostics.sections_detected = sum(
            1
            for result in section_results
            if result.detected
        )

        diagnostics.sections_expanded = sum(
            1
            for result in section_results
            if result.expanded
        )

        # ====================================================
        # 7. Wait for expanded content
        # ====================================================

        (
            content_stable,
            content_stability_latency_ms,
        ) = (
            await wait_for_disclosure_content_stable(
                disclosure_page,
                logger=logger,
            )
        )

        diagnostics.content_stable = (
            content_stable
        )

        diagnostics.content_stability_latency_ms = (
            content_stability_latency_ms
        )

        # ====================================================
        # 8. Extract page text
        # ====================================================

        try:

            page_text = (
                await disclosure_page.locator(
                    "body"
                ).inner_text(
                    timeout=1000
                )
            )

        except Exception as exc:

            logger.debug(
                "[AdDisclosure] Text "
                "extraction failed: %s",
                exc,
            )

            page_text = ""

        diagnostics.page_text_length = (
            len(page_text)
        )

        # ====================================================
        # 9. Extract out-links
        # ====================================================

        out_links = (
            await _get_ad_disclosure_out_links(
                disclosure_page
            )
        )

        # ====================================================
        # 10. Screenshot
        # ====================================================

        diagnostics.screenshot_success = (
            await _take_disclosure_screenshot(
                disclosure_page,
                screenshot_path,
                logger,
            )
        )

        diagnostics.extraction_success = bool(
            page_text
            or out_links
            or diagnostics.screenshot_success
        )

        # ====================================================
        # 11. Return
        # ====================================================

        return {
            # Original fields
            "pageUrl":
                page_url,

            "pageText":
                page_text,

            "adDiscUrl":
                ad_disc_url,

            "adDisclosureOutLinks":
                out_links,

            "screenshot":
                disclosure_screenshot_name,

            # Collection quality information
            "disclosureCollection": {

                "diagnostics":
                    asdict(
                        diagnostics
                    ),

                "sections": [
                    asdict(result)
                    for result
                    in section_results
                ],
            },
        }

    except Exception as exc:

        logger.warning(
            "[AdDisclosure] Failed to "
            "process disclosure page: %s",
            exc,
        )

        return None

    finally:

        try:

            if (
                disclosure_page
                and not disclosure_page.is_closed()
            ):

                await disclosure_page.close()

        except Exception as exc:

            logger.debug(
                "[AdDisclosure] Failed "
                "to close disclosure page: %s",
                exc,
            )