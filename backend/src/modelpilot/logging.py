from collections import deque
from threading import Lock

from modelpilot.schemas import RequestLog


class RequestLogStore:
    def __init__(self, limit: int = 500) -> None:
        self._items: deque[RequestLog] = deque(maxlen=limit)
        self._lock = Lock()

    def append(self, item: RequestLog) -> None:
        with self._lock:
            self._items.appendleft(item)

    def list(self) -> list[RequestLog]:
        with self._lock:
            return list(self._items)
