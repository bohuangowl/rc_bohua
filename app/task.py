"""Task model and in-memory queue module.

Defines the Task data structure and TaskQueue using a thread-safe in-memory dict.
In MVP phase, no database or message middleware is required; the queue is lost on restart.
"""

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskStatus(Enum):
    """Task status enumeration."""
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"


@dataclass
class Task:
    """A single notification delivery task."""
    id: int
    idempotency_key: str
    vendor_name: str
    payload: Dict[str, Any]
    status: TaskStatus = TaskStatus.PENDING
    retry_count: int = 0
    next_retry_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a serializable dict for JSON responses."""
        def _fmt(dt: Optional[datetime]) -> Optional[str]:
            return dt.isoformat() if dt else None
        return {
            "idempotency_key": self.idempotency_key,
            "vendor_name": self.vendor_name,
            "status": self.status.value,
            "retry_count": self.retry_count,
            "last_error": self.last_error,
            "created_at": _fmt(self.created_at),
            "updated_at": _fmt(self.updated_at),
            "next_retry_at": _fmt(self.next_retry_at),
        }


class TaskQueue:
    """Thread-safe in-memory task queue.

    Backed by a dict keyed by idempotency_key, providing natural idempotent dedup.
    Cleanup strategy: no active cleanup (MVP workloads are small); freed on process exit.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: Dict[str, Task] = {}
        self._next_id: int = 1

    def add(self, idempotency_key: str, vendor_name: str,
            payload: Any) -> Task:
        """Add a task. If idempotency_key exists, return existing task (idempotent dedup).

        None payload is silently converted to empty dict to avoid JSON serialization issues.
        """
        if payload is None:
            payload = {}
        with self._lock:
            existing = self._tasks.get(idempotency_key)
            if existing is not None:
                return existing
            now = datetime.now(timezone.utc)
            task = Task(
                id=self._next_id,
                idempotency_key=idempotency_key,
                vendor_name=vendor_name,
                payload=payload,
                next_retry_at=now,
            )
            self._next_id += 1
            self._tasks[idempotency_key] = task
            return task

    def update_status(self, idempotency_key: str, status: TaskStatus,
                      retry_count: Optional[int] = None,
                      next_retry_at: Optional[datetime] = None,
                      last_error: Optional[str] = None) -> None:
        """Update task status and timestamp. Only provided fields are changed."""
        with self._lock:
            task = self._tasks.get(idempotency_key)
            if task is None:
                return
            task.status = status
            task.updated_at = datetime.now(timezone.utc)
            if retry_count is not None:
                task.retry_count = retry_count
            if next_retry_at is not None:
                task.next_retry_at = next_retry_at
            if last_error is not None:
                task.last_error = last_error

    def get(self, idempotency_key: str) -> Optional[Task]:
        """Look up a task by its idempotency key."""
        with self._lock:
            return self._tasks.get(idempotency_key)

    def get_pending(self) -> List[Task]:
        """Retrieve all pending tasks whose retry time has arrived.

        Selection: status == PENDING and next_retry_at <= now.
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            return [
                t for t in self._tasks.values()
                if t.status == TaskStatus.PENDING
                and t.next_retry_at is not None
                and t.next_retry_at <= now
            ]

    def list_by_status(self, status: TaskStatus) -> List[Task]:
        """List tasks by status (useful for dead-letter queries)."""
        with self._lock:
            return [t for t in self._tasks.values() if t.status == status]
