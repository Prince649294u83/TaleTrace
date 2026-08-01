"""Playback state machine.

Explicit states and explicit legal transitions, rather than a set of booleans
(`is_playing`, `is_paused`, `meaning_mode`, ...) that can drift into impossible
combinations. An invalid transition raises rather than silently corrupting state.

The machine also owns elapsed reading time, because the clock must freeze
whenever playback is suspended — Meaning Mode must not inflate reading-speed
analytics.
"""

import time
from collections.abc import Callable

from backend.app.modules.audio_engine.models import PauseReason, PlaybackState

# Legal transitions. Anything absent here is a bug in the caller.
_TRANSITIONS: dict[PlaybackState, frozenset[PlaybackState]] = {
    PlaybackState.IDLE: frozenset({PlaybackState.READY}),
    PlaybackState.READY: frozenset(
        {PlaybackState.PLAYING, PlaybackState.IDLE, PlaybackState.FINISHED}
    ),
    PlaybackState.PLAYING: frozenset(
        {
            PlaybackState.PAUSED,
            PlaybackState.WAITING_FOR_POINTER,
            PlaybackState.FINISHED,
            PlaybackState.IDLE,
        }
    ),
    PlaybackState.PAUSED: frozenset(
        {PlaybackState.PLAYING, PlaybackState.IDLE, PlaybackState.FINISHED}
    ),
    PlaybackState.WAITING_FOR_POINTER: frozenset(
        {
            PlaybackState.PLAYING,
            # Meaning Mode can be toggled while a Reading Update is settling.
            PlaybackState.PAUSED,
            PlaybackState.IDLE,
            PlaybackState.FINISHED,
        }
    ),
    # A finished page becomes READY again on page turn.
    PlaybackState.FINISHED: frozenset({PlaybackState.READY, PlaybackState.IDLE}),
}

# States in which the reading clock should be running.
_RUNNING_STATES = frozenset({PlaybackState.PLAYING, PlaybackState.WAITING_FOR_POINTER})


class InvalidTransition(RuntimeError):
    """Raised when a caller requests a transition the machine forbids."""

    def __init__(self, current: PlaybackState, requested: PlaybackState) -> None:
        super().__init__(
            f"Cannot move from {current.value!r} to {requested.value!r}"
        )
        self.current = current
        self.requested = requested


class PlaybackStateMachine:
    """Tracks playback state, pause reason, and elapsed reading time."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        # `clock` is injected so tests can advance time without sleeping.
        self._clock = clock
        self._state = PlaybackState.IDLE
        self._pause_reason: PauseReason | None = None
        self._elapsed_ms = 0
        self._running_since: float | None = None

    @property
    def state(self) -> PlaybackState:
        return self._state

    @property
    def pause_reason(self) -> PauseReason | None:
        return self._pause_reason

    def can_transition_to(self, target: PlaybackState) -> bool:
        if target == self._state:
            return True
        return target in _TRANSITIONS.get(self._state, frozenset())

    def transition_to(
        self, target: PlaybackState, *, pause_reason: PauseReason | None = None
    ) -> PlaybackState:
        """Move to `target`, updating the reading clock.

        Re-entering the current state is a no-op, so repeated pause or stop
        calls are safe.
        """

        if target == self._state:
            return self._state

        if not self.can_transition_to(target):
            raise InvalidTransition(self._state, target)

        was_running = self._state in _RUNNING_STATES
        will_run = target in _RUNNING_STATES

        if was_running and not will_run:
            self._freeze_clock()
        elif will_run and not was_running:
            self._start_clock()

        self._state = target
        self._pause_reason = pause_reason if target is PlaybackState.PAUSED else None

        if target in (PlaybackState.IDLE, PlaybackState.READY):
            self._elapsed_ms = 0
            self._running_since = None

        return self._state

    @property
    def elapsed_reading_ms(self) -> int:
        """Reading time so far, excluding every paused interval."""

        pending = 0.0
        if self._running_since is not None:
            pending = (self._clock() - self._running_since) * 1000
        return int(self._elapsed_ms + pending)

    def _start_clock(self) -> None:
        self._running_since = self._clock()

    def _freeze_clock(self) -> None:
        if self._running_since is not None:
            self._elapsed_ms += int((self._clock() - self._running_since) * 1000)
            self._running_since = None
