"""Collect discovered browser targets (pages, frames, workers)."""

from __future__ import annotations

from pathlib import Path


class TargetCollector:
    COLLECTOR_NAME = "TargetCollector"

    def init(self, output_dir: str, logger, url_hash: str) -> None:
        self._output_dir = Path(output_dir)
        self._logger = logger
        self._url_hash = url_hash
        self._targets: list[dict] = []
        self._seen: set[tuple[str, str]] = set()
        self._ready = False
        self._context = None

    @staticmethod
    def _normalize_url(url_value) -> str:
        if url_value is None:
            return ""
        try:
            if callable(url_value):
                url_value = url_value()
        except Exception:
            return ""
        return str(url_value or "").strip()

    def _add_target(self, target_type: str, url_value) -> None:
        url = self._normalize_url(url_value)
        if not url:
            return
        key = (target_type, url)
        if key in self._seen:
            return
        self._seen.add(key)
        self._targets.append({"type": target_type, "url": url})

    def _snapshot_page_targets(self, page) -> None:
        self._add_target("page", getattr(page, "url", ""))

        try:
            frames = list(getattr(page, "frames", []) or [])
        except Exception:
            frames = []
        for frame in frames:
            try:
                parent_attr = getattr(frame, "parent_frame", None)
                parent_frame = parent_attr() if callable(parent_attr) else parent_attr
            except Exception:
                parent_frame = None
            frame_type = "page" if parent_frame is None else "frame"
            self._add_target(frame_type, getattr(frame, "url", ""))

        try:
            workers = list(getattr(page, "workers", []) or [])
        except Exception:
            workers = []
        for worker in workers:
            self._add_target("worker", getattr(worker, "url", ""))

    def _attach_page_listeners(self, page) -> None:
        def _on_frame_navigated(frame) -> None:
            try:
                parent_attr = getattr(frame, "parent_frame", None)
                parent_frame = parent_attr() if callable(parent_attr) else parent_attr
            except Exception:
                parent_frame = None
            frame_type = "page" if parent_frame is None else "frame"
            self._add_target(frame_type, getattr(frame, "url", ""))

        def _on_worker(worker) -> None:
            self._add_target("worker", getattr(worker, "url", ""))

        def _on_popup(popup) -> None:
            self._add_target("page", getattr(popup, "url", ""))
            self._attach_page_listeners(popup)

        page.on("framenavigated", _on_frame_navigated)
        page.on("worker", _on_worker)
        page.on("popup", _on_popup)

    async def pre_crawl(self, page) -> None:
        context = page.context
        self._context = context

        for existing_page in context.pages:
            self._snapshot_page_targets(existing_page)
            self._attach_page_listeners(existing_page)

        def _on_page(new_page) -> None:
            self._add_target("page", getattr(new_page, "url", ""))
            self._attach_page_listeners(new_page)

        context.on("page", _on_page)
        self._ready = True

    async def collect(self, page) -> list[dict]:
        if not self._ready:
            self._logger.warning(f"[{self.COLLECTOR_NAME}] pre_crawl was not called; collecting snapshot only")

        try:
            await page.wait_for_timeout(500)
        except Exception:
            pass

        context = self._context or page.context
        for existing_page in context.pages:
            self._snapshot_page_targets(existing_page)

        self._logger.info(f"[{self.COLLECTOR_NAME}] Collected {len(self._targets)} target(s)")
        return list(self._targets)
