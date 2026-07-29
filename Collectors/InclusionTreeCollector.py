from Helpers.inclusion_tree import InclusionTreeBuilder
from Helpers.inclusion_tree_visualizer import visualize_tree


class InclusionTreeCollector:
    COLLECTOR_NAME = "InclusionTreeCollector"

    def init(self, output_dir: str, logger, url_hash: str) -> None:
        self._output_dir = output_dir
        self._logger = logger
        self._url_hash = url_hash
        self._builder = InclusionTreeBuilder(logger=logger)
        self._ready = False

    def _handle_event(self, method: str, params: dict) -> None:
        try:
            self._builder.handle_event(method, params)
        except Exception as exc:
            self._logger.debug(f"[InclusionTreeCollector] Failed to process {method}: {exc}")

    async def pre_crawl(self, page) -> None:
        self._cdp = await page.context.new_cdp_session(page)
        await self._cdp.send("Network.enable")
        await self._cdp.send("Page.enable")
        await self._cdp.send("Runtime.enable")
        await self._cdp.send("Debugger.enable")

        for method in (
            "Network.requestWillBeSent",
            "Network.responseReceived",
            "Network.webSocketCreated",
            "Network.webSocketWillSendHandshakeRequest",
            "Network.webSocketHandshakeResponseReceived",
            "Network.webSocketFrameSent",
            "Network.webSocketFrameReceived",
            "Network.webSocketClosed",
            "Page.frameAttached",
            "Page.frameNavigated",
            "Runtime.executionContextCreated",
            "Debugger.scriptParsed",
            "Runtime.consoleAPICalled",
            "Network.getAllCookies",
        ):
            self._cdp.on(method, lambda event, event_name=method: self._handle_event(event_name, event))

        self._ready = True

    async def collect(self, page) -> dict:
        if not self._ready:
            self._logger.warning("[InclusionTreeCollector] pre_crawl was not called; skipping")
            return {"inclusionTrees": [], "nTrees": 0, "nNodes": 0}

        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        try:
            await page.wait_for_timeout(1000)
        except Exception:
            pass

        await self._cdp.detach()

        trees = self._builder.build_trees()
        node_count = sum(self._builder.count_nodes(tree) for tree in trees)
        self._logger.info(
            f"[InclusionTreeCollector] Built {len(trees)} inclusion tree(s) with {node_count} node(s)"
        )

        visualisations: list[str] = []
        for tree in trees:
            try:
                svg = visualize_tree(tree, self._output_dir, filename=f"{self._url_hash}_inclusion_tree.svg")
                if svg:
                    visualisations.append(svg)
                    self._logger.info(f"[InclusionTreeCollector] wrote visualisation: {svg}")
            except Exception as exc:
                self._logger.debug(f"[InclusionTreeCollector] failed to render visualisation: {exc}")

        return {
            "inclusionTrees": trees,
            "nTrees": len(trees),
            "nNodes": node_count,
            "visualisations": visualisations,
        }