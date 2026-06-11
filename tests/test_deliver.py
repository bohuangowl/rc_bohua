"""Delivery Worker unit tests."""

import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from app.deliver import DeliveryWorker, NotificationRouter, RouteResult
from app.task import TaskQueue, TaskStatus


class TestNotificationRouter(unittest.TestCase):
    """Test vendor routing logic."""

    def setUp(self):
        self.vendors = {
            "ad_system": {
                "url": "https://ad.example.com/notify",
                "method": "POST",
                "headers": {"Content-Type": "application/json", "X-Key": "abc"},
            },
            "crm_system": {
                "url": "https://crm.example.com/hook",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body_template": '{"event": "{{event}}", "uid": {{user_id}}}',
            },
        }
        self.router = NotificationRouter(self.vendors)

    def test_route_known_vendor(self):
        """Known vendor returns a RouteResult."""
        result = self.router.route("ad_system", {"event": "test"})
        self.assertIsNotNone(result)
        self.assertEqual(result.url, "https://ad.example.com/notify")
        self.assertEqual(result.method, "POST")
        self.assertEqual(result.headers["X-Key"], "abc")
        self.assertEqual(json.loads(result.body), {"event": "test"})

    def test_route_unknown_vendor_returns_none(self):
        """Unknown vendor returns None."""
        result = self.router.route("nonexistent", {"x": 1})
        self.assertIsNone(result)

    def test_route_body_template(self):
        """{{key}} in body_template is replaced by corresponding payload values."""
        result = self.router.route("crm_system",
                                    {"event": "payment", "user_id": 42})
        self.assertIsNotNone(result)
        expected = '{"event": "payment", "uid": 42}'
        self.assertEqual(result.body, expected)

    def test_route_missing_vendor_key_in_payload(self):
        """When payload is missing a template key, the placeholder is kept as-is."""
        result = self.router.route("crm_system", {"event": "test"})
        self.assertIsNotNone(result)
        self.assertIn("{{user_id}}", result.body)


