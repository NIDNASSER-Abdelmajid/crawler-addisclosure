from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import NamedTuple

import cv2
import numpy as np
from numpy.typing import NDArray
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "resources" / "AdChoiceIcons"
_SUPPORTED_EXTENSIONS = {".png"}
_DEFAULT_THRESHOLD = 0.90          # raw threshold; final acceptance uses verified ZNCC
_VERIFY_THRESHOLD = 0.93           # zero-mean NCC verification, immune to brightness bias
_HIGH_CONFIDENCE_THRESHOLD = 0.985
_COARSE_SCALES = np.linspace(0.5, 1.5, 21)   # 0.05 steps instead of ~0.17
_FINE_SPAN = 0.05
_FINE_STEP = 0.01

logger = logging.getLogger(__name__)


class MatchResult(NamedTuple):
    x: int
    y: int
    confidence: float
    template_name: str
    scale: float


class _Template(NamedTuple):
    name: str
    gray: NDArray[np.uint8]
    mask: NDArray[np.uint8] | None   # binary 0/255


def _load_templates(directory: Path) -> list[_Template]:
    templates: list[_Template] = []
    if not directory.is_dir():
        logger.warning("[AdChoicesMatcher] Template directory does not exist: %s", directory)
        return templates

    for filepath in sorted(directory.iterdir()):
        if filepath.is_dir() or filepath.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            continue
        raw = cv2.imread(str(filepath), cv2.IMREAD_UNCHANGED)
        if raw is None:
            logger.warning("[AdChoicesMatcher] Could not decode: %s", filepath.name)
            continue

        mask: NDArray[np.uint8] | None = None
        if raw.ndim == 3 and raw.shape[2] == 4:
            # Binarize the alpha channel: soft edges poison masked matching.
            mask = np.where(raw[:, :, 3] > 127, 255, 0).astype(np.uint8)
            if cv2.countNonZero(mask) == 0:
                logger.warning("[AdChoicesMatcher] Fully transparent template: %s", filepath.name)
                continue
            # Treat near-fully-opaque alpha as no mask (cheaper + unbiased TM_CCOEFF).
            if cv2.countNonZero(mask) / mask.size > 0.99:
                mask = None
            gray = cv2.cvtColor(raw[:, :, :3], cv2.COLOR_BGR2GRAY)
        elif raw.ndim == 3:
            gray = cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY)
        else:
            gray = raw

        templates.append(_Template(name=filepath.name, gray=gray, mask=mask))

    logger.info("[AdChoicesMatcher] Loaded %d template(s) from %s", len(templates), directory)
    return templates


_TEMPLATE_BANK: list[_Template] = _load_templates(_TEMPLATE_DIR)


def reload_templates() -> int:
    global _TEMPLATE_BANK
    _TEMPLATE_BANK = _load_templates(_TEMPLATE_DIR)
    return len(_TEMPLATE_BANK)


def _masked_zncc(
    patch: NDArray[np.uint8],
    tmpl: NDArray[np.uint8],
    mask: NDArray[np.uint8] | None,
) -> float:
    """Zero-mean normalized cross-correlation over masked pixels.

    Brightness/contrast invariant — used to verify candidates and as the
    final confidence, eliminating TM_CCORR_NORMED's bright-region bias.
    """
    if mask is not None:
        sel = mask > 0
        a = patch[sel].astype(np.float32)
        b = tmpl[sel].astype(np.float32)
    else:
        a = patch.astype(np.float32).ravel()
        b = tmpl.astype(np.float32).ravel()
    a -= a.mean()
    b -= b.mean()
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom < 1e-6:
        return 0.0
    return float(np.dot(a, b) / denom)


def _score_at_scale(
    screen_gray: NDArray[np.uint8],
    template: _Template,
    scale: float,
    threshold: float,
) -> MatchResult | None:
    src_h, src_w = template.gray.shape[:2]
    new_w, new_h = max(int(src_w * scale), 4), max(int(src_h * scale), 4)
    if new_w > screen_gray.shape[1] or new_h > screen_gray.shape[0]:
        return None

    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
    resized = cv2.resize(template.gray, (new_w, new_h), interpolation=interp)

    resized_mask: NDArray[np.uint8] | None = None
    if template.mask is not None:
        # NEAREST keeps the mask strictly binary.
        resized_mask = cv2.resize(template.mask, (new_w, new_h), interpolation=cv2.INTER_NEAREST)
        if cv2.countNonZero(resized_mask) == 0:
            return None
        result = cv2.matchTemplate(screen_gray, resized, cv2.TM_CCORR_NORMED, mask=resized_mask)
        # Masked CCORR_NORMED can emit NaN/inf — sanitize before minMaxLoc.
        result = np.nan_to_num(result, nan=-1.0, posinf=-1.0, neginf=-1.0)
        np.clip(result, -1.0, 1.0, out=result)
    else:
        result = cv2.matchTemplate(screen_gray, resized, cv2.TM_CCOEFF_NORMED)

    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val < threshold:
        return None

    # Verify with brightness-invariant ZNCC on the actual candidate patch.
    x0, y0 = max_loc
    patch = screen_gray[y0:y0 + new_h, x0:x0 + new_w]
    confidence = _masked_zncc(patch, resized, resized_mask)
    if confidence < _VERIFY_THRESHOLD:
        return None

    return MatchResult(
        x=x0 + new_w // 2,
        y=y0 + new_h // 2,
        confidence=round(confidence, 6),
        template_name=template.name,
        scale=round(float(scale), 4),
    )


