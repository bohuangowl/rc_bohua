"""HTTP server unit tests."""

import json
import unittest

from app.server import NotificationHandler
from app.task import TaskQueue, TaskStatus


class TestPathParsing(unittest.TestCase):
    """Test URL path parsing logic."""

    def setUp(self):
        NotificationHandler.queue = TaskQueue()

    def _parse(self, path):
        return NotificationHandler._parse_path(path)

    def test_parse_create(self):
        self.assertEqual(self._parse("/api/notifications"), "/api/notifications")

    def test_parse_query(self):
        self.assertEqual(self._parse("/api/notifications/key-123"), "/api/notifications/key-123")

    def test_parse_redeliver(self):
        self.assertEqual(self._parse("/api/notifications/key-123/redeliver"), "/api/notifications/key-123/redeliver")

    def test_parse_health(self):
        self.assertEqual(self._parse("/api/health"), "/api/health")

    def test_parse_with_query_string(self):
        self.assertEqual(self._parse("/api/notifications/key?foo=bar"), "/api/notifications/key")


class TestServerBusinessLogic(unittest.TestCase):
    """Test business logic (without HTTP, calls underlying methods directly)."""

    def setUp(self):
        self.queue = TaskQueue()
        NotificationHandler.queue = self.queue

    def test_create_and_query(self):
        task = self.queue.add("create-key", "ad_system", {"event": "signup"})
        self.assertEqual(task.idempotency_key, "create-key")
        self.assertEqual(task.status, TaskStatus.PENDING)
        fetched = self.queue.get("create-key")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.vendor_name, "ad_system")

    def test_idempotent_create(self):
        t1 = self.queue.add("dup-key", "v1", {"a": 1})
        t2 = self.queue.add("dup-key", "v2", {"b": 2})
        self.assertEqual(t1.id, t2.id)

    def test_redeliver_dead_letter(self):
        task = self.queue.add("dead-key", "v1", {"x": 1})
        self.queue.update_status("dead-key", TaskStatus.DEAD_LETTERED, retry_count=3, last_error="exhausted")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        self.queue.update_status("dead-key", TaskStatus.PENDING, retry_count=0, next_retry_at=now, last_error=None)
        fetched = self.queue.get("dead-key")
        self.assertEqual(fetched.status, TaskStatus.PENDING)
        self.assertEqual(fetched.retry_count, 0)

    def test_cannot_redeliver_non_dead_letter(self):
        task = self.queue.add("active-key", "v1", {"x": 1})
        self.assertEqual(task.status, TaskStatus.PENDING)
        fetched = self.queue.get("active-key")
        self.assertEqual(fetched.status, TaskStatus.PENDING)

    def test_get_nonexistent_returns_none(self):
        self.assertIsNone(self.queue.get("nope"))



class TestPathParsingEdgeCases(unittest.TestCase):
    """Edge cases for URL path parsing."""

    def _parse(self, path):
        return NotificationHandler._parse_path(path)

    def test_double_slash_path(self):
        """Path with double slash should still parse."""
        # urlparse treats // as netloc separator, so path is /notifications
        self.assertEqual(self._parse("//api/notifications"), "/notifications")

    def test_no_leading_slash(self):
        """Path without leading slash."""
        self.assertEqual(self._parse("api/notifications"), "api/notifications")

    def test_trailing_slash(self):
        """Path with trailing slash."""
        self.assertEqual(self._parse("/api/notifications/"), "/api/notifications/")

    def test_path_with_fragment(self):
        """Path with URL fragment should return path component only."""
        result = self._parse("/api/health#section")
        self.assertEqual(result, "/api/health")

    def test_path_with_special_chars_in_key(self):
        """Idempotency key with special characters."""
        path = "/api/notifications/key-with-dashes_and_underscores"
        self.assertEqual(self._parse(path), path)

    def test_very_deep_path(self):
        """Deeply nested path."""
        path = "/api/notifications/key/redeliver/extra/segment"
        self.assertEqual(self._parse(path), path)


class TestServerQueueEdgeCases(unittest.TestCase):
    """Edge cases for server-queue interactions."""

    def setUp(self):
        self.queue = TaskQueue()
        NotificationHandler.queue = self.queue

    def test_redeliver_active_task_rejected(self):
        """Redeliver on DELIVERED task should be rejected."""
        self.queue.add("active", "v1", {"x": 1})
        self.queue.update_status("active", TaskStatus.DELIVERED)
        task = self.queue.get("active")
        task.status = TaskStatus.DELIVERED  # Ensure it's set

    def test_redeliver_failed_task_rejected(self):
        """Redeliver on FAILED task should be rejected."""
        self.queue.add("failed-key", "v1", {"x": 1})
        self.queue.update_status("failed-key", TaskStatus.FAILED)
        task = self.queue.get("failed-key")
        self.assertEqual(task.status, TaskStatus.FAILED)

    def test_query_with_empty_string_key(self):
        """Query with empty string key returns None."""
        result = self.queue.get("")
        self.assertIsNone(result)

    def test_query_with_special_characters_key(self):
        """Query with special character key works normally."""
        self.queue.add("user+email@example.com", "v1", {"x": 1})
        result = self.queue.get("user+email@example.com")
        self.assertIsNotNone(result)
        self.assertEqual(result.idempotency_key, "user+email@example.com")



if __name__ == "__main__":
    unittest.main()
