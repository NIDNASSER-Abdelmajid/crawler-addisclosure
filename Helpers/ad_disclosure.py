import json
import logging
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from playwright.async_api import Page

_CONFIG_PATH = Path(__file__).parent.parent / "resources" / "ad_disclosure.json"


def _load_ad_disclosure_config() -> tuple[list[str], list[str]]:
    """Load disclosure host/text selectors from resources with safe fallbacks."""
    fallback_links = ["See more ads by this advertiser", "Report this ad"]
    fallback_hosts = [
        "adssettings.google.com",
            "privacy.us.criteo.com",
        "privacy.eu.criteo.com",
    ]

    try:
        raw = _CONFIG_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        links = data.get("adDisclosureOutLinkTexts")
        hosts = data.get("adDisclosureHosts")
        if isinstance(links, list) and isinstance(hosts, list) and links and hosts:
            return [str(item) for item in links], [str(item) for item in hosts]
    except Exception:
        pass

    return fallback_links, fallback_hosts


AD_DISC_LINKS_TO_COLLECT, AD_DISCLOSURE_LINKS = _load_ad_disclosure_config()


def _is_disclosure_host(hostname: str) -> bool:
    host = (hostname or "").lower()
    return any(link in host for link in AD_DISCLOSURE_LINKS)


def _matches_expected_href(page_url: str, expected_href: str | None) -> bool:
    if not expected_href:
        return False

    try:
        expected = urlparse(expected_href)
        current = urlparse(page_url)
    except Exception:
        return False

    expected_host = (expected.hostname or "").lower()
    current_host = (current.hostname or "").lower()
    if not expected_host or expected_host != current_host:
        return False

    expected_path = (expected.path or "/").rstrip("/") or "/"
    current_path = (current.path or "/").rstrip("/") or "/"
    if expected.query:
        expected_query = parse_qsl(expected.query, keep_blank_values=True)
        current_query = parse_qsl(current.query, keep_blank_values=True)
        current_values: dict[str, set[str]] = {}
        for key, value in current_query:
            current_values.setdefault(key, set()).add(value)
        for key, value in expected_query:
            if value not in current_values.get(key, set()):
                return False

    return current_path == expected_path or current_path.startswith(f"{expected_path}/")


async def _get_ad_disclosure_out_links(disclosure_page: Page) -> list[dict]:
    try:
        return await disclosure_page.evaluate(
            """
            (selectors) => {
                const linksArray = [];
                for (const link of document.querySelectorAll('a')) {
                    const linkText = link.innerText.trim();
                    const linkHref = link.getAttribute('href');
                    if (selectors.includes(linkText) && linkHref !== null) {
                        linksArray.push({ text: linkText, href: linkHref });
                    }
                }
                return linksArray;
            }
            """,
            AD_DISC_LINKS_TO_COLLECT,
        )
    except Exception:
        return []


async def process_ad_disclosure_page(
    disclosure_page: Page,
    ad_screenshot_name: str,
    output_dir: Path,
    logger: logging.Logger,
    expected_href: str | None = None,
) -> dict | None:
    """Process an ad-disclosure tab using the upstream Targeted-and-Troublesome logic."""
    try:
        if disclosure_page.is_closed():
            return None

        # Upstream waits briefly before reading the disclosure page.
        await disclosure_page.wait_for_timeout(2_000)

        page_url = disclosure_page.url
        page_hostname = (urlparse(page_url).hostname or "").lower()

        logger.debug(
            f"[AdCollector] Captured ad disclosure page/ad opened in a new tab: {page_url[:100]}"
        )

        page_text = await disclosure_page.evaluate("() => window.document?.body?.innerText || ''")

        match_url = page_url
        if expected_href and "adssettings.google.com" in page_hostname:
            try:
                goog_full_url = await disclosure_page.evaluate(
                    "() => window.AF_dataServiceRequests?.['ds:0']?.request?.[5] || ''"
                )
                if goog_full_url:
                    match_url = goog_full_url
            except Exception as exc:
                logger.debug(f"[AdCollector] Error while getting AF_dataServiceRequests for matching: {exc}")

        if expected_href:
            if not _matches_expected_href(match_url, expected_href):
                await disclosure_page.close()
                return None
        elif not _is_disclosure_host(page_hostname):
            await disclosure_page.close()
            return None

        disclosure_screenshot_name = f"disclosure_{ad_screenshot_name}"
        screenshot_path = output_dir / "ad_disclosures" / disclosure_screenshot_name
        await disclosure_page.screenshot(path=str(screenshot_path), full_page=False)

        out_links = await _get_ad_disclosure_out_links(disclosure_page)

        ad_disc_url = page_url
        if "adssettings.google.com" in page_hostname:
            try:
                goog_full_url = await disclosure_page.evaluate(
                    "() => window.AF_dataServiceRequests?.['ds:0']?.request?.[5] || ''"
                )
                if goog_full_url:
                    ad_disc_url = goog_full_url
            except Exception as exc:
                logger.debug(f"[AdCollector] Error while getting AF_dataServiceRequests: {exc}")

        await disclosure_page.close()

        return {
            "pageUrl": page_url,
            "pageText": page_text,
            "adDiscUrl": ad_disc_url,
            "adDisclosureOutLinks": out_links,
            "screenshot": disclosure_screenshot_name,
        }
    except Exception as exc:
        logger.warning(f"[AdCollector] Failed to process adchoice page: {exc}")
        try:
            if disclosure_page and not disclosure_page.is_closed():
                await disclosure_page.close()
        except Exception:
            pass
        return None
