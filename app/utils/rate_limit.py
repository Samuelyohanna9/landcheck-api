from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock
from time import monotonic


_windows: dict[str, deque[float]] = defaultdict(deque)
_lock = Lock()


def allow_request(key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Apply a small process-local limit to abuse-sensitive endpoints.

    The API can add a shared Redis limiter in front of multiple workers later. This local guard
    still protects a single worker and avoids trusting spoofable forwarded-IP headers.
    """
    now = monotonic()
    cutoff = now - max(1, int(window_seconds))
    with _lock:
        timestamps = _windows[key]
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if len(timestamps) >= max(1, int(limit)):
            retry_after = max(1, int(timestamps[0] + window_seconds - now + 0.999))
            return False, retry_after
        timestamps.append(now)
        if len(_windows) > 4096:
            for stale_key in list(_windows)[:512]:
                if not _windows[stale_key]:
                    _windows.pop(stale_key, None)
        return True, 0
