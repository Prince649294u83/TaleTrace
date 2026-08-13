"""Time as an injected dependency, so a session can run faster than it reads.

Most of the system already took a clock: `ReadingSpeedService`, `ReadingEngine` and
`GesturePipeline` all accept `clock: Callable[[], float]`. `DeviceLoop` did not —
it called `asyncio.sleep(_TICK_SECONDS)` directly — and that one omission is what
made a fifteen-minute stability run take fifteen minutes and a five-hundred-image
stress run impractical.

    RealClock      time.monotonic + asyncio.sleep      production, and any test of timing itself
    VirtualClock   a number it advances itself         everything else

Reading time must stay honest
-----------------------------
The subtle part, and the reason this is not just "skip the sleeps". A virtual clock
that returns real wall time while sleeping zero seconds would report a
fifteen-minute session as having taken no time at all, and every metric computed
from elapsed time — observed WPM, Reading Difficulty, Idle Time — would be
garbage. Worse, it would be *plausible* garbage: the session would complete and
the numbers would look like numbers.

So `sleep()` advances the clock by the full amount asked for and only the *real*
wait is scaled. A session that believes an hour passed believes it consistently:
`now()`, the reading clock and the analytics all agree, and the only thing that
differs from a real hour is how long the operator waited. That is what makes an
accelerated run a valid rehearsal rather than a faster-but-different one.

    speed = 1.0    real time; a simulation you can watch
    speed = 0.2    1s of session per 0.2s of waiting
    speed = 0.0    no waiting at all; session time still advances

`speed=0.0` still yields to the event loop, because a loop that never awaits
starves whatever else the runtime has scheduled — the Audio Engine's queue drain
among them — and a stress run that deadlocks proves nothing about stability.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A source of time and of waiting.

    Both, together, on purpose: a component that reads the time from one place and
    waits somewhere else can be told an hour passed while it actually waits an
    hour, which is the bug this whole module exists to make impossible.
    """

    def now(self) -> float:
        """Seconds, monotonic. Only differences are meaningful."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait, and let the clock reflect having waited."""
        ...


class RealClock:
    """Wall time. What production uses, and what a test of timing itself uses."""

    __slots__ = ()

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class VirtualClock:
    """Session time the caller controls, advanced by sleeping or by hand.

    Not a mock: it is a real clock over a number instead of over a crystal. The
    session cannot tell, which is the requirement — a simulated run must exercise
    the same code paths in the same order, differing only in how long the operator
    waits for them.
    """

    __slots__ = ("_now", "speed", "slept_seconds", "sleeps")

    def __init__(self, *, start: float = 0.0, speed: float = 0.0) -> None:
        """`speed` is real seconds waited per session second. 0.0 waits not at all.

        Starting at 0 rather than at the real monotonic value makes a failing run's
        timestamps readable: "idle from 4.0s to 34.0s" is a sentence, and
        "idle from 913_204.118s" is not. Nothing may depend on the origin anyway,
        since a monotonic clock's absolute value is meaningless by contract.
        """

        if speed < 0.0:
            raise ValueError("speed cannot be negative; time does not run backwards")

        self._now = float(start)
        self.speed = float(speed)
        # Kept for assertions: a stress run wants to state how much session time it
        # covered, and a test of the loop's cadence wants to prove it waited at all.
        self.slept_seconds = 0.0
        self.sleeps = 0

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        """Move session time forward without waiting. Returns the new time.

        The seam the Focus engine's tests use: an idle period is thirty seconds
        between two pointer updates, and expressing it as `advance(30)` states the
        scenario directly instead of arranging for a real wait.
        """

        if seconds < 0.0:
            raise ValueError("cannot advance a monotonic clock backwards")
        self._now += seconds
        return self._now

    async def sleep(self, seconds: float) -> None:
        """Advance by `seconds` of session time, waiting `seconds * speed` of real.

        The zero-length yield at `speed=0` is not a formality. `DeviceLoop` awaits
        this once per tick and the Audio Engine drains its queue on other tasks; a
        sleep that never suspended would run the loop to completion without ever
        letting them proceed, and the run would report a stable audio queue only
        because nothing had been given the chance to touch it.
        """

        if seconds < 0.0:
            raise ValueError("cannot sleep for a negative duration")

        self._now += seconds
        self.slept_seconds += seconds
        self.sleeps += 1

        await asyncio.sleep(seconds * self.speed)
