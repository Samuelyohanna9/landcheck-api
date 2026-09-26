from __future__ import annotations

"""Run one heavy function in a throw-away child process with a memory ceiling.

Why: a single analysis that balloons to several GB used to take the whole API worker down (the
kernel OOM-killer picks the biggest process), failing every other user's in-flight request with a
"Network Error". Here the parent watches the child's resident memory and kills only the child when
it crosses the limit, so the API keeps serving and the caller gets a clear, catchable error."""

import multiprocessing
import os
import threading
import time
from typing import Any, Callable


class IsolatedRunError(RuntimeError):
    pass


class IsolatedMemoryLimit(IsolatedRunError):
    pass


def _child_main(connection, func: Callable[..., Any], args: tuple, kwargs: dict) -> None:
    try:
        connection.send(("ok", func(*args, **kwargs)))
    except BaseException as exc:  # noqa: BLE001 - everything must reach the parent
        try:
            connection.send(("error", f"{type(exc).__name__}: {exc}"))
        except Exception:
            pass
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _rss_mb(pid: int) -> float | None:
    try:
        import psutil

        return psutil.Process(pid).memory_info().rss / 1e6
    except Exception:
        pass
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        return None
    return None


def max_rss_default_mb() -> int:
    try:
        return max(256, int(os.getenv("HAZARD_JOB_MAX_RSS_MB", "1400")))
    except ValueError:
        return 1400


def run_isolated(func: Callable[..., Any], *args: Any, max_rss_mb: int | None = None, timeout_s: float = 600, heartbeat: Callable[[], None] | None = None, **kwargs: Any) -> Any:
    """`func` must be importable at module level (it is pickled into the child)."""
    limit = max_rss_mb or max_rss_default_mb()
    ctx = multiprocessing.get_context("spawn")
    parent_end, child_end = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_child_main, args=(child_end, func, args, kwargs), daemon=True)
    process.start()
    child_end.close()

    outcome: dict[str, Any] = {}

    def receive() -> None:
        try:
            outcome["message"] = parent_end.recv()
        except Exception:
            outcome["message"] = None

    reader = threading.Thread(target=receive, daemon=True)
    reader.start()
    started = time.monotonic()
    last_beat = started
    peak = 0.0
    try:
        while reader.is_alive():
            reader.join(0.4)
            if not reader.is_alive():
                break
            if heartbeat is not None and time.monotonic() - last_beat >= 20:
                last_beat = time.monotonic()
                try:
                    heartbeat()
                except Exception:
                    pass
            rss = _rss_mb(process.pid)
            if rss is not None:
                peak = max(peak, rss)
                if rss > limit:
                    process.kill()
                    raise IsolatedMemoryLimit(f"analysis needed more than {limit} MB of memory (stopped at {rss:.0f} MB)")
            if time.monotonic() - started > timeout_s:
                process.kill()
                raise IsolatedRunError(f"analysis took longer than {int(timeout_s)} seconds")
            if not process.is_alive() and reader.is_alive():
                reader.join(1.0)
                break
    finally:
        if process.is_alive():
            process.kill()
        process.join(2)
        try:
            parent_end.close()
        except Exception:
            pass
    message = outcome.get("message")
    if not message:
        raise IsolatedRunError("analysis stopped unexpectedly (it may have run out of memory)")
    kind, payload = message
    if kind == "error":
        raise IsolatedRunError(payload)
    return payload
