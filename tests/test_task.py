"""TaskQueue unit tests."""

import unittest
from datetime import datetime, timezone, timedelta

from app.task import TaskQueue, TaskStatus

import threading


class TestTaskQueue(unittest.TestCase):
    """Test core TaskQueue operations."""

    def setUp(self):
        self.queue = TaskQueue()

    def test_add_and_get(self):
        """Task can be retrieved by idempotency_key after being added."""
        task = self.queue.add("key-1", "ad_system", {"event": "signup"})
        self.assertEqual(task.idempotency_key, "key-1")
        self.assertEqual(task.vendor_name, "ad_system")
        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertEqual(task.retry_count, 0)

        fetched = self.queue.get("key-1")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, task.id)

    def test_dedup_same_key_returns_existing(self):
        """Same idempotency_key returns the existing task (idempotent)."""
        t1 = self.queue.add("dup", "v1", {"a": 1})
        t2 = self.queue.add("dup", "v2", {"b": 2})
        self.assertEqual(t1.id, t2.id)
        self.assertEqual(t1.vendor_name, "v1")


    def test_get_pending_initially_returns_new_tasks(self):
        """Newly added tasks should appear in get_pending() results."""
        self.queue.add("k1", "v1", {"x": 1})
        self.queue.add("k2", "v2", {"y": 2})
        pending = self.queue.get_pending()
        self.assertEqual(len(pending), 2)

    def test_get_pending_does_not_return_future_retries(self):
        """Tasks with a future retry time should NOT be returned by get_pending()."""
        self.queue.add("k1", "v1", {"x": 1})
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        self.queue.update_status("k1", TaskStatus.PENDING, next_retry_at=future)

        pending = self.queue.get_pending()
        self.assertEqual(len(pending), 0)

    def test_get_pending_returns_due_retries(self):
        """Overdue next_retry_at values should be picked up by get_pending()."""
        self.queue.add("k1", "v1", {"x": 1})
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        self.queue.update_status("k1", TaskStatus.PENDING, next_retry_at=past)

        pending = self.queue.get_pending()
        self.assertEqual(len(pending), 1)

    def test_update_status_delivered(self):
        """After status is updated to DELIVERED, get_pending should not return it."""
        self.queue.add("k1", "v1", {"x": 1})
        self.queue.update_status("k1", TaskStatus.DELIVERED)

        pending = self.queue.get_pending()
        self.assertEqual(len(pending), 0)

        fetched = self.queue.get("k1")
        self.assertEqual(fetched.status, TaskStatus.DELIVERED)

    def test_update_status_with_fields(self):
        """update_status can update multiple fields at once."""
        self.queue.add("k1", "v1", {"x": 1})
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        self.queue.update_status("k1", TaskStatus.PENDING,
                                 retry_count=2, next_retry_at=future,
                                 last_error="HTTP 500")

        task = self.queue.get("k1")
        self.assertEqual(task.retry_count, 2)
        self.assertIsNotNone(task.next_retry_at)
        self.assertAlmostEqual(
            (task.next_retry_at - future).total_seconds(), 0, delta=1
        )
        self.assertEqual(task.last_error, "HTTP 500")
        self.assertEqual(task.status, TaskStatus.PENDING)

    def test_list_by_status(self):
        """List tasks by status."""
        self.queue.add("k1", "v1", {"x": 1})
        self.queue.add("k2", "v2", {"y": 2})
        self.queue.update_status("k1", TaskStatus.DEAD_LETTERED)

        dead = self.queue.list_by_status(TaskStatus.DEAD_LETTERED)
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0].idempotency_key, "k1")

        pending = self.queue.list_by_status(TaskStatus.PENDING)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].idempotency_key, "k2")

    def test_get_nonexistent_returns_none(self):
        """Querying a nonexistent key returns None."""
        self.assertIsNone(self.queue.get("nope"))

    def test_to_dict_serialization(self):
        """Task.to_dict() returns a JSON-serializable dictionary."""
        task = self.queue.add("serial", "v1", {"msg": "hello"})
        d = task.to_dict()
        self.assertEqual(d["idempotency_key"], "serial")
        self.assertEqual(d["status"], "pending")
        self.assertEqual(d["retry_count"], 0)
        self.assertIsNone(d["last_error"])
        self.assertIsNotNone(d["created_at"])
        self.assertIsNotNone(d["updated_at"])
        self.assertIsNotNone(d["next_retry_at"])



class TestTaskQueueEdgeCases(unittest.TestCase):
    """Edge cases for TaskQueue."""

    def setUp(self):
        self.queue = TaskQueue()

    def test_add_empty_payload(self):
        """Empty dict payload should be accepted."""
        task = self.queue.add("key-empty", "v1", {})
        self.assertIsNotNone(task)
        self.assertEqual(task.payload, {})

    def test_add_none_payload_is_stored(self):
        """None payload is auto-converted to empty dict to prevent JSON serialization issues."""
        task = self.queue.add("key-none", "v1", None)
        self.assertIsNotNone(task)
        self.assertEqual(task.payload, {})

    def test_get_pending_empty_queue(self):
        """get_pending on empty queue returns empty list."""
        self.assertEqual(self.queue.get_pending(), [])

    def test_list_by_status_empty_queue(self):
        """list_by_status on empty queue returns empty list."""
        self.assertEqual(self.queue.list_by_status(TaskStatus.PENDING), [])

    def test_update_status_nonexistent_key(self):
        """update_status on nonexistent key should not raise."""
        self.queue.update_status("nope", TaskStatus.DELIVERED)

    def test_get_pending_excludes_delivered(self):
        """DELIVERED tasks are excluded from get_pending."""
        self.queue.add("k1", "v1", {"x": 1})
        self.queue.update_status("k1", TaskStatus.DELIVERED)
        self.assertEqual(self.queue.get_pending(), [])

    def test_get_pending_excludes_failed(self):
        """FAILED tasks are excluded from get_pending."""
        self.queue.add("k1", "v1", {"x": 1})
        self.queue.update_status("k1", TaskStatus.FAILED)
        self.assertEqual(self.queue.get_pending(), [])

    def test_get_pending_excludes_dead_lettered(self):
        """DEAD_LETTERED tasks are excluded from get_pending."""
        self.queue.add("k1", "v1", {"x": 1})
        self.queue.update_status("k1", TaskStatus.DEAD_LETTERED)
        self.assertEqual(self.queue.get_pending(), [])

    def test_concurrent_adds(self):
        """Concurrent adds should not cause race conditions."""
        results = []
        errors = []

        def add_task(key):
            try:
                t = self.queue.add(key, "v1", {"n": key})
                results.append(t.id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_task, args=(f"concurrent-{i}",))
                   for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertEqual(len(set(results)), 50)

    def test_to_dict_with_none_last_error(self):
        """to_dict should handle None last_error."""
        task = self.queue.add("k1", "v1", {"x": 1})
        d = task.to_dict()
        self.assertIsNone(d["last_error"])

    def test_to_dict_does_not_mutate_task(self):
        """to_dict should not modify the original task."""
        task = self.queue.add("k1", "v1", {"x": 1})
        original_id = task.id
        task.to_dict()
        self.assertEqual(task.id, original_id)



if __name__ == "__main__":
    unittest.main()
