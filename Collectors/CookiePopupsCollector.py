"""
Collectors/CookiePopupsCollector.py
---------------------------
Detects cookie consent management popups (CMPs) using DuckDuckGo's
autoconsent library injected into every page frame.

Short CLI alias: cookiepopup

Autoconsent JS bundle is downloaded once from CDN and cached in:
    resources/autoconsent.playwright.js

Output fields
────────────────────────────────
The collector returns a dict with two top-level keys:

detections : list   One entry per detected CMP rule:
  name            str    CMP identifier (e.g. "cookiebot", "onetrust", …)
  open            bool   A consent popup was visible at crawl time
  final           bool   Autoconsent finished processing this CMP
  action          str    The action taken: "optIn", "optOut", or None (detect only)
  patterns        list   Heuristic text patterns matched on the page
  snippets        list   Heuristic text snippets matched
  filterListMatch bool   Matched a filter-list rule
  errors          list   Any autoconsent errors encountered

scrape : dict   Visual DOM snapshot from scrapeScript.js:
  origin          str    Page origin at scrape time
  isTop           bool   True when scraped from the top-level frame
  potentialPopups list   Fixed/sticky elements that look like cookie banners:
    text            str    Visible text content of the popup element
    selector        str    Unique CSS selector for the popup element
    buttons         list   Actionable buttons inside the popup:
      text            str    Button label
      selector        str    Unique CSS selector for the button
  buttons         list   All actionable buttons on the page (same shape as above)
"""

import json
import urllib.request
from pathlib import Path

from playwright.async_api import Page

_RESOURCES = Path(__file__).parent.parent / "resources"
_SCRIPT_CACHE = _RESOURCES / "autoconsent.playwright.js"
_RULES_CACHE  = _RESOURCES / "autoconsent_rules.json"

_JS_CDN    = "https://cdn.jsdelivr.net/npm/@duckduckgo/autoconsent@latest/dist/autoconsent.playwright.js"
_RULES_CDN = "https://cdn.jsdelivr.net/npm/@duckduckgo/autoconsent@latest/rules/rules.json"
_SCRAPE_SCRIPT_CACHE = _RESOURCES / "scrapeScript.js"
_SCRAPE_SCRIPT_URL   = "https://raw.githubusercontent.com/duckduckgo/tracker-radar-collector/main/collectors/CookiePopups/scrapeScript.js"

_ASSET_MEMORY_CACHE: dict[str, str] = {}

# autoconsent config base — autoAction is overridden per-instance
_AC_CONFIG_BASE = {
    "enabled":                True,
    "disabledCmps":           [],
    "enablePrehide":          False,
    "enableCosmeticRules":    True,
    "enableFilterList":       False,
    "enableHeuristicDetection": True,
    "detectRetries":          5,     # keep low to avoid CDP eval floods
    "isMainWorld":            True,  # we inject into main world via add_init_script
}

_ACTION_MAP = {
    "in":  "optIn",
    "out": "optOut",
    None:  None,
}


def _fetch_cached(url: str, cache: Path) -> str:
    """Return cached file text, downloading from CDN on first use."""
    cache_key = str(cache.resolve())
    mem_hit = _ASSET_MEMORY_CACHE.get(cache_key)
    if mem_hit is not None:
        return mem_hit

    if cache.exists():
        content = cache.read_text(encoding="utf-8")
        _ASSET_MEMORY_CACHE[cache_key] = content
        return content

    cache.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=30) as resp:  # nosec – trusted first-party CDN
        content = resp.read().decode("utf-8")
    cache.write_text(content, encoding="utf-8")

    _ASSET_MEMORY_CACHE[cache_key] = content
    return content


