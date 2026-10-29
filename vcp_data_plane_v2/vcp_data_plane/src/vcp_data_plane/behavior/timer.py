"""Timer Scheduler for VCP periodic tasks.

Manages per-VCP recurring and one-shot timers:
  - Heartbeat: periodic, interval from profile
  - Meter values: periodic per transaction, interval from profile
  - Session timeout: one-shot, triggers auto-stop if configured
  - Reconnect backoff: exponential, clamped

All timers are asyncio Tasks.  The scheduler keeps track and cancels them
cleanly on VCP shutdown or state transition.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Timer callback type: async, no args, no return value
TimerCallback = Callable[[], Coroutine[Any, Any, None]]


class Timer:
    """A named repeating or one-shot asyncio task."""

    def __init__(self, name: str, coro_factory: TimerCallback, interval_s: float, repeat: bool = True) -> None:
        self.name = name
        self._factory = coro_factory
        self._interval = interval_s
        self._repeat = repeat
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return  # Already running
        self._task = asyncio.create_task(self._run(), name=self.name)

    def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        try:
            if self._repeat:
                while True:
                    await asyncio.sleep(self._interval)
                    await self._factory()
            else:
                await asyncio.sleep(self._interval)
                await self._factory()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("Timer %s raised an exception", self.name)


class VcpTimerScheduler:
    """Manages all timers for one VCP instance."""

    def __init__(self, vcp_id: str) -> None:
        self._vcp_id = vcp_id
        self._timers: dict[str, Timer] = {}

    # ------------------------------------------------------------------
    # High-level timer management
    # ------------------------------------------------------------------

    def start_heartbeat(self, interval_s: int, callback: TimerCallback) -> None:
        """Start repeating heartbeat timer."""
        self._start("heartbeat", callback, interval_s, repeat=True)

    def stop_heartbeat(self) -> None:
        self._cancel("heartbeat")

    def start_meter_values(self, interval_s: int, callback: TimerCallback) -> None:
        """Start repeating meter values timer (active during a transaction)."""
        self._start("meter_values", callback, interval_s, repeat=True)

    def stop_meter_values(self) -> None:
        self._cancel("meter_values")

    def start_session_timeout(self, timeout_s: int, callback: TimerCallback) -> None:
        """One-shot timer that fires if a session exceeds max duration."""
        if timeout_s <= 0:
            return  # 0 = unlimited
        self._start("session_timeout", callback, timeout_s, repeat=False)

    def cancel_session_timeout(self) -> None:
        self._cancel("session_timeout")

    def start_authorize_timeout(self, timeout_s: float, callback: TimerCallback) -> None:
        """One-shot: fires if CSMS doesn't respond to Authorize within timeout."""
        self._start("authorize_timeout", callback, timeout_s, repeat=False)

    def cancel_authorize_timeout(self) -> None:
        self._cancel("authorize_timeout")

    def start_boot_retry(self, interval_s: int, callback: TimerCallback) -> None:
        """One-shot retry timer for BootNotification rejection."""
        self._start("boot_retry", callback, interval_s, repeat=False)

    def cancel_all(self) -> None:
        """Cancel every timer (called on shutdown or connection loss)."""
        for name in list(self._timers):
            self._cancel(name)
        logger.debug("[%s] All timers cancelled", self._vcp_id)

    def running_timers(self) -> list[str]:
        return [name for name, t in self._timers.items() if t.is_running()]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start(self, name: str, callback: TimerCallback, interval_s: float, repeat: bool) -> None:
        self._cancel(name)
        timer = Timer(name=f"{self._vcp_id}:{name}", coro_factory=callback, interval_s=interval_s, repeat=repeat)
        self._timers[name] = timer
        timer.start()
        logger.debug("[%s] Timer '%s' started (interval=%.1fs repeat=%s)", self._vcp_id, name, interval_s, repeat)

    def _cancel(self, name: str) -> None:
        timer = self._timers.pop(name, None)
        if timer:
            timer.cancel()


# ---------------------------------------------------------------------------
# Reconnect backoff calculator (pure function, no state)
# ---------------------------------------------------------------------------

def reconnect_backoff(
    attempt: int,
    initial_s: float = 1.0,
    multiplier: float = 2.0,
    max_s: float = 60.0,
    jitter: bool = True,
) -> float:
    """Exponential backoff with optional ±25% jitter."""
    delay = min(initial_s * (multiplier ** attempt), max_s)
    if jitter:
        delay *= random.uniform(0.75, 1.25)
    return delay


async def boot_jitter_sleep(jitter_ms_range: tuple[int, int]) -> None:
    """Sleep a random duration within jitter_ms_range before connecting."""
    lo, hi = jitter_ms_range
    if hi <= 0:
        return
    delay_ms = random.randint(lo, hi)
    await asyncio.sleep(delay_ms / 1000.0)
