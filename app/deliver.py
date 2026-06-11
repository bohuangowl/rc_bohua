"""Delivery module: vendor router + background delivery worker.

Responsibilities:
  - NotificationRouter: assembles a generic task into a vendor-specific HTTP request
  - DeliveryWorker: background thread that polls TaskQueue and dispatches via NotificationRouter
"""

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .task import TaskQueue, TaskStatus

logger = logging.getLogger(__name__)


@dataclass
class RouteResult:
    """Routing result: all parameters for one assembled HTTP request."""
    url: str
    method: str
    headers: Dict[str, str]
    body: str


class NotificationRouter:
    """Vendor router: routes a task to the target vendor's HTTP request format.

    Uses the URL and Header templates from vendors.json to build the request.
    """

    def __init__(self, vendors: Dict[str, Any]):
        self._vendors = vendors

    def route(self, vendor_name: str, payload: Dict[str, Any]) -> Optional[RouteResult]:
        """Assemble a generic payload into vendor-specific HTTP request parameters.

        Returns None if the vendor is not found.
        """
        vendor = self._vendors.get(vendor_name)
        if vendor is None:
            logger.warning("Unknown vendor: %s", vendor_name)
            return None

        url: str = vendor["url"]
        method: str = vendor.get("method", "POST")
        headers: Dict[str, str] = dict(vendor.get("headers", {}))



        body_template: Optional[str] = vendor.get("body_template")
        if body_template:
            body = self._render_template(body_template, payload)
        else:
            body = json.dumps(payload, ensure_ascii=False)

        return RouteResult(url=url, method=method, headers=headers, body=body)

    @staticmethod
    def _render_template(template: str, payload: Dict[str, Any]) -> str:
        """Simple template rendering: replaces {{key}} with matching values from payload.

        Nested keys are not supported; MVP assumes flat payload structures.
        """
        import re
        def _replacer(match):
            key = match.group(1)
            return str(payload.get(key, match.group(0)))
        return re.sub(r"\{\{(\w+)\}\}", _replacer, template)


class DeliveryWorker:
    """Background delivery worker.

    Runs in a dedicated thread, polls TaskQueue for pending tasks,
    and dispatches HTTP requests via NotificationRouter and urllib.
    Decides based on response status: delivered / retry / dead-letter.
    """



    _CLIENT_ERROR_CODES = {400, 401, 403, 404, 422}

    def __init__(self, queue: TaskQueue, router: NotificationRouter,
                 max_retries: int = 3,
                 retry_base_interval_sec: int = 10,
                 poll_interval_sec: int = 5):
        self._queue = queue
        self._router = router
        self._max_retries = max_retries
        self._retry_base_interval = retry_base_interval_sec
        self._poll_interval = poll_interval_sec
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None




    def start(self) -> None:
        """Start the worker background thread."""
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="delivery-worker")
        self._thread.start()
        logger.info("Worker started: poll=%ds, max_retries=%d, base_interval=%ds",
                     self._poll_interval, self._max_retries, self._retry_base_interval)

    def stop(self) -> None:
        """Gracefully stop the worker."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            logger.info("Worker stopped")




    def deliver_one(self, task_id: int, idempotency_key: str,
                    vendor_name: str, payload: Dict[str, Any],
                    retry_count: int) -> bool:
        """Deliver a single task. Returns True on success, False on failure."""


        route = self._router.route(vendor_name, payload)
        if route is None:
            self._queue.update_status(idempotency_key, TaskStatus.FAILED,
                                      last_error=f"unknown vendor: {vendor_name}")
            return False



        try:
            data = route.body.encode("utf-8")
            req = Request(route.url, data=data, headers=route.headers,
                          method=route.method)
            with urlopen(req, timeout=10) as resp:
                status = resp.status
        except HTTPError as e:
            status = e.code
            if status in self._CLIENT_ERROR_CODES:
                self._queue.update_status(idempotency_key, TaskStatus.FAILED,
                                          last_error=f"HTTP {status} {e.reason}")
                return False


            return self._handle_failure(idempotency_key, retry_count,
                                        f"HTTP {status} {e.reason}")
        except (URLError, ConnectionError, TimeoutError) as e:
            return self._handle_failure(idempotency_key, retry_count, str(e))
        except Exception as e:
            logger.error("Unexpected error for task %d: %s", task_id, e)
            return self._handle_failure(idempotency_key, retry_count, str(e))



        if 200 <= status < 300:
            logger.info("Delivered task %s -> %s (HTTP %d)",
                         idempotency_key, vendor_name, status)
            self._queue.update_status(idempotency_key, TaskStatus.DELIVERED)
            return True
        elif status in self._CLIENT_ERROR_CODES:
            logger.warning("Client error %d for %s, marking failed", status, idempotency_key)
            self._queue.update_status(idempotency_key, TaskStatus.FAILED,
                                      last_error=f"HTTP {status}")
            return False
        else:
            return self._handle_failure(idempotency_key, retry_count,
                                        f"HTTP {status}")




    def _handle_failure(self, idempotency_key: str,
                        current_retry_count: int,
                        error_msg: str) -> bool:
        """Handle delivery failure: schedule retry or move to dead-letter queue."""
        new_retry_count = current_retry_count + 1

        if new_retry_count > self._max_retries:
            logger.warning("Task %s exhausted retries (%d), dead-lettering",
                           idempotency_key, self._max_retries)
            self._queue.update_status(idempotency_key, TaskStatus.DEAD_LETTERED,
                                      retry_count=new_retry_count,
                                      last_error=error_msg)
        else:
            delay = self._retry_base_interval * (2 ** current_retry_count)
            next_retry = datetime.now(timezone.utc) + timedelta(seconds=delay)
            logger.info("Retry %d/%d for %s in %ds",
                        new_retry_count, self._max_retries,
                        idempotency_key, delay)
            self._queue.update_status(idempotency_key, TaskStatus.PENDING,
                                      retry_count=new_retry_count,
                                      next_retry_at=next_retry,
                                      last_error=error_msg)
        return False




    def _run(self) -> None:
        """Worker main loop: poll the queue and deliver tasks."""
        logger.info("Worker run loop started")
        while not self._stop_event.is_set():
            try:
                tasks = self._queue.get_pending()
                for t in tasks:
                    if self._stop_event.is_set():
                        break
                    self.deliver_one(t.id, t.idempotency_key,
                                     t.vendor_name, t.payload,
                                     t.retry_count)
            except Exception as e:
                logger.error("Worker loop error: %s", e, exc_info=True)


            self._stop_event.wait(self._poll_interval)
        logger.info("Worker run loop ended")
