"""HTTP API module.

Three endpoints using Python stdlib http.server:
    POST   /api/notifications              - create a notification task
    GET    /api/notifications/<key>        - query task status
    POST   /api/notifications/<key>/redeliver - redeliver a dead-letter task
    GET    /api/health                     - health check

All responses are JSON.
"""

import json
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict
from urllib.parse import urlparse

from .task import TaskQueue, TaskStatus

logger = logging.getLogger(__name__)


class NotificationHandler(BaseHTTPRequestHandler):
    """HTTP request handler.

    TaskQueue is injected via the class-level "queue" attribute.
    """

    queue: TaskQueue = None  # type: ignore[assignment]

    # ---- Helper methods ----

    def log_message(self, fmt: str, *args: Any) -> None:
        """Route log output through the logging module instead of stderr."""
        logger.info("HTTP %s - %s", self.command, args[0] if args else "")

    def _read_body(self) -> str:
        """Read the request body."""
        raw = self.headers.get("Content-Length", "0")
        try:
            content_length = int(raw)
        except (ValueError, TypeError):
            content_length = 0
        if content_length > 0:
            try:
                return self.rfile.read(content_length).decode("utf-8")
            except (ConnectionError, OSError):
                return ""
        return ""

    def _json_response(self, status_code: int, data: Dict[str, Any]) -> None:
        """Send a JSON response."""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    @staticmethod
    def _parse_path(path: str) -> str:
        """Parse URL path, strip query string, return the path component."""
        return urlparse(path).path

    # ---- Route dispatch ----

    def do_POST(self) -> None:
        path = self._parse_path(self.path)
        if path == "/api/notifications":
            self._handle_create()
        elif path.startswith("/api/notifications/") and path.endswith("/redeliver"):
            key = path[len("/api/notifications/"):-len("/redeliver")]
            self._handle_redeliver(key)
        else:
            self._json_response(404, {"error": "not found"})

    def do_GET(self) -> None:
        path = self._parse_path(self.path)
        if path == "/api/health":
            self._json_response(200, {"status": "ok"})
        elif path.startswith("/api/notifications/"):
            key = path[len("/api/notifications/"):]
            self._handle_query(key)
        else:
            self._json_response(404, {"error": "not found"})

    # ---- Business logic handlers ----

    def _handle_create(self) -> None:
        """POST /api/notifications - enqueue a notification delivery task."""
        # Parse request body
        try:
            raw = self._read_body()
            data: Dict[str, Any] = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._json_response(400, {"error": "invalid JSON body"})
            return

        # Validate required fields
        vendor_name = data.get("vendor_name")
        payload = data.get("payload")
        idempotency_key = data.get("idempotency_key")
        missing = []
        if not vendor_name:
            missing.append("vendor_name")
        if not payload:
            missing.append("payload")
        if not idempotency_key:
            missing.append("idempotency_key")
        if missing:
            self._json_response(400, {
                "error": "missing required fields: {0}".format(", ".join(missing))
            })
            return

        # Enqueue (idempotent: same key returns existing task)
        task = self.queue.add(idempotency_key, vendor_name, payload)
        self._json_response(202, {
            "idempotency_key": task.idempotency_key,
            "status": task.status.value,
        })

    def _handle_query(self, idempotency_key: str) -> None:
        """GET /api/notifications/<key> - query task status."""
        task = self.queue.get(idempotency_key)
        if task is None:
            self._json_response(404, {"error": "notification not found"})
        else:
            self._json_response(200, task.to_dict())

    def _handle_redeliver(self, idempotency_key: str) -> None:
        """POST /api/notifications/<key>/redeliver - redeliver a dead-letter task."""
        task = self.queue.get(idempotency_key)
        if task is None:
            self._json_response(404, {"error": "notification not found"})
            return

        if task.status != TaskStatus.DEAD_LETTERED:
            self._json_response(400, {
                "error": "cannot redeliver task with status '{0}'".format(task.status.value)
            })
            return

        # Reset to PENDING, clear retry count
        self.queue.update_status(idempotency_key, TaskStatus.PENDING,
                                 retry_count=0,
                                 next_retry_at=datetime.now(timezone.utc),
                                 last_error=None)
        self._json_response(202, {
            "idempotency_key": idempotency_key,
            "status": "pending",
        })


def create_server(queue: TaskQueue, host: str, port: int) -> HTTPServer:
    """Create and return a configured HTTPServer instance."""
    NotificationHandler.queue = queue
    server = HTTPServer((host, port), NotificationHandler)
    return server
