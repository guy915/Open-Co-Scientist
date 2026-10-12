import logging
import logging.handlers
import queue
import sys
import threading
import time
from collections import deque


class CaptureQueue:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._records: tuple[deque[tuple[logging.LogRecord, int]], ...] = (deque(), deque())
        self._bytes = [0, 0]
        self._dropped = [0, 0]
        self._closing = False
        self._warned_at = float("-inf")

    def put_nowait(self, record: logging.LogRecord | None) -> None:
        with self._condition:
            if record is None:
                # A virtual sentinel cannot fail when either reserved pool is full.
                self._closing = True
                self._condition.notify_all()
                return
            priority = int(record.levelno >= logging.WARNING)
            size = (
                sys.getsizeof(record)
                + sys.getsizeof(record.__dict__)
                + sum(sys.getsizeof(value) for value in record.__dict__.values())
                + 1024
            )
            count_limit, byte_limit = ((384, 6 * 1024**2), (128, 2 * 1024**2))[priority]
            if (
                self._closing
                or len(self._records[priority]) >= count_limit
                or self._bytes[priority] + size > byte_limit
            ):
                self._dropped[priority] += 1
                now = time.monotonic()
                if now - self._warned_at >= 60:
                    self._warned_at = now
                    print(
                        "cosci: log capture overflow; dropped normal="
                        f"{self._dropped[0]} critical={self._dropped[1]}",
                        file=sys.stderr,
                    )
                return
            self._records[priority].append((record, size))
            self._bytes[priority] += size
            self._condition.notify()

    def get(self, block: bool = True) -> logging.LogRecord | None:
        with self._condition:
            while not any(self._records):
                if self._closing:
                    return None
                if not block:
                    raise queue.Empty
                self._condition.wait()
            priority = int(bool(self._records[1]))
            record, size = self._records[priority].popleft()
            self._bytes[priority] -= size
            return record

    def stats(self) -> dict[str, int]:
        with self._condition:
            return {
                "queued_records": sum(len(records) for records in self._records),
                "queued_bytes": sum(self._bytes),
                "dropped_normal": self._dropped[0],
                "dropped_critical": self._dropped[1],
            }

    def discard_pending(self) -> None:
        with self._condition:
            for index, records in enumerate(self._records):
                self._dropped[index] += len(records)
                records.clear()
                self._bytes[index] = 0
            self._condition.notify_all()


class CaptureListener(logging.handlers.QueueListener):
    queue: CaptureQueue

    def stop(self) -> None:
        self.enqueue_sentinel()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                self.queue.discard_pending()
                print(
                    "cosci: log capture shutdown timed out; pending records discarded",
                    file=sys.stderr,
                )
        self._thread = None
