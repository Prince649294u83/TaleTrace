"""Buttons pressed by a script instead of by a finger.

Satisfies `ButtonSource`, so `DeviceLoop` cannot tell it from the rig. The six
controls the product exposes, each producing exactly the event the hardware
produces:

    camera_on()             CAMERA_ON
    reading_update()        READING_UPDATE_REQUESTED
    set_meaning_mode(on)    MEANING_MODE_ON / MEANING_MODE_OFF
    audio_on()              SESSION_RESUMED
    audio_off()             SESSION_PAUSED
    camera_off()            CAMERA_OFF

The toggle's setter is `set_meaning_mode`, not `meaning_mode`, because the protocol
requires `meaning_mode` to be a *level* — `DeviceLoop` reads it as a bool to decide
whether to skip frame capture. A method of that name would shadow the property, and
a bound method is always truthy, so the loop would treat Meaning Mode as permanently
on and never capture another frame.

Two controls, six buttons
-------------------------
Worth stating plainly rather than hiding: the rig on the desk has *two* physical
controls, a momentary and a latching switch, and `Esp32Buttons` derives CAMERA_ON,
CAMERA_OFF, MEANING_MODE_ON/OFF and READING_UPDATE_REQUESTED from them. Audio
ON/OFF has no wire yet — `SESSION_PAUSED` and `SESSION_RESUMED` exist in the enum
and the Reading Engine handles both, but nothing on the current rig produces them.

So this class can drive a path the hardware cannot reach today. That is a real
asymmetry and it is left visible on purpose: it is the honest state of the product,
and pretending Audio ON/OFF does not exist would leave the Audio Engine's pause and
resume — named as the weakest part of the system — with no way to be exercised at
all. When a third control is wired, `Esp32Buttons` grows the two edges and this file
does not change.

Edges, not levels
-----------------
Same contract the hardware honours. A press is recorded as a pending event and
handed over by the next `poll()`, once. `meaning_mode` is a level derived from the
last state applied, so it cannot disagree with the edges — a caller that sets the
toggle on twice gets one MEANING_MODE_ON, exactly as a reader holding a switch does.

Presses queue rather than overwrite
-----------------------------------
Two presses between polls both survive, in order. Dropping the earlier one would
make a rapid double-press indistinguishable from a single one, and rapid presses
are specifically what the hardware-reliability tests need to reproduce.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence

from backend.app.shared.clock import Clock, RealClock
from backend.app.shared.events import SessionEvent


class VirtualButtons:
    """A scripted button source. Press through the named methods, not by event.

    Named methods rather than `press(SessionEvent.X)` because the mapping from
    control to event is part of what is being tested: a caller that could publish
    MEANING_MODE_ON directly could also publish it without ever setting the toggle,
    reaching a state the hardware cannot produce — and the runtime's behaviour there
    would say nothing about the product.
    """

    source_name = "virtual_buttons"

    def __init__(self, *, camera_on: bool = True) -> None:
        """`camera_on` announces the camera on the first poll, as a live rig does.

        On by default because `Esp32Buttons` publishes CAMERA_ON as soon as the
        device answers, and a session that never saw it has the Reading Engine
        believing the camera is unavailable while frames arrive — a state no real
        session is in.
        """

        self._pending: deque[SessionEvent] = deque()
        self._meaning_mode = False
        self._camera_on = False
        self._polls = 0
        self._presses = 0

        if camera_on:
            self.camera_on()

    # ------------------------------------------------------------ the six controls

    def camera_on(self) -> None:
        """The rig started answering. Ignored if it already had."""

        if self._camera_on:
            return
        self._camera_on = True
        self._emit(SessionEvent.CAMERA_ON)

    def camera_off(self) -> None:
        """The rig stopped answering. Ignored if it already had."""

        if not self._camera_on:
            return
        self._camera_on = False
        self._emit(SessionEvent.CAMERA_OFF)

    def reading_update(self) -> None:
        """The momentary button: re-read from where I am pointing.

        Suppressed while the toggle is on, matching `Esp32Buttons` and, behind it,
        the reference's `if btn_toggle: ... continue`. Without the suppression a
        reader holding the switch and pressing the button runs two gesture
        selections on one frame, and the second moves the pointer to a word they
        were only asking about.
        """

        if self._meaning_mode:
            return
        self._emit(SessionEvent.READING_UPDATE_REQUESTED)

    def set_meaning_mode(self, on: bool) -> None:
        """The latching switch. Emits an edge only when the level actually changes."""

        if bool(on) == self._meaning_mode:
            return
        self._meaning_mode = bool(on)
        self._emit(
            SessionEvent.MEANING_MODE_ON if self._meaning_mode else SessionEvent.MEANING_MODE_OFF
        )

    def audio_on(self) -> None:
        """Resume narration and the reading clock. See the module note on wiring."""

        self._emit(SessionEvent.SESSION_RESUMED)

    def audio_off(self) -> None:
        """Pause narration and the reading clock. See the module note on wiring."""

        self._emit(SessionEvent.SESSION_PAUSED)

    # -------------------------------------------------------------- the surface

    @property
    def configured(self) -> bool:
        """True always. Mirrors `Esp32Buttons`, which means "a URL is set"."""

        return True

    @property
    def meaning_mode(self) -> bool:
        """Whether the latching switch is currently held on. The protocol's level.

        Derived from the last state applied rather than tracked alongside it, so it
        cannot disagree with the edges already published.
        """

        return self._meaning_mode

    @property
    def polls(self) -> int:
        return self._polls

    @property
    def presses(self) -> int:
        """How many control actions produced an event. For rapid-press assertions."""

        return self._presses

    @property
    def pending(self) -> int:
        """Events queued and not yet polled."""

        return len(self._pending)

    def poll(self) -> list[SessionEvent]:
        """Everything pressed since the last poll, in order. Empty if nothing was."""

        self._polls += 1
        events = list(self._pending)
        self._pending.clear()
        return events

    def _emit(self, event: SessionEvent) -> None:
        self._pending.append(event)
        self._presses += 1


class ScriptedButtons(VirtualButtons):
    """Buttons pressed on a schedule, by session time.

    A `VirtualButtons` whose presses are declared up front instead of called by
    hand:

        ScriptedButtons(
            [
                (2.0, "reading_update"),
                (5.0, "meaning_on"),
                (9.0, "meaning_off"),
            ],
            clock=clock,
        )

    Why the schedule lives here and not in `DeviceLoop`
    --------------------------------------------------
    The loop could have grown a list of timed callbacks, and that would have been
    the wrong place for it: the loop would then contain a concept — "a press that
    has not happened yet" — that exists in no hardware session, and Hardware Mode
    would be carrying code only Simulation Mode uses. Here, a scheduled press
    becomes an ordinary press at the moment `poll()` notices its time has come,
    which is exactly what a finger does. The loop stays one loop.

    Times are session time from the clock, so a schedule written in seconds means
    the same thing at every speed: `(300.0, "meaning_on")` is five minutes into the
    reading whether the operator waited five minutes or six seconds.

    Due presses fire in order, and a poll that skips past several fires all of them
    rather than dropping to the latest. Coalescing would quietly turn a script into
    a different script.
    """

    source_name = "scripted_buttons"

    # The vocabulary a schedule may use. Explicit rather than `getattr` on the
    # instance, so a typo in a script names itself at construction instead of
    # silently never firing — a simulation that skipped the press it was written to
    # test would still pass, which is the worst way for this to fail.
    ACTIONS: dict[str, Callable[[VirtualButtons], None]] = {
        "camera_on": lambda b: b.camera_on(),
        "camera_off": lambda b: b.camera_off(),
        "reading_update": lambda b: b.reading_update(),
        "meaning_on": lambda b: b.set_meaning_mode(True),
        "meaning_off": lambda b: b.set_meaning_mode(False),
        "audio_on": lambda b: b.audio_on(),
        "audio_off": lambda b: b.audio_off(),
    }

    def __init__(
        self,
        schedule: Sequence[tuple[float, str]],
        *,
        clock: Clock | None = None,
        camera_on: bool = True,
    ) -> None:
        unknown = sorted({name for _, name in schedule} - set(self.ACTIONS))
        if unknown:
            raise ValueError(
                f"unknown button action(s): {', '.join(unknown)}. "
                f"Known: {', '.join(sorted(self.ACTIONS))}"
            )

        self._clock = clock if clock is not None else RealClock()
        # Sorted by time so the script's order on the page does not have to be its
        # order in time, and stably, so two presses at the same instant keep the
        # order they were written in.
        self._schedule = sorted(schedule, key=lambda item: item[0])
        self._fired = 0
        self._started_at: float | None = None

        super().__init__(camera_on=camera_on)

    @property
    def remaining(self) -> int:
        """Scheduled presses that have not fired yet."""

        return len(self._schedule) - self._fired

    @property
    def finished(self) -> bool:
        """Whether the whole script has been played."""

        return self._fired >= len(self._schedule)

    @property
    def elapsed(self) -> float:
        """Session seconds since the first poll. Zero before the script starts."""

        if self._started_at is None:
            return 0.0
        return self._clock.now() - self._started_at

    def poll(self) -> list[SessionEvent]:
        """Fire whatever is due, then hand over everything pending.

        Time starts at the *first poll*, not at construction. The session may be
        built well before it is started — credentials resolved, images read from
        disk — and anchoring to construction would let that setup time eat into the
        script, firing the first presses before the loop had ticked once.
        """

        now = self._clock.now()
        if self._started_at is None:
            self._started_at = now

        elapsed = now - self._started_at
        while self._fired < len(self._schedule) and self._schedule[self._fired][0] <= elapsed:
            _, action = self._schedule[self._fired]
            self._fired += 1
            self.ACTIONS[action](self)

        return super().poll()
