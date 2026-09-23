"""Safeguard Engine — orchestrates the 19-step safeguard execution order.

Wraps the existing ``crawl()`` function with pre-checks, post-checks,
concurrency control, timeout enforcement, and audit logging.  Does not
restructure ``crawl()`` internally.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from Helpers.hasher import get_registrable_domain

from safeguard_audit import EventType, SafeguardAuditLogger
from safeguard_captcha import detect_captcha, detect_captcha_in_response
from safeguard_config import (
    BACKOFF_JITTER_MAX_SECONDS,
    BACKOFF_JITTER_MIN_SECONDS,
    CAPTCHA_REASON_CODE,
    DOMAIN_VISIT_INTERVAL_SECONDS,
    MAX_429_BACKOFF_SECONDS,
    MAX_CONSECUTIVE_DOMAIN_5XX,
    MAX_DOMAIN_VISITS_PER_UTC_DAY,
    MAX_DOWNLOADED_BYTES_PER_VISIT,
    MAX_PAGE_VISIT_SECONDS,
    MAX_REQUESTS_PER_VISIT,
    MAX_RETRIES_PER_PAGE,
    MAX_SIMULTANEOUS_CRAWLERS,
    NON_RETRYABLE_REASONS,
)
from safeguard_state import SafeguardState
from safeguard_traffic import TrafficMonitor
from timeout_manager import generate_attempt_id, generate_website_id, get_website_folder_name


class VisitRejectedError(Exception):
    """Raised when a visit is rejected by a safeguard pre-check."""

    def __init__(self, reason_code: str, message: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code)


class SafeguardEngine:
    """Orchestrates safeguard checks around page visits.

    Usage::

        engine = SafeguardEngine(state, audit)
        result = await engine.execute_visit(url, crawl_kwargs)
    """

    def __init__(
        self,
        state: SafeguardState,
        audit: SafeguardAuditLogger,
        worker_id: str = "",
        production_mode: bool = False,
    ) -> None:
        self._state = state
        self._audit = audit
        self._worker_id = worker_id
        self._production_mode = production_mode

        if production_mode:
            self._validate_production_config()

    def _validate_production_config(self) -> None:
        """Refuse to start a production run if pilot-defined thresholds are missing."""
        errors: list[str] = []
        if MAX_REQUESTS_PER_VISIT is None:
            errors.append("MAX_REQUESTS_PER_VISIT is not configured (PILOT_DEFINED)")
        if MAX_DOWNLOADED_BYTES_PER_VISIT is None:
            errors.append("MAX_DOWNLOADED_BYTES_PER_VISIT is not configured (PILOT_DEFINED)")
        if MAX_429_BACKOFF_SECONDS is None:
            errors.append("MAX_429_BACKOFF_SECONDS is not configured (RESEARCH_TEAM_DECISION)")
        if errors:
            raise ValueError(
                "Production mode requires all safeguard thresholds to be configured:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )

    async def execute_visit(
        self,
        url: str,
        crawl_fn,
        crawl_kwargs: dict[str, Any] | None = None,
    ) -> dict:
        """Execute a single page visit with full safeguard orchestration.

        Parameters
        ----------
        url : str
            The URL to crawl.
        crawl_fn : coroutine function
            The ``crawl()`` function from ``crawler.py``.
        crawl_kwargs : dict
            Keyword arguments forwarded to ``crawl_fn``.

        Returns
        -------
        dict
            The crawl result dict with safeguard metadata attached.
        """
        domain = get_registrable_domain(url)
        visit_id = uuid.uuid4().hex
        crawl_kwargs = dict(crawl_kwargs or {})
        attempt_number = crawl_kwargs.pop("_attempt_number", 1)
        retry_number = crawl_kwargs.pop("_retry_number", 0)

        result: dict = {
            "initialUrl": url,
            "finalUrl": url,
            "successful": "false",
            "safeguard_visit_id": visit_id,
            "safeguard_domain": domain,
            "safeguard_attempt": attempt_number,
            "safeguard_retry": retry_number,
        }

        global_slot_held = False
        domain_lock_held = False
        domain_lock: asyncio.Lock | None = None
        stop_reason = ""

        try:
            # === Step 1: Check emergency stop ===
            if self._state.is_emergency_stop_active():
                stop_reason = "EMERGENCY_STOP"
                self._audit.log_event(
                    EventType.VISIT_REJECTED, worker_id=self._worker_id,
                    visit_id=visit_id, domain=domain, url=url,
                    safeguard="emergency_stop", action="rejected",
                    reason_code=stop_reason, attempt_number=attempt_number,
                    retry_number=retry_number,
                )
                raise VisitRejectedError(stop_reason)

            # === Step 2: Check domain exclusion/pause/review ===
            stopped, stop_code = self._state.is_domain_stopped(domain)
            if stopped:
                stop_reason = stop_code
                self._audit.log_event(
                    EventType.VISIT_REJECTED, worker_id=self._worker_id,
                    visit_id=visit_id, domain=domain, url=url,
                    safeguard="domain_status", action="rejected",
                    reason_code=stop_reason, attempt_number=attempt_number,
                    retry_number=retry_number,
                )
                raise VisitRejectedError(stop_reason)

            # === Step 3: Check daily domain limit ===
            daily_count = self._state.get_daily_count(domain)
            if daily_count >= MAX_DOMAIN_VISITS_PER_UTC_DAY:
                stop_reason = "DAILY_LIMIT_EXHAUSTED"
                self._audit.log_event(
                    EventType.DAILY_LIMIT_REACHED, worker_id=self._worker_id,
                    visit_id=visit_id, domain=domain, url=url,
                    safeguard="daily_limit", action="rejected",
                    reason_code=stop_reason, daily_domain_count=daily_count,
                    attempt_number=attempt_number, retry_number=retry_number,
                )
                raise VisitRejectedError(stop_reason)

            # === Step 4: Check 429 backoff ===
            if self._state.is_domain_in_backoff(domain):
                remaining = self._state.get_backoff_remaining(domain)
                self._audit.log_event(
                    EventType.VISIT_DELAYED, worker_id=self._worker_id,
                    visit_id=visit_id, domain=domain, url=url,
                    safeguard="429_backoff", action="delayed",
                    reason_code="DOMAIN_IN_BACKOFF",
                    backoff_duration_sec=remaining,
                    attempt_number=attempt_number, retry_number=retry_number,
                )
                # Wait out the backoff
                await asyncio.sleep(remaining)

            # === Step 5: Acquire global semaphore slot ===
            await self._state.acquire_global_slot()
            global_slot_held = True

            # === Step 6: Acquire per-domain lock ===
            domain_lock = await self._state.get_domain_lock(domain)
            await domain_lock.acquire()
            domain_lock_held = True

            # === Step 7: Recheck emergency stop ===
            if self._state.is_emergency_stop_active():
                stop_reason = "EMERGENCY_STOP"
                self._audit.log_event(
                    EventType.VISIT_REJECTED, worker_id=self._worker_id,
                    visit_id=visit_id, domain=domain, url=url,
                    safeguard="emergency_stop_recheck", action="rejected",
                    reason_code=stop_reason, attempt_number=attempt_number,
                    retry_number=retry_number,
                )
                raise VisitRejectedError(stop_reason)

            # === Step 8: Recheck domain exclusion/pause/review ===
            stopped, stop_code = self._state.is_domain_stopped(domain)
            if stopped:
                stop_reason = stop_code
                self._audit.log_event(
                    EventType.VISIT_REJECTED, worker_id=self._worker_id,
                    visit_id=visit_id, domain=domain, url=url,
                    safeguard="domain_status_recheck", action="rejected",
                    reason_code=stop_reason, attempt_number=attempt_number,
                    retry_number=retry_number,
                )
                raise VisitRejectedError(stop_reason)

            # === Step 9: Recheck daily domain limit ===
            daily_count = self._state.get_daily_count(domain)
            if daily_count >= MAX_DOMAIN_VISITS_PER_UTC_DAY:
                stop_reason = "DAILY_LIMIT_EXHAUSTED"
                self._audit.log_event(
                    EventType.DAILY_LIMIT_REACHED, worker_id=self._worker_id,
                    visit_id=visit_id, domain=domain, url=url,
                    safeguard="daily_limit_recheck", action="rejected",
                    reason_code=stop_reason, daily_domain_count=daily_count,
                    attempt_number=attempt_number, retry_number=retry_number,
                )
                raise VisitRejectedError(stop_reason)

            # === Step 10: Enforce 60-second domain interval ===
            wait_seconds = self._state.seconds_until_next_allowed(domain)
            if wait_seconds > 0:
                self._audit.log_event(
                    EventType.DOMAIN_INTERVAL_ENFORCED, worker_id=self._worker_id,
                    visit_id=visit_id, domain=domain, url=url,
                    safeguard="domain_interval", action="delayed",
                    time_since_last_visit_sec=DOMAIN_VISIT_INTERVAL_SECONDS - wait_seconds,
                    backoff_duration_sec=wait_seconds,
                    attempt_number=attempt_number, retry_number=retry_number,
                )
                await asyncio.sleep(wait_seconds)

            # Reserve the visit slot atomically
            self._state.record_visit_start(domain, visit_id)

            # === Step 11: Increment daily visit counter ===
            new_daily_count = self._state.increment_daily_count(domain)

            # Register active visit for lease tracking
            self._state.register_active_visit(visit_id, domain, self._worker_id)

            # Log visit allowed
            self._audit.log_event(
                EventType.VISIT_ALLOWED, worker_id=self._worker_id,
                visit_id=visit_id, domain=domain, url=url,
                safeguard="all_prechecks_passed", action="allowed",
                daily_domain_count=new_daily_count,
                active_global_count=self._state.get_active_global_count(),
                active_domain_count=self._state.get_active_domain_count(domain),
                attempt_number=attempt_number, retry_number=retry_number,
            )

            # === Step 12: Start 180-second page-visit timeout ===
            # === Step 13: Begin main-page navigation (via crawl_fn) ===
            # Create traffic monitor
            traffic_monitor = TrafficMonitor(
                max_requests=MAX_REQUESTS_PER_VISIT,
                max_bytes=MAX_DOWNLOADED_BYTES_PER_VISIT,
            )

            # Inject safeguard callbacks into crawl_kwargs
            crawl_kwargs["_safeguard_callbacks"] = {
                "traffic_monitor": traffic_monitor,
                "check_emergency_stop": self._state.is_emergency_stop_active,
                "visit_id": visit_id,
                "heartbeat": lambda: self._state.heartbeat_visit(visit_id),
            }

            # Page timeout setting for crawler internal budget
            visit_timeout = min(
                crawl_kwargs.get("timeout", MAX_PAGE_VISIT_SECONDS),
                MAX_PAGE_VISIT_SECONDS,
            )
            crawl_kwargs["timeout"] = visit_timeout

            # Hard timeout must exceed the crawler's internal budget (page load + all
            # stage timeouts).  The AdCollector stage alone is 120s, so a flat 30s grace
            # is far too short.  Use the largest stage timeout + a cleanup buffer.
            from crawler import STAGE_TIMEOUTS
            max_stage = max(STAGE_TIMEOUTS.values()) if STAGE_TIMEOUTS else 120.0
            grace_period = (max_stage + 45.0) if visit_timeout >= 30.0 else 1.0
            hard_timeout = visit_timeout + grace_period


            try:
                crawl_result = await asyncio.wait_for(
                    crawl_fn(url, **crawl_kwargs),
                    timeout=hard_timeout,
                )
                result.update(crawl_result)
            except asyncio.TimeoutError:
                # === Page timeout ===
                result["successful"] = "timeout"
                result["safeguard_timeout"] = True

                # Retrieve saved partial data from disk if result.json was already written
                info = crawl_kwargs.get("attempt_info", {})
                out_dir = crawl_kwargs.get("output_dir", "processed")
                web_folder = info.get("website_folder")
                att_num = info.get("attempt_number", attempt_number)
                att_id = info.get("attempt_id")
                if web_folder and att_id:
                    from timeout_manager import get_attempt_dir
                    att_dir = get_attempt_dir(out_dir, web_folder, att_num, att_id)
                    res_file = att_dir / "result.json"
                    if res_file.is_file():
                        try:
                            saved_res = json.loads(res_file.read_text(encoding="utf-8"))
                            if isinstance(saved_res, dict) and "data" in saved_res:
                                result["data"] = saved_res["data"]
                                result["finalUrl"] = saved_res.get("finalUrl", result.get("finalUrl", url))
                                # Carry forward ad_timeout_no_retry flag if set
                                if saved_res.get("ad_timeout_no_retry"):
                                    result["ad_timeout_no_retry"] = True
                        except Exception:
                            pass

                self._audit.log_event(
                    EventType.PAGE_TIMEOUT, worker_id=self._worker_id,
                    visit_id=visit_id, domain=domain, url=url,
                    safeguard="page_timeout", action="visit_terminated",
                    reason_code="PAGE_TIMEOUT",
                    request_count=traffic_monitor.request_count,
                    downloaded_bytes=traffic_monitor.total_bytes,
                    attempt_number=attempt_number, retry_number=retry_number,
                )
                stop_reason = "PAGE_TIMEOUT"

            # === Step 14: Post-navigation checks ===
            crawl_status = result.get("_response_status")
            crawl_response_url = result.get("finalUrl", url)

            # Check for 429
            if crawl_status == 429:
                await self._handle_429(domain, visit_id, url, attempt_number, retry_number, result)
                stop_reason = "HTTP_429"

            # Check for 5xx
            if isinstance(crawl_status, int) and 500 <= crawl_status <= 599:
                await self._handle_5xx(domain, visit_id, url, crawl_status, attempt_number, retry_number)
                stop_reason = "HTTP_5XX"
            elif isinstance(crawl_status, int) and crawl_status < 500:
                # Non-5xx resets the counter
                self._state.reset_5xx_counter(domain)

            # Check traffic thresholds
            if traffic_monitor.exceeded:
                self._state.add_domain_to_manual_review(
                    domain, "TRAFFIC_THRESHOLD_EXCEEDED",
                    evidence=traffic_monitor.exceeded_reason,
                )
                self._audit.log_event(
                    EventType.TRAFFIC_THRESHOLD_EXCEEDED, worker_id=self._worker_id,
                    visit_id=visit_id, domain=domain, url=url,
                    safeguard="traffic_threshold", action="visit_aborted",
                    reason_code="TRAFFIC_THRESHOLD_EXCEEDED",
                    request_count=traffic_monitor.request_count,
                    downloaded_bytes=traffic_monitor.total_bytes,
                    domain_manual_review=True,
                    attempt_number=attempt_number, retry_number=retry_number,
                )
                stop_reason = "TRAFFIC_THRESHOLD_EXCEEDED"

            # === Step 16: Mark partial results ===
            if result.get("successful") == "timeout":
                result["safeguard_partial"] = True
                self._audit.log_event(
                    EventType.PARTIAL_TIMEOUT_PRESERVED, worker_id=self._worker_id,
                    visit_id=visit_id, domain=domain, url=url,
                    safeguard="timeout_data", action="partial_data_preserved",
                    partial_data_preserved=True,
                    attempt_number=attempt_number, retry_number=retry_number,
                )

            # === Step 17: Final audit event ===
            if not stop_reason:
                # Successful visit; no safeguard stop was triggered
                pass

        except VisitRejectedError:
            result["successful"] = "rejected"
            result["safeguard_rejected"] = True
            result["safeguard_reason"] = stop_reason

        except Exception as exc:
            self._audit.log_event(
                EventType.INTERNAL_SAFEGUARD_ERROR, worker_id=self._worker_id,
                visit_id=visit_id, domain=domain, url=url,
                safeguard="engine", action="error",
                error_category=type(exc).__name__,
                attempt_number=attempt_number, retry_number=retry_number,
                extra={"message": str(exc)[:500]},
            )
            result["successful"] = "false"
            result["safeguard_error"] = str(exc)[:500]

        finally:
            # === Step 18 & 19: Cleanup — release locks and slots ===
            self._state.release_active_visit(visit_id)
            if domain_lock_held and domain_lock is not None:
                domain_lock.release()
            if global_slot_held:
                self._state.release_global_slot()

        return result

    async def execute_visit_with_retries(
        self,
        url: str,
        crawl_fn,
        crawl_kwargs: dict[str, Any] | None = None,
    ) -> dict:
        """Execute a visit with up to MAX_RETRIES_PER_PAGE retries, tracking attempt lineage.

        Ad-collection timeouts are never retried — data collected up to that
        point is preserved and the result is returned immediately.
        """
        domain = get_registrable_domain(url)
        crawl_kwargs = dict(crawl_kwargs or {})

        website_id = crawl_kwargs.get("website_id") or generate_website_id(url)
        website_folder = crawl_kwargs.get("website_folder") or get_website_folder_name(url)
        existing_info = crawl_kwargs.get("attempt_info") or {}
        depth_level = int(existing_info.get("depth_level", crawl_kwargs.get("depth_level", 0)))
        parent_url = existing_info.get("parent_url", crawl_kwargs.get("parent_url"))
        if depth_level == 0 and not parent_url:
            parent_url = url

        root_attempt_id: str | None = None
        previous_attempt_id: str | None = None
        crawl_id = crawl_kwargs.get("crawl_id") or f"crawl_{int(time.time())}"

        result: dict = {}
        for attempt in range(1, MAX_RETRIES_PER_PAGE + 2):  # 1 original + MAX_RETRIES_PER_PAGE retries
            retry_num = max(0, attempt - 1)
            attempt_id = generate_attempt_id()
            if root_attempt_id is None:
                root_attempt_id = attempt_id

            crawl_kwargs["attempt_info"] = {
                "website_id": website_id,
                "website_folder": website_folder,
                "attempt_id": attempt_id,
                "attempt_number": attempt,
                "retry_of_attempt_id": previous_attempt_id,
                "root_attempt_id": root_attempt_id,
                "crawl_id": crawl_id,
                "worker_id": self._worker_id,
                "input_index": crawl_kwargs.get("input_index"),
                "depth_level": depth_level,
                "parent_url": parent_url,
            }
            crawl_kwargs["_attempt_number"] = attempt
            crawl_kwargs["_retry_number"] = retry_num

            result = await self.execute_visit(url, crawl_fn, crawl_kwargs)

            # SAVE-BEFORE-RETRY VALIDATION: ensure previous attempt was finalized
            previous_attempt_id = attempt_id

            def _persist_retry_decision(will_retry: bool) -> None:
                att_dir = Path(crawl_kwargs.get("attempt_info", {}).get("output_folder", ""))
                if not att_dir.is_dir():
                    from timeout_manager import get_attempt_dir
                    att_dir = get_attempt_dir(crawl_kwargs.get("output_dir", "output"), website_folder, attempt, attempt_id)
                meta_file = att_dir / "attempt_metadata.json"
                if meta_file.is_file():
                    try:
                        from timeout_manager import atomic_write_json
                        m_data = json.loads(meta_file.read_text(encoding="utf-8"))
                        m_data["retry_scheduled"] = will_retry
                        m_data["final_attempt"] = not will_retry
                        atomic_write_json(meta_file, m_data)
                    except Exception:
                        pass
                res_file = att_dir / "result.json"
                if res_file.is_file():
                    try:
                        from timeout_manager import atomic_write_json
                        r_data = json.loads(res_file.read_text(encoding="utf-8"))
                        # Remove stale duplicate _attempt_metadata from result.json
                        if "_attempt_metadata" in r_data:
                            r_data.pop("_attempt_metadata", None)
                            atomic_write_json(res_file, r_data)
                    except Exception:
                        pass


            stop_reason = result.get("safeguard_reason", "")
            successful = result.get("successful")

            # Do not retry if success
            if successful is True or successful == "true":
                _persist_retry_decision(False)
                return result

            # Do not retry if ad-collection timed out (data already saved)
            _ad_stage_names = {"adcollector", "ad_collection", "adcollection"}
            is_ad_timeout = bool(
                result.get("ad_timeout_no_retry")
                or result.get("timeout_stage", "").lower() in _ad_stage_names
            )
            if is_ad_timeout:
                self._audit.log_event(
                    EventType.RETRY_REJECTED, worker_id=self._worker_id,
                    domain=domain, url=url,
                    safeguard="ad_timeout_no_retry", action="retry_rejected",
                    reason_code="AD_COLLECTION_TIMEOUT",
                    attempt_number=attempt, retry_number=retry_num,
                )
                _persist_retry_decision(False)
                return result

            # Do not retry if partial ad data was already captured
            ad_data = result.get("data", {}).get("AdCollector", {})
            has_ads = bool(ad_data.get("adAttrs")) if isinstance(ad_data, dict) else bool(ad_data)
            if has_ads:
                _persist_retry_decision(False)
                return result

            # Do not retry for non-retryable reasons
            if stop_reason in NON_RETRYABLE_REASONS:
                self._audit.log_event(
                    EventType.RETRY_REJECTED, worker_id=self._worker_id,
                    domain=domain, url=url,
                    safeguard="retry_gate", action="retry_rejected",
                    reason_code=stop_reason,
                    attempt_number=attempt, retry_number=retry_num,
                )
                _persist_retry_decision(False)
                return result

            # Do not retry if we've hit the limit
            if retry_num >= MAX_RETRIES_PER_PAGE:
                self._audit.log_event(
                    EventType.RETRY_LIMIT_REACHED, worker_id=self._worker_id,
                    domain=domain, url=url,
                    safeguard="retry_limit", action="retry_limit_reached",
                    attempt_number=attempt, retry_number=retry_num,
                )
                _persist_retry_decision(False)
                return result

            # Do not retry if emergency stop, domain stopped, or daily limit
            if self._state.is_emergency_stop_active():
                _persist_retry_decision(False)
                return result
            stopped, _ = self._state.is_domain_stopped(domain)
            if stopped:
                _persist_retry_decision(False)
                return result
            if self._state.is_daily_limit_reached(domain):
                _persist_retry_decision(False)
                return result
            if self._state.is_5xx_limit_reached(domain):
                _persist_retry_decision(False)
                return result

            # Schedule retry
            _persist_retry_decision(True)
            self._audit.log_event(
                EventType.RETRY_SCHEDULED, worker_id=self._worker_id,
                domain=domain, url=url,
                safeguard="retry", action="retry_scheduled",
                attempt_number=attempt, retry_number=retry_num + 1,
            )

            # The next iteration will go through all safeguard checks again
            # (including domain interval, daily limit, etc.)

        return result

    async def _handle_429(
        self, domain: str, visit_id: str, url: str,
        attempt_number: int, retry_number: int, result: dict,
    ) -> None:
        """Process an HTTP 429 response: parse Retry-After, apply backoff."""
        self._audit.log_event(
            EventType.HTTP_429_RECEIVED, worker_id=self._worker_id,
            visit_id=visit_id, domain=domain, url=url,
            safeguard="429_handler", action="429_received",
            http_status=429,
            attempt_number=attempt_number, retry_number=retry_number,
        )

        # Try to parse Retry-After header from result
        retry_after_value = result.get("_retry_after_header")
        retry_after_seconds = self._parse_retry_after(retry_after_value)

        # Calculate exponential backoff
        backoff_retry_num = self._state.get_backoff_retry_number(domain) + 1
        exponential_delay = DOMAIN_VISIT_INTERVAL_SECONDS * (2 ** backoff_retry_num)

        # Cap at MAX_429_BACKOFF_SECONDS if configured
        if MAX_429_BACKOFF_SECONDS is not None:
            exponential_delay = min(exponential_delay, MAX_429_BACKOFF_SECONDS)

        # Use the larger of Retry-After and exponential backoff
        if retry_after_seconds is not None:
            delay = max(retry_after_seconds, DOMAIN_VISIT_INTERVAL_SECONDS)
        else:
            delay = max(exponential_delay, DOMAIN_VISIT_INTERVAL_SECONDS)

        # Add bounded jitter
        jitter = random.uniform(BACKOFF_JITTER_MIN_SECONDS, BACKOFF_JITTER_MAX_SECONDS)
        delay += jitter

        self._state.set_backoff(domain, delay, backoff_retry_num, reason="429")

        self._audit.log_event(
            EventType.DOMAIN_BACKOFF_SCHEDULED, worker_id=self._worker_id,
            visit_id=visit_id, domain=domain, url=url,
            safeguard="429_backoff", action="backoff_scheduled",
            backoff_duration_sec=delay,
            attempt_number=attempt_number, retry_number=retry_number,
        )

    async def _handle_5xx(
        self, domain: str, visit_id: str, url: str,
        status: int, attempt_number: int, retry_number: int,
    ) -> None:
        """Process a 5xx response: increment counter, stop domain if limit reached."""
        count = self._state.record_5xx(domain, status)

        self._audit.log_event(
            EventType.HTTP_5XX_RECEIVED, worker_id=self._worker_id,
            visit_id=visit_id, domain=domain, url=url,
            safeguard="5xx_handler", action="5xx_recorded",
            http_status=status,
            attempt_number=attempt_number, retry_number=retry_number,
            extra={"consecutive_5xx_count": count},
        )

        if count >= MAX_CONSECUTIVE_DOMAIN_5XX:
            self._state.add_domain_to_manual_review(
                domain, "CONSECUTIVE_5XX_LIMIT",
                evidence=f"{count} consecutive 5xx responses, last={status}",
            )
            self._audit.log_event(
                EventType.CONSECUTIVE_5XX_REACHED, worker_id=self._worker_id,
                visit_id=visit_id, domain=domain, url=url,
                safeguard="5xx_limit", action="domain_sent_to_manual_review",
                http_status=status,
                domain_manual_review=True,
                attempt_number=attempt_number, retry_number=retry_number,
            )

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        """Parse a Retry-After header (delta-seconds or HTTP-date)."""
        if not value:
            return None
        value = value.strip()

        # Try delta-seconds first
        try:
            seconds = int(value)
            return max(0, seconds)
        except ValueError:
            pass

        # Try HTTP-date
        try:
            dt = parsedate_to_datetime(value)
            delta = (dt - parsedate_to_datetime(None)).total_seconds()
            return max(0, delta)
        except Exception:
            pass

        return None

    async def handle_captcha_detected(
        self, domain: str, visit_id: str, url: str,
        signal_type: str, signal_value: str,
        attempt_number: int = 1, retry_number: int = 0,
    ) -> None:
        """Called when CAPTCHA detection fires during a visit."""
        self._state.exclude_domain(
            domain, CAPTCHA_REASON_CODE,
            evidence=f"signal_type={signal_type}, signal_value={signal_value}",
        )
        self._audit.log_event(
            EventType.CAPTCHA_DETECTED, worker_id=self._worker_id,
            visit_id=visit_id, domain=domain, url=url,
            safeguard="captcha_detection", action="captcha_detected",
            reason_code=CAPTCHA_REASON_CODE,
            domain_excluded=True, queued_visits_cancelled=True,
            attempt_number=attempt_number, retry_number=retry_number,
            extra={"signal_type": signal_type, "signal_value": signal_value},
        )
        self._audit.log_event(
            EventType.DOMAIN_EXCLUDED_CHALLENGE, worker_id=self._worker_id,
            visit_id=visit_id, domain=domain, url=url,
            safeguard="captcha_exclusion", action="domain_excluded",
            reason_code=CAPTCHA_REASON_CODE,
            domain_excluded=True,
            attempt_number=attempt_number, retry_number=retry_number,
        )