class TestDeliveryWorker(unittest.TestCase):
    """Test DeliveryWorker.deliver_one logic."""

    def setUp(self):
        self.queue = TaskQueue()
        self.vendors = {
            "test_vendor": {
                "url": "https://test.example.com/notify",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
            }
        }
        self.router = NotificationRouter(self.vendors)
        self.worker = DeliveryWorker(
            self.queue, self.router,
            max_retries=3, retry_base_interval_sec=10,
            poll_interval_sec=3600,

        )

    def _make_task(self, key="test-key"):
        """Helper: create a test task."""
        return self.queue.add(key, "test_vendor", {"event": "test"})

    @patch("app.deliver.urlopen")
    def test_deliver_success_2xx(self, mock_urlopen):
        """2xx marks the task as DELIVERED."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        task = self._make_task()
        result = self.worker.deliver_one(task.id, task.idempotency_key,
                                          task.vendor_name, task.payload,
                                          task.retry_count)
        self.assertTrue(result)
        fetched = self.queue.get(task.idempotency_key)
        self.assertEqual(fetched.status, TaskStatus.DELIVERED)

    @patch("app.deliver.urlopen")
    def test_deliver_client_error_4xx_fails(self, mock_urlopen):
        """4xx marks the task as FAILED, no retry."""
        mock_resp = MagicMock()
        mock_resp.status = 400
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        task = self._make_task("key-400")
        result = self.worker.deliver_one(task.id, task.idempotency_key,
                                          task.vendor_name, task.payload,
                                          task.retry_count)
        self.assertFalse(result)
        fetched = self.queue.get("key-400")
        self.assertEqual(fetched.status, TaskStatus.FAILED)

    @patch("app.deliver.urlopen")
    def test_deliver_server_error_5xx_retries(self, mock_urlopen):
        """5xx schedules a retry, retry_count increments."""
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        task = self._make_task("key-500")
        result = self.worker.deliver_one(task.id, task.idempotency_key,
                                          task.vendor_name, task.payload,
                                          task.retry_count)
        self.assertFalse(result)
        fetched = self.queue.get("key-500")
        self.assertEqual(fetched.status, TaskStatus.PENDING)
        self.assertEqual(fetched.retry_count, 1)
        self.assertIsNotNone(fetched.next_retry_at)

    @patch("app.deliver.urlopen")
    def test_exhaust_retries_dead_letters(self, mock_urlopen):
        """Exhausted retries (count >= max_retries) move the task to dead-letter queue."""
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        task = self._make_task("key-dead")
        # retry_count=3 means new_retry_count=4, which exceeds max_retries=3
        result = self.worker.deliver_one(task.id, task.idempotency_key,
                                          task.vendor_name, task.payload,
                                          retry_count=3)
        self.assertFalse(result)
        fetched = self.queue.get("key-dead")
        self.assertEqual(fetched.status, TaskStatus.DEAD_LETTERED)
        self.assertEqual(fetched.retry_count, 4)

    @patch("app.deliver.urlopen")
    def test_deliver_network_error_retries(self, mock_urlopen):
        """Network error (URLError) triggers a retry."""
        from urllib.error import URLError
        mock_urlopen.side_effect = URLError("connection refused")

        task = self._make_task("key-net")
        result = self.worker.deliver_one(task.id, task.idempotency_key,
                                          task.vendor_name, task.payload,
                                          task.retry_count)
        self.assertFalse(result)
        fetched = self.queue.get("key-net")
        self.assertEqual(fetched.status, TaskStatus.PENDING)
        self.assertEqual(fetched.retry_count, 1)

    @patch("app.deliver.urlopen")
    def test_unknown_vendor_fails_immediately(self, mock_urlopen):
        """Unknown vendor is marked FAILED without making any HTTP request."""
        task = self.queue.add("key-unk", "unknown_vendor", {"x": 1})
        result = self.worker.deliver_one(task.id, task.idempotency_key,
                                          task.vendor_name, task.payload,
                                          task.retry_count)
        self.assertFalse(result)
        mock_urlopen.assert_not_called()
        fetched = self.queue.get("key-unk")
        self.assertEqual(fetched.status, TaskStatus.FAILED)
        self.assertIn("unknown vendor", fetched.last_error)



class TestDeliveryRouterEdgeCases(unittest.TestCase):
    """Edge cases for NotificationRouter."""

    def test_route_missing_url_field_raises_keyerror(self):
        """Vendor config missing 'url' field should raise KeyError."""
        vendors = {"bad_vendor": {"method": "POST"}}
        router = NotificationRouter(vendors)
        with self.assertRaises(KeyError):
            router.route("bad_vendor", {"x": 1})

    def test_route_empty_vendors_returns_none(self):
        """Empty vendors dict returns None for any vendor."""
        router = NotificationRouter({})
        result = router.route("anything", {"x": 1})
        self.assertIsNone(result)

    def test_route_empty_payload(self):
        """Empty payload is serialized as empty JSON object."""
        vendors = {
            "tv": {
                "url": "https://example.com/hook",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
            }
        }
        router = NotificationRouter(vendors)
        result = router.route("tv", {})
        self.assertIsNotNone(result)
        self.assertEqual(result.body, "{}")


class TestDeliveryWorkerEdgeCases(unittest.TestCase):
    """Edge cases for DeliveryWorker."""

    def setUp(self):
        self.queue = TaskQueue()
        self.vendors = {
            "tv": {
                "url": "https://test.example.com/notify",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
            }
        }
        self.router = NotificationRouter(self.vendors)
        self.worker = DeliveryWorker(
            self.queue, self.router,
            max_retries=3, retry_base_interval_sec=10, poll_interval_sec=3600,
        )

    def _add(self, key="edge-key"):
        return self.queue.add(key, "tv", {"msg": "hello"})

    @patch("app.deliver.urlopen")
    def test_deliver_http_429_retries_with_backoff(self, mock_urlopen):
        """HTTP 429 (rate limit) should trigger retry with backoff."""
        mock_resp = MagicMock()
        mock_resp.status = 429
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        task = self._add("key-429")
        result = self.worker.deliver_one(task.id, task.idempotency_key,
                                          task.vendor_name, task.payload,
                                          task.retry_count)
        self.assertFalse(result)  # Not delivered yet, will retry
        fetched = self.queue.get("key-429")
        self.assertEqual(fetched.status, TaskStatus.PENDING)
        self.assertEqual(fetched.retry_count, 1)

    @patch("app.deliver.urlopen")
    def test_deliver_http_3xx_triggers_retry(self, mock_urlopen):
        """HTTP 3xx (redirect) should trigger retry (not handled as success)."""
        mock_resp = MagicMock()
        mock_resp.status = 301
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        task = self._add("key-3xx")
        result = self.worker.deliver_one(task.id, task.idempotency_key,
                                          task.vendor_name, task.payload,
                                          task.retry_count)
        self.assertFalse(result)
        fetched = self.queue.get("key-3xx")
        self.assertEqual(fetched.status, TaskStatus.PENDING)
        self.assertEqual(fetched.retry_count, 1)

    @patch("app.deliver.urlopen")
    def test_deliver_timeout_triggers_retry(self, mock_urlopen):
        """TimeoutError triggers retry."""
        mock_urlopen.side_effect = TimeoutError("timed out")

        task = self._add("key-timeout")
        result = self.worker.deliver_one(task.id, task.idempotency_key,
                                          task.vendor_name, task.payload,
                                          task.retry_count)
        self.assertFalse(result)
        fetched = self.queue.get("key-timeout")
        self.assertEqual(fetched.status, TaskStatus.PENDING)
        self.assertEqual(fetched.retry_count, 1)

    @patch("app.deliver.urlopen")
    def test_deliver_connection_error_triggers_retry(self, mock_urlopen):
        """ConnectionError triggers retry."""
        mock_urlopen.side_effect = ConnectionError("connection refused")

        task = self._add("key-conn")
        result = self.worker.deliver_one(task.id, task.idempotency_key,
                                          task.vendor_name, task.payload,
                                          task.retry_count)
        self.assertFalse(result)
        fetched = self.queue.get("key-conn")
        self.assertEqual(fetched.status, TaskStatus.PENDING)
        self.assertEqual(fetched.retry_count, 1)

    @patch("app.deliver.urlopen")
    def test_deliver_empty_payload(self, mock_urlopen):
        """Empty payload {} can be delivered."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        task = self.queue.add("key-empty", "tv", {})
        result = self.worker.deliver_one(task.id, task.idempotency_key,
                                          task.vendor_name, task.payload,
                                          task.retry_count)
        self.assertTrue(result)
        fetched = self.queue.get("key-empty")
        self.assertEqual(fetched.status, TaskStatus.DELIVERED)

    @patch("app.deliver.urlopen")
    def test_worker_empty_queue_no_error(self, mock_urlopen):
        """Worker _run loop handles empty queue gracefully."""
        self.queue.add("k1", "tv", {"x": 1})
        # Deliver one task successfully
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        pending = self.queue.get_pending()
        for t in pending:
            self.worker.deliver_one(t.id, t.idempotency_key,
                                    t.vendor_name, t.payload, t.retry_count)

        # Queue should now be empty
        self.assertEqual(self.queue.get_pending(), [])



if __name__ == "__main__":
    unittest.main()
