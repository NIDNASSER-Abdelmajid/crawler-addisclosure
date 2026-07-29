"""Collect browser API calls using DDG TrackerTracker-style CDP breakpoints."""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from Collectors.APICalls import TrackerTracker


class APICallCollector:
    COLLECTOR_NAME = "APICallCollector"
    BINDING_NAME = "registerAPICall"

    def init(
        self,
        output_dir: str,
        logger,
        url_hash: str,
        enable_async_stacktraces: bool = False,
    ) -> None:
        self._output_dir = output_dir
        self._logger = logger
        self._url_hash = url_hash
        self._enable_async_stacktraces = bool(enable_async_stacktraces)

        self._stats: dict[str, dict[str, int]] = {}
        self._calls: list[dict] = []

        self._ready = False
        self._closed = False
        self._incomplete_data = False

        self._cdp = None
        self._tracker: TrackerTracker | None = None
        self._tasks: set[asyncio.Task] = set()
        self._context_setup_lock = asyncio.Lock()
        self._tracked_context_ids: set[int] = set()

    def _is_ignored_error(self, exc: Exception) -> bool:
        text = str(exc)
        ignored = (
            "Target closed",
            "Session closed",
            "Cannot find context with specified id",
            "Execution context was destroyed",
            "Target page, context or browser has been closed",
        )
        return any(token in text for token in ignored)

    def _track_task(self, coro, label: str) -> None:
        if self._closed:
            return

        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _on_done(done_task: asyncio.Task) -> None:
            self._tasks.discard(done_task)
            if done_task.cancelled():
                return
            exc = done_task.exception()
            if exc is None:
                return
            if self._is_ignored_error(exc):
                return
            self._logger.debug(f"[{self.COLLECTOR_NAME}] {label} failed: {exc}")

        task.add_done_callback(_on_done)

    async def _resume_debugger(self) -> None:
        if not self._tracker or self._closed:
            return
        try:
            await self._tracker.send_command("Debugger.resume")
        except Exception as exc:
            if self._is_ignored_error(exc):
                return
            if "Operation timed out" in str(exc):
                self._logger.warning(f"[{self.COLLECTOR_NAME}] Debugger got stuck")
            self._incomplete_data = True

    def _update_call_stats(self, source: str, description: str) -> None:
        source_stats = self._stats.setdefault(source, {})
        source_stats[description] = source_stats.get(description, 0) + 1

    def _on_script_parsed(self, params: dict) -> None:
        if self._closed or not self._tracker:
            return
        self._tracker.process_script_parsed(params)

    def _on_binding_called(self, params: dict) -> None:
        if self._closed or not self._tracker:
            return
        if params.get("name") != self.BINDING_NAME:
            return

        breakpoint = self._tracker.process_binding_pause(params)
        if not breakpoint:
            return

        source = breakpoint.get("source")
        description = breakpoint.get("description")
        if not source or not description:
            return

        self._update_call_stats(source, description)
        if breakpoint.get("saveArguments"):
            self._calls.append(
                {
                    "source": source,
                    "description": description,
                    "arguments": breakpoint.get("arguments") or [],
                }
            )

    def _on_debugger_paused(self, params: dict) -> None:
        if self._closed or not self._tracker:
            return

        # Resume breakpoints quickly so page execution can continue.
        self._track_task(self._resume_debugger(), "Debugger.resume")

        breakpoint = self._tracker.process_debugger_pause(params)
        if not breakpoint:
            return

        source = breakpoint.get("source")
        description = breakpoint.get("description")
        if not source or not description:
            return

        self._update_call_stats(source, description)
        if breakpoint.get("saveArguments"):
            call = self._tracker.retrieve_call_arguments(breakpoint.get("id"))
            if call:
                self._calls.append(
                    {
                        **call,
                        "source": source,
                    }
                )
            else:
                self._logger.debug(
                    f"[{self.COLLECTOR_NAME}] Missing call args for {breakpoint.get('id')}"
                )

    async def _handle_execution_context_created(self, params: dict) -> None:
        if self._closed or not self._tracker:
            return

        context = params.get("context") or {}
        aux_data = context.get("auxData") or {}
        origin = context.get("origin")
        context_type = aux_data.get("type")

        # Ignore isolated contexts created by Playwright itself.
        if (not origin or origin == "://") and context_type == "isolated":
            return

        context_id = context.get("id")
        if context_id is None:
            return

        # Skip non-web contexts (e.g. extension internals) to avoid excessive
        # breakpoint fan-out and CDP instability on complex sites.
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme and parsed.scheme not in {"http", "https"}:
                return

        if context_id in self._tracked_context_ids:
            return
        self._tracked_context_ids.add(context_id)

        async with self._context_setup_lock:
            await self._tracker.setup_context_tracking(context_id)

    def _on_execution_context_created(self, params: dict) -> None:
        self._track_task(
            self._handle_execution_context_created(params),
            "Runtime.executionContextCreated",
        )

    @staticmethod
    def _is_acceptable_url(url_string: str) -> bool:
        try:
            parsed = urlparse(url_string)
        except Exception:
            return False

        if parsed.scheme == "data":
            return False

        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    async def pre_crawl(self, page) -> None:
        self._closed = False
        self._incomplete_data = False

        def _mark_closed(*_) -> None:
            self._closed = True

        page.on("close", _mark_closed)

        self._cdp = await page.context.new_cdp_session(page)
        self._tracker = TrackerTracker(self._cdp.send, self._logger.debug)
        self._tracker.set_main_url(page.url or "")

        self._cdp.on("Debugger.scriptParsed", self._on_script_parsed)
        self._cdp.on("Debugger.paused", self._on_debugger_paused)
        self._cdp.on("Runtime.executionContextCreated", self._on_execution_context_created)
        self._cdp.on("Runtime.bindingCalled", self._on_binding_called)

        await self._cdp.send("Runtime.addBinding", {"name": self.BINDING_NAME})
        await self._tracker.init(enable_async_stacktraces=self._enable_async_stacktraces)

        self._ready = True

    async def _drain_tasks(self, timeout_seconds: float = 5.0) -> None:
        if not self._tasks:
            return

        done, pending = await asyncio.wait(list(self._tasks), timeout=timeout_seconds)
        self._tasks.difference_update(done)

        for task in pending:
            task.cancel()
            self._tasks.discard(task)

    async def collect(self, page) -> dict:
        if not self._ready:
            self._logger.warning(f"[{self.COLLECTOR_NAME}] pre_crawl was not called; skipping")
            return {"callStats": {}, "savedCalls": []}

        try:
            await page.wait_for_timeout(2000)
        except Exception:
            pass

        if self._tracker:
            self._tracker.set_main_url(page.url or "")

        await self._drain_tasks(timeout_seconds=5.0)
        self._closed = True

        if self._cdp:
            try:
                await self._cdp.detach()
            except Exception as exc:
                if not self._is_ignored_error(exc):
                    self._logger.debug(f"[{self.COLLECTOR_NAME}] CDP detach failed: {exc}")

        call_stats = {
            source: stats
            for source, stats in self._stats.items()
            if self._is_acceptable_url(source)
        }
        saved_calls = [
            call
            for call in self._calls
            if self._is_acceptable_url(call.get("source", ""))
        ]

        if self._incomplete_data:
            self._logger.warning(
                f"[{self.COLLECTOR_NAME}] Collected data may be incomplete due to debugger errors"
            )

        self._logger.info(
            f"[{self.COLLECTOR_NAME}] Recorded {len(saved_calls)} API call(s) "
            f"from {len(call_stats)} source(s)"
        )
        return {"callStats": call_stats, "savedCalls": saved_calls}