class CookiePopupsCollector:
    COLLECTOR_NAME = "CookiePopupsCollector"

    def init(self, output_dir: str, logger, url_hash: str, cmp_action: str | None = None) -> None:
        self._output_dir = Path(output_dir)
        self._logger = logger
        self._url_hash = url_hash
        self._messages: list[dict] = []
        self._patterns: set[str] = set()
        self._snippets: set[str] = set()
        self._ready = False
        self._closed = False
        self._close_flag: list[bool] | None = None
        # "in" → "optIn", "out" → "optOut", None → None (detect only)
        self._auto_action = _ACTION_MAP.get(cmp_action, None)

    async def pre_crawl(self, page: Page) -> None:
        """Must be called before page.goto() — injects autoconsent into every frame."""
        try:
            ac_script = _fetch_cached(_JS_CDN, _SCRIPT_CACHE)
            rules = json.loads(_fetch_cached(_RULES_CDN, _RULES_CACHE))
        except Exception as exc:
            self._logger.error(f"[{self.COLLECTOR_NAME}] Could not load autoconsent: {exc}")
            return

        # Pre-build the initResp object for efficient CDP argument passing
        ac_config = {**_AC_CONFIG_BASE, "autoAction": self._auto_action}
        self._init_resp = {"type": "initResp", "config": ac_config, "rules": rules}

        # Capture locals so the binding closure doesn't hold a reference to self
        # (avoids Playwright circular-reference issues in some versions).
        msgs      = self._messages
        patterns  = self._patterns
        snippets  = self._snippets
        logger    = self._logger
        init_resp = self._init_resp   # pre-serialised initResp dict
        closed    = [False]           # mutable box — set to True in collect() to
        self._close_flag = closed     # stop eval responses after page teardown

        def _mark_closed(*_) -> None:
            closed[0] = True

        page.on("close", _mark_closed)

        async def _on_ac_message(msg_raw: str) -> None:
            """Playwright binding called by autoconsent whenever it has a message."""
            if closed[0]:
                return
            try:
                msg = json.loads(msg_raw) if isinstance(msg_raw, str) else msg_raw
                msgs.append(msg)
                t = msg.get("type")

                if t == "init":
                    try:
                        await page.evaluate(
                            "(r) => typeof autoconsentReceiveMessage !== 'undefined' && autoconsentReceiveMessage(r)",
                            init_resp,
                        )
                    except Exception:
                        pass

                elif t == "eval":
                    code = msg.get("code", "false")
                    try:
                        eval_result = bool(await page.evaluate(code))
                    except Exception:
                        eval_result = False
                    try:
                        await page.evaluate(
                            "(r) => typeof autoconsentReceiveMessage !== 'undefined' && autoconsentReceiveMessage(r)",
                            {"id": msg.get("id", ""), "type": "evalResp", "result": eval_result},
                        )
                    except Exception:
                        pass

                elif t == "report":
                    state = msg.get("state", {})
                    for p in state.get("heuristicPatterns", []):
                        patterns.add(p)
                    for s in state.get("heuristicSnippets", []):
                        snippets.add(s)

            except Exception as exc:
                logger.debug(f"[{self.COLLECTOR_NAME}] _on_ac_message: {exc}")

        await page.expose_function("autoconsentSendMessage", _on_ac_message)
        # Wire Playwright's exposed function into the global autoconsent expects,
        # then append the full autoconsent library script.
        # Only run autoconsent in the top-level frame to avoid flooding
        # Playwright with concurrent CDP messages from every ad iframe.
        await page.add_init_script(
            "if (window === window.top) {\n"
            "const __acSend = autoconsentSendMessage;\n"
            "window.autoconsentSendMessage = (...args) => {\n"
            "  try {\n"
            "    const maybePromise = __acSend(...args);\n"
            "    if (maybePromise && typeof maybePromise.catch === 'function') {\n"
            "      maybePromise.catch(() => undefined);\n"
            "    }\n"
            "  } catch (_) {}\n"
            "};\n"
            + ac_script
            + "\n}"
        )
        self._ready = True

    @staticmethod
    def _map_detection_to_cmp_row(detection: dict) -> dict:
        action = detection.get("action")
        open_popup = bool(detection.get("open"))
        final = bool(detection.get("final"))
        started = bool(action in {"optIn", "optOut"} and open_popup)
        succeeded = bool(started and final and not detection.get("errors"))

        return {
            "name": str(detection.get("name", "")),
            "final": final,
            "open": open_popup,
            "started": started,
            "succeeded": succeeded,
            "selfTestFail": False,
            "errors": detection.get("errors", []) if isinstance(detection.get("errors"), list) else [],
            "patterns": detection.get("patterns", []) if isinstance(detection.get("patterns"), list) else [],
            "snippets": detection.get("snippets", []) if isinstance(detection.get("snippets"), list) else [],
            "filterListMatched": bool(detection.get("filterListMatch", False)),
        }

    async def collect(self, page: Page) -> dict:
        if not self._ready:
            self._logger.warning(f"[{self.COLLECTOR_NAME}] pre_crawl was not called — skipping")
            return {"cmps": [], "scrapedFrames": []}

        # Give autoconsent time to finish detecting and scraping
        try:
            await page.wait_for_timeout(5000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass

        # Stop the binding handler issuing page.evaluate() after we leave
        if hasattr(self, "_close_flag"):
            self._close_flag[0] = True

        detections = self._build_results()
        self._logger.info(f"[{self.COLLECTOR_NAME}] Detected {len(detections)} CMP(s)")

        # ── Visual DOM scrape (scrapeScript.js) ───────────────────────────────
        # Finds fixed/sticky popup-like elements and all actionable buttons
        # regardless of whether autoconsent recognises the CMP.
        scrape: dict = {}
        try:
            scrape_js = _fetch_cached(_SCRAPE_SCRIPT_URL, _SCRAPE_SCRIPT_CACHE)
            # The script ends with `scrapePage();` — wrap so the IIFE returns it.
            # Replace the bare call at the end with a returned call.
            iife_src = scrape_js.rstrip()
            if iife_src.endswith("scrapePage();"):
                iife_src = iife_src[:-len("scrapePage();")] + "return scrapePage();"
            raw = await page.evaluate(f"(() => {{ {iife_src} }})()")
            # Drop cleanedText — it can be 150 KB and is redundant with the screenshot
            scrape = {
                "isTop":           raw.get("isTop", True),
                "origin":          raw.get("origin", ""),
                "potentialPopups": raw.get("potentialPopups", []),
                "buttons":         raw.get("buttons", []),
            }
            self._logger.info(
                f"[{self.COLLECTOR_NAME}] Scrape: {len(scrape['potentialPopups'])} popup(s), "
                f"{len(scrape['buttons'])} button(s)"
            )
        except Exception as exc:
            self._logger.warning(f"[{self.COLLECTOR_NAME}] Scrape failed: {exc}")

        cmps = [
            self._map_detection_to_cmp_row(detection)
            for detection in detections
            if isinstance(detection, dict)
        ]

        scraped_frames: list[dict] = []
        if isinstance(scrape, dict) and scrape:
            scraped_frames.append(
                {
                    "isTop": bool(scrape.get("isTop", True)),
                    "origin": str(scrape.get("origin", "")),
                    "cleanedText": "",
                    "buttons": scrape.get("buttons", []) if isinstance(scrape.get("buttons"), list) else [],
                    "potentialPopups": (
                        scrape.get("potentialPopups", [])
                        if isinstance(scrape.get("potentialPopups"), list)
                        else []
                    ),
                }
            )

        self._logger.info(
            f"[{self.COLLECTOR_NAME}] Mapped {len(cmps)} CMP result(s) and {len(scraped_frames)} scraped frame(s)"
        )
        return {"cmps": cmps, "scrapedFrames": scraped_frames}

    # ------------------------------------------------------------------
    def _build_results(self) -> list:
        detected = [m for m in self._messages if m.get("type") == "cmpDetected"]
        found    = [m for m in self._messages if m.get("type") == "popupFound"]
        done     = [m for m in self._messages if m.get("type") == "autoconsentDone"]
        errors   = [m for m in self._messages if m.get("type") == "autoconsentError"]

        results: list[dict] = []
        seen: set[str] = set()

        for msg in detected:
            cmp = msg.get("cmp", "unknown")
            if cmp in seen:
                continue
            seen.add(cmp)
            results.append({
                "name":            cmp,
                "open":            any(f.get("cmp") == cmp for f in found),
                "final":           any(d.get("cmp") == cmp for d in done),
                "action":          self._auto_action,
                "patterns":        sorted(self._patterns),
                "snippets":        sorted(self._snippets),
                "filterListMatch": False,
                "errors":          [e.get("details", str(e)) for e in errors],
            })

        # Heuristic-only match (no named CMP rule fired but patterns found)
        if not results and (self._patterns or self._snippets):
            results.append({
                "name":            "",
                "open":            False,
                "final":           False,
                "action":          self._auto_action,
                "patterns":        sorted(self._patterns),
                "snippets":        sorted(self._snippets),
                "filterListMatch": False,
                "errors":          [],
            })

        return results
