"""Collectors/CookieCollector.py
------------------------------
Collects all cookies present after page load via Playwright's context API.
Also instruments cookie and storage operations (read, write, delete, clear)
recording them as timestamped events with keyed-HMAC values and source scripts.
Labels snapshot cookies with observed_at.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from playwright.async_api import Page
from Helpers.hasher import get_registrable_domain

if TYPE_CHECKING:
    from Helpers.crawl_context import CrawlContext


_STORAGE_HOOK_JS = """
(function() {
    const STACK_REGEX = /(\\()?(https?:[^)]+):[0-9]+:[0-9]+(\\))?/i;
    function getSource() {
        try {
            const stack = (new Error()).stack || '';
            for (let line of stack.split('\\n')) {
                const m = line.match(STACK_REGEX);
                if (m && m[2]) return m[2];
            }
        } catch (_) {}
        return '<unknown>';
    }

    function emit(op, storageType, key, val) {
        try {
            if (typeof window.__recordStorageOperation === 'function') {
                window.__recordStorageOperation({
                    operation: op,
                    storage_type: storageType,
                    key: key ? String(key).slice(0, 100) : '',
                    val_len: val ? String(val).length : 0,
                    val_sample: val ? String(val).slice(0, 500) : '',
                    source_script: getSource(),
                    frame_url: window.location ? window.location.href : '',
                    timestamp_ms: Date.now()
                });
            }
        } catch (_) {}
    }

    // Hook localStorage and sessionStorage
    try {
        const origGetItem = Storage.prototype.getItem;
        Storage.prototype.getItem = function(k) {
            const res = origGetItem.apply(this, arguments);
            emit('read', this === window.sessionStorage ? 'session_storage' : 'local_storage', k, res);
            return res;
        };
        const origSetItem = Storage.prototype.setItem;
        Storage.prototype.setItem = function(k, v) {
            emit('write', this === window.sessionStorage ? 'session_storage' : 'local_storage', k, v);
            return origSetItem.apply(this, arguments);
        };
        const origRemoveItem = Storage.prototype.removeItem;
        Storage.prototype.removeItem = function(k) {
            emit('delete', this === window.sessionStorage ? 'session_storage' : 'local_storage', k, null);
            return origRemoveItem.apply(this, arguments);
        };
        const origClear = Storage.prototype.clear;
        Storage.prototype.clear = function() {
            emit('clear', this === window.sessionStorage ? 'session_storage' : 'local_storage', null, null);
            return origClear.apply(this, arguments);
        };
    } catch (_) {}

    // Hook document.cookie
    try {
        const cookieDesc = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie') ||
                           Object.getOwnPropertyDescriptor(HTMLDocument.prototype, 'cookie');
        if (cookieDesc && cookieDesc.configurable) {
            Object.defineProperty(document, 'cookie', {
                configurable: true,
                enumerable: true,
                get: function() {
                    const val = cookieDesc.get.call(this);
                    emit('read', 'cookie', 'document.cookie', val);
                    return val;
                },
                set: function(val) {
                    const k = val ? String(val).split('=')[0].trim() : '';
                    emit('write', 'cookie', k, val);
                    return cookieDesc.set.call(this, val);
                }
            });
        }
    } catch (_) {}
})();
"""


class CookieCollector:
    COLLECTOR_NAME = "CookieCollector"
    BINDING_NAME = "__recordStorageOperation"

    def init(self, output_dir: str, logger, url_hash: str, crawl_context: CrawlContext | None = None) -> None:
        self._output_dir = Path(output_dir)
        self._logger = logger
        self._url_hash = url_hash
        self._crawl_context = crawl_context
        self._storage_operations: list[dict] = []
        self._ready = False

    def _is_first_party(self, cookie_domain: str) -> bool:
        if not self._crawl_context or not self._crawl_context.publisher_domain:
            return True
        pub_domain = self._crawl_context.publisher_domain.lower().lstrip(".")
        cd = (cookie_domain or "").lower().lstrip(".")
        if cd == pub_domain or cd.endswith("." + pub_domain):
            return True
        return get_registrable_domain(cd) == pub_domain

    def _process_cookie(self, c: dict) -> dict:
        expires_raw = c.get("expires", -1)
        is_session = expires_raw == -1 or expires_raw is None
        expires_ms = None if is_session else int(expires_raw * 1000)

        first_party = self._is_first_party(c.get("domain", ""))
        observed_at = datetime.now(timezone.utc).isoformat()

        record = {
            "name":        c.get("name", ""),
            "domain":      c.get("domain", ""),
            "path":        c.get("path", "/"),
            "expires":     expires_ms,
            "session":     is_session,
            "sameSite":    c.get("sameSite"),
            "httpOnly":    c.get("httpOnly", False),
            "secure":      c.get("secure", False),
            "first_party": first_party,
            "observed_at": observed_at,
        }
        if self._crawl_context:
            self._crawl_context.enrich_event(record)
        return record

    async def pre_crawl(self, page: Page) -> None:
        def _on_storage_op(data: dict) -> None:
            op = data.get("operation", "unknown")
            stype = data.get("storage_type", "unknown")
            key = data.get("key", "")
            val_len = data.get("val_len", 0)
            val_sample = data.get("val_sample", "")
            src = data.get("source_script", "<unknown>")
            frame_url = data.get("frame_url", "")
            ts_ms = data.get("timestamp_ms") or int(time.time() * 1000)

            hmac_val = self._crawl_context.hmac_value(val_sample) if self._crawl_context else ""

            record: dict[str, Any] = {
                "operation": op,
                "storage_type": stype,
                "key": key,
                "value_length": val_len,
                "value_hmac": hmac_val,
                "source_script": src,
                "frame": frame_url,
                "timestamp_ms": ts_ms,
            }
            if self._crawl_context:
                self._crawl_context.enrich_event(record, timestamp_ms=ts_ms)

            self._storage_operations.append(record)

        try:
            await page.expose_function(self.BINDING_NAME, _on_storage_op)
            await page.add_init_script(_STORAGE_HOOK_JS)
            self._ready = True
        except Exception as exc:
            self._logger.debug(f"[CookieCollector] Storage hook setup failed: {exc}")

    def get_storage_operations(self) -> list[dict]:
        return list(self._storage_operations)

    async def collect(self, page: Page) -> list:
        raw_cookies = await page.context.cookies()
        results = [self._process_cookie(c) for c in raw_cookies]
        self._logger.info(
            f"[CookieCollector] Collected {len(results)} cookie(s) (observed_at snapshot) "
            f"and {len(self._storage_operations)} storage operation event(s)"
        )
        return results

    async def collect_quick(self, page: Page) -> list:
        """Extract cookies directly from context without waiting for networkidle."""
        try:
            raw_cookies = await page.context.cookies()
        except Exception:
            return []
        return [self._process_cookie(c) for c in raw_cookies]
