from collections import defaultdict, deque
from datetime import datetime, timedelta
from threading import Lock
from src.core.config import settings


class InMemoryRateLimiter:
    """Small-process limiter for the MVP; replace with Redis when scaled out."""

    def __init__(self, limit: int = 30, window_seconds: int = 60):
        self.limit = limit
        self.window = timedelta(seconds=window_seconds)
        self._events: dict[str, deque[datetime]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        now = datetime.utcnow()
        cutoff = now - self.window
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                retry_after = max(1, int((events[0] + self.window - now).total_seconds()))
                return False, retry_after
            events.append(now)
            return True, 0


ai_rate_limiter = InMemoryRateLimiter(limit=settings.AI_RATE_LIMIT_PER_MINUTE)