def _match_single_template(
    screen_gray: NDArray[np.uint8],
    template: _Template,
    threshold: float,
) -> MatchResult | None:
    # Coarse pass.
    best: MatchResult | None = None
    for scale in _COARSE_SCALES:
        hit = _score_at_scale(screen_gray, template, float(scale), threshold)
        if hit and (best is None or hit.confidence > best.confidence):
            best = hit
    if best is None:
        return None

    # Fine pass around the best coarse scale for sub-step precision.
    lo, hi = best.scale - _FINE_SPAN, best.scale + _FINE_SPAN
    for scale in np.arange(lo, hi + 1e-9, _FINE_STEP):
        if abs(scale - best.scale) < 1e-9 or scale <= 0:
            continue
        hit = _score_at_scale(screen_gray, template, float(scale), threshold)
        if hit and hit.confidence > best.confidence:
            best = hit
    return best


def match_ad_choices(
    screenshot_bytes: bytes,
    *,
    threshold: float = _DEFAULT_THRESHOLD,
) -> MatchResult | None:
    if not _TEMPLATE_BANK:
        logger.warning("[AdChoicesMatcher] No templates loaded.")
        return None

    arr = np.frombuffer(screenshot_bytes, dtype=np.uint8)
    screenshot_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if screenshot_bgr is None:
        logger.error("[AdChoicesMatcher] Failed to decode screenshot bytes.")
        return None

    # No downscaling: AdChoices icons are tiny (~15-20px) and downscaling
    # destroys exactly the detail needed to match them.
    screen_gray = cv2.cvtColor(screenshot_bgr, cv2.COLOR_BGR2GRAY)

    best: MatchResult | None = None
    for tmpl in _TEMPLATE_BANK:
        hit = _match_single_template(screen_gray, tmpl, threshold)
        if hit and (best is None or hit.confidence > best.confidence):
            best = hit
            if best.confidence >= _HIGH_CONFIDENCE_THRESHOLD:
                break

    if best:
        logger.info(
            "[AdChoicesMatcher] Match: template='%s' confidence=%.4f center=(%d,%d) scale=%.2f",
            best.template_name, best.confidence, best.x, best.y, best.scale,
        )
    return best


def _to_page_coords(match: MatchResult, bbox: dict, image_size: tuple[int, int]) -> tuple[float, float]:
    """Map screenshot-pixel coords to CSS/page coords via the actual
    screenshot-to-bbox ratio (robust to any DPR or browser scaling)."""
    img_w, img_h = image_size
    sx = bbox["width"] / img_w if img_w else 1.0
    sy = bbox["height"] / img_h if img_h else 1.0
    return bbox["x"] + match.x * sx, bbox["y"] + match.y * sy


async def find_ad_choices_in_screenshot(
    screenshot_bytes: bytes,
    bbox: dict,
    page: Page,  # kept for API compatibility; no longer needed for DPR
    *,
    threshold: float = _DEFAULT_THRESHOLD,
) -> tuple[float, float] | None:
    match = await asyncio.to_thread(match_ad_choices, screenshot_bytes, threshold=threshold)
    if match is None:
        return None

    arr = np.frombuffer(screenshot_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    coords = _to_page_coords(match, bbox, (img.shape[1], img.shape[0]))
    logger.info(
        "[AdChoicesMatcher] Icon at viewport (%.1f, %.1f) [confidence=%.4f]",
        coords[0], coords[1], match.confidence,
    )
    return coords


async def click_ad_choices_icon(
    page: Page,
    ad_selector: str,
    *,
    threshold: float = _DEFAULT_THRESHOLD,
    settle_ms: int = 200,
    timeout_ms: int = 5_000,
) -> MatchResult | None:
    try:
        locator = page.locator(ad_selector).first
        await locator.wait_for(state="visible", timeout=timeout_ms)
        await locator.scroll_into_view_if_needed(timeout=timeout_ms)
        await page.wait_for_timeout(settle_ms)

        screenshot_bytes: bytes = await locator.screenshot(type="png")
        bbox = await locator.bounding_box()
        if bbox is None:
            return None

        # Match exactly once and reuse the result for both click and return.
        match = await asyncio.to_thread(match_ad_choices, screenshot_bytes, threshold=threshold)
        if match is None:
            return None

        arr = np.frombuffer(screenshot_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        x, y = _to_page_coords(match, bbox, (img.shape[1], img.shape[0]))

        await page.mouse.click(x, y)
        return match

    except PlaywrightTimeoutError:
        logger.warning("[AdChoicesMatcher] Timed out waiting for '%s'.", ad_selector)
        return None
    except Exception:
        logger.exception("[AdChoicesMatcher] Error processing '%s'.", ad_selector)
        return None
