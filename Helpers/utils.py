import re
from typing import Any, Callable

from playwright.async_api import ElementHandle

DISABLE_ADCHOICES_CLICKING = False
CLICK_METHODS = ("JS_CLICK", "NATIVE_CLICK")
ADCHOICES_REGEX_SRC = r"adchoice|whythisad|(privacy/adinfo)"
ADCHOICES_REGEX = re.compile(ADCHOICES_REGEX_SRC, re.IGNORECASE)


async def click_to_element(
    el_handle: ElementHandle,
    method: str,
    page_or_frame: Any,
) -> tuple[bool, str]:
    """Click an element with upstream-like JS/NATIVE fallbacks.

    Returns `(success, error_message)`.
    """
    try:
        if method == "NATIVE_CLICK":
            await el_handle.click()
        else:
            await page_or_frame.evaluate("(el) => el.click()", el_handle)
    except Exception as exc:
        return False, str(exc)
    return True, ""


async def open_adchoice_link(
    href: str,
    link_handle: ElementHandle,
    page_or_frame: Any,
    log: Callable[[str], None],
) -> bool:
    """Upstream-equivalent click policy: success means click succeeded.

    This mirrors JS utils semantics where a successful click returns True,
    even before any popup/new-tab is observed.
    """
    if DISABLE_ADCHOICES_CLICKING:
        return False

    href_preview = (href or "")[:100]
    for method in CLICK_METHODS:
        success, err = await click_to_element(link_handle, method, page_or_frame)
        if success:
            log(f"[AdCollector] Clicked the adchoice link using {method} {href_preview}")
            return True
        log(f"[AdCollector] Failed to click the adchoice link using {method}, ERR_MESSAGE: {err}, URL: {href}")

    return False
