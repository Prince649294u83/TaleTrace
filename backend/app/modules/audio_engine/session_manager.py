"""Session registry — one PlaybackEngine per reader.

A single module-level engine meant two readers shared one playback state: one
person's Meaning Mode pause would stop the other's book, and their pointers
would fight over the same queue. Engine state has to be keyed by session.

The registry is deliberately small. It creates engines on demand, hands them
out by id, and disposes of them on request. It does not own session lifecycle
(the Session module does) and it does not know what a reader is.
"""

import logging
from collections.abc import Callable

from backend.app.modules.audio_engine.models import PlaybackState, PlaybackStatus
from backend.app.modules.audio_engine.playback_engine import PlaybackEngine

logger = logging.getLogger(__name__)

# Above this, `get()` prunes finished sessions before creating another. A
# ceiling matters because nothing else reclaims engines if a client stops
# calling /audio/stop — the prototype has no session expiry.
_SOFT_LIMIT = 32


class AudioSessionManager:
    """Maps `session_id` to its own PlaybackEngine."""

    def __init__(self, *, engine_factory: Callable[[str], PlaybackEngine] | None = None) -> None:
        # Injectable so tests and the simulator can supply fake providers
        # without reaching into the engines afterwards.
        self._factory = engine_factory or (lambda sid: PlaybackEngine(session_id=sid))
        self._engines: dict[str, PlaybackEngine] = {}

    def get(self, session_id: str) -> PlaybackEngine:
        """Return the engine for `session_id`, creating it on first use."""

        engine = self._engines.get(session_id)
        if engine is not None:
            return engine

        if len(self._engines) >= _SOFT_LIMIT:
            self._prune_finished()

        engine = self._factory(session_id)
        self._engines[session_id] = engine
        logger.info(
            "[audio] session_created session_id=%r active=%d", session_id, len(self._engines)
        )
        return engine

    def has(self, session_id: str) -> bool:
        return session_id in self._engines

    def session_ids(self) -> list[str]:
        return list(self._engines)

    def statuses(self) -> list[PlaybackStatus]:
        """Snapshot every live session, for debugging and isolation checks."""

        return [engine.get_status() for engine in self._engines.values()]

    async def close(self, session_id: str) -> bool:
        """Stop and discard one session's engine.

        Stopping first matters: the engine may have a playback task running, and
        dropping the reference without cancelling it would leave the task
        speaking into a sink nobody is listening to.
        """

        engine = self._engines.pop(session_id, None)
        if engine is None:
            return False

        await engine.stop()
        logger.info(
            "[audio] session_closed session_id=%r active=%d", session_id, len(self._engines)
        )
        return True

    async def close_all(self) -> int:
        """Stop every session. Used on app shutdown and between tests."""

        count = 0
        for session_id in list(self._engines):
            if await self.close(session_id):
                count += 1
        return count

    def _prune_finished(self) -> None:
        """Drop engines that have nothing in flight.

        Only IDLE and FINISHED are removed, and neither has a live task to
        cancel — so this stays synchronous. A PAUSED session is left alone: the
        reader is mid-book in Meaning Mode and still expects to resume.
        """

        reclaimable = (PlaybackState.IDLE, PlaybackState.FINISHED)
        for session_id, engine in list(self._engines.items()):
            if engine.get_status().state in reclaimable:
                del self._engines[session_id]
                logger.info("[audio] session_pruned session_id=%r", session_id)


# Process-wide registry used by the router.
session_manager = AudioSessionManager()
