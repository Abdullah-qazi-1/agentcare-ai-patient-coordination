"""A minimal in-memory sliding-window rate limiter.

Good enough for this app's actual deployment shape — a single process backed by
SQLite, the same assumption `app/agents/graph.py`'s checkpointer already makes. A
multi-process deployment would need a shared store (Redis) instead; that's a real
infrastructure decision, not something to smuggle in as a silent default here.
"""

import threading
import time
from collections import defaultdict

_lock = threading.Lock()
_attempts: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(key: str, *, limit: int, window_seconds: float) -> bool:
    """Record an attempt for `key` and report whether it's still within the limit.

    Returns True if this attempt is allowed, False if `key` has already made `limit`
    attempts within the trailing `window_seconds`. Recording happens either way, so a
    caller past the limit can't reset their own window by retrying faster.
    """
    now = time.monotonic()
    cutoff = now - window_seconds
    with _lock:
        recent = [t for t in _attempts[key] if t > cutoff]
        allowed = len(recent) < limit
        recent.append(now)
        _attempts[key] = recent
    return allowed


def reset() -> None:
    """Clear all tracked attempts. Test-only — production has no need to reset this."""
    with _lock:
        _attempts.clear()
