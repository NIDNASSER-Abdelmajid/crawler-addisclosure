"""
Collectors/ScreenshotCollector.py
-----------------------------------
Takes a full-page JPEG screenshot of the page after load.

Inspired by:
https://github.com/duckduckgo/tracker-radar-collector/blob/main/collectors/ScreenshotCollector.js

Output
───────
screenshot_<hash>.jpg   Full-page JPEG (quality 75)
"""

from pathlib import Path

from playwright.async_api import Page


class ScreenshotCollector:
    COLLECTOR_NAME = "ScreenshotCollector"

    def init(self, output_dir: str, logger, url_hash: str) -> None:
        self._output_dir = Path(output_dir)
        self._logger = logger
        self._url_hash = url_hash

    async def collect(self, page: Page) -> list:
        try:
            await page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:
            pass

        screenshot_path = self._output_dir / f"screenshot_{self._url_hash}.jpg"
        try:
            # Removed scrollTo(0, 0) to maintain consistency with AdCollector position
            # await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(300)

            await page.screenshot(
                path=str(screenshot_path),
                full_page=True,
                type="jpeg",
                quality=75,
            )
            self._logger.info(
                f"[ScreenshotCollector] Full-page screenshot → {screenshot_path}"
            )
        except Exception as exc:
            self._logger.warning(f"[ScreenshotCollector] Full-page screenshot failed: {exc}")
            try:
                dims = await page.evaluate(
                    """
                    () => {
                        const de = document.documentElement || {};
                        const body = document.body || {};
                        const width = Math.max(
                            Number(window.innerWidth) || 0,
                            Number(de.clientWidth) || 0,
                            Number(body.clientWidth) || 0,
                            1280
                        );
                        const height = Math.max(
                            Number(window.innerHeight) || 0,
                            Number(de.clientHeight) || 0,
                            Number(body.clientHeight) || 0,
                            720
                        );
                        return {
                            width: Math.max(320, Math.min(3840, Math.floor(width))),
                            height: Math.max(240, Math.min(3840, Math.floor(height))),
                        };
                    }
                    """
                )
                await page.set_viewport_size({
                    "width": int(dims.get("width", 1280)),
                    "height": int(dims.get("height", 720)),
                })
                await page.wait_for_timeout(200)
                await page.screenshot(
                    path=str(screenshot_path),
                    full_page=False,
                    type="jpeg",
                    quality=75,
                )
                self._logger.info(
                    f"[ScreenshotCollector] Viewport fallback screenshot → {screenshot_path}"
                )
            except Exception as fallback_exc:
                self._logger.error(f"[ScreenshotCollector] Screenshot fallback failed: {fallback_exc}")
                screenshot_path = None

        return [{"screenshot": str(screenshot_path)}] if screenshot_path else []
