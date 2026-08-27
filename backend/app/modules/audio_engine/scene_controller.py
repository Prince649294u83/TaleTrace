"""Scene evaluation controller with caching and concurrent deduplication.

Evaluates scene mood once per paragraph.  Concurrent requests for the same
paragraph coalesce into a single AI call.  AI failures return the most
recent valid decision rather than crashing the audio session.

The in-flight task dictionary is cleaned up in a ``finally`` block so the
invariant survives cancellation as well as normal completion and errors.

    SceneController.evaluate(pointer, paragraph)
            ↓
        cache hit?  → return immediately
        in-flight?  → await the existing task
        neither     → create_task(_fetch), register, await
            ↓
        SceneDecision
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from backend.app.modules.audio_engine.models import ReadingPointer, SceneDecision

logger = logging.getLogger(__name__)

_DEFAULT_SCENE = SceneDecision(
    scene_mood="neutral_narration",
    emotion="neutral",
    intensity=0.3,
    audio_tag="neutral_narration",
)

ALLOWED_AUDIO_TAGS = frozenset([
    "forest", "forest_night", "ocean", "river", "rain_light", "rain_heavy", 
    "thunderstorm", "wind", "desert", "mountain", "jungle", "swamp",
    "library", "tavern", "castle", "city", "city_old", "marketplace",
    "church", "classroom", "fireplace", "ship",
    "peaceful", "wonder", "suspense", "sorrow", "joy", "fear", "anger",
    "romance", "mystery", "comedy", "epic",
    "battle", "chase", "dungeon", "space", "magic", "stealth", "celebration",
    "morning", "night", "snowfall", "fog", "neutral_narration"
])


@dataclass
class SceneController:
    """Cache boundary between reading context and AI scene analysis.

    Evaluates scene mood once per paragraph. Concurrent requests for the
    same paragraph coalesce into a single AI call. AI failures return the
    most recent valid decision rather than crashing the audio session.

    The in-flight task dictionary is cleaned up in a finally block so the
    invariant survives cancellation as well as normal completion and errors.
    """

    ai: Any = None  # AiBridge | None
    _cache: dict[tuple[int, int], SceneDecision] = field(
        default_factory=dict, init=False
    )
    _in_flight: dict[tuple[int, int], asyncio.Task] = field(
        default_factory=dict, init=False
    )
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _last_valid: SceneDecision = field(default_factory=lambda: _DEFAULT_SCENE)

    async def evaluate(
        self, *, pointer: ReadingPointer, paragraph: str
    ) -> SceneDecision:
        """Return the scene for this paragraph, calling AI at most once.

        Cache key is ``(page_index, paragraph_index)``. Two concurrent calls
        for the same key coalesce into one AI call via the in-flight dict.
        """

        key = (pointer.page_index, pointer.paragraph_index)

        # Fast path: already cached.
        if key in self._cache:
            return self._cache[key]

        # No AI bridge: return last known or default.
        if self.ai is None:
            return self._last_valid

        # Deduplication: check/register under the lock.
        async with self._lock:
            # Double-check after acquiring lock.
            if key in self._cache:
                return self._cache[key]
            if key in self._in_flight:
                task = self._in_flight[key]
            else:
                task = asyncio.create_task(self._fetch(key, paragraph))
                self._in_flight[key] = task

        # Wait outside the lock so other keys can proceed concurrently.
        try:
            return await task
        except Exception:
            logger.warning("[scene] in-flight task failed for %s", key)
            return self._last_valid

    async def _fetch(
        self, key: tuple[int, int], paragraph: str
    ) -> SceneDecision:
        """The actual AI call. Runs at most once per key.

        Cleanup is in a ``finally`` block so the in-flight dict is cleared
        even if the task is cancelled or raises unexpectedly.
        """

        try:
            try:
                outcome = await self.ai.scene_mood(
                    paragraph=paragraph, page_number=key[0]
                )
                if outcome.ok:
                    audio_tag = outcome.data.get("audio_tag", "neutral_narration")
                    if audio_tag not in ALLOWED_AUDIO_TAGS:
                        logger.warning("[scene] AI generated invalid audio_tag '%s', normalizing to 'neutral_narration'", audio_tag)
                        audio_tag = "neutral_narration"
                        
                    decision = SceneDecision(
                        scene_mood=outcome.data.get("scene_mood", "neutral_narration"),
                        emotion=outcome.data.get("emotion", "neutral"),
                        intensity=outcome.data.get("intensity", 0.3),
                        audio_tag=audio_tag,
                    )
                else:
                    decision = self._last_valid
            except Exception:
                decision = self._last_valid

            self._cache[key] = decision
            if decision is not self._last_valid:
                self._last_valid = decision

            return decision
        finally:
            async with self._lock:
                self._in_flight.pop(key, None)

    def invalidate(self) -> None:
        """Clear cache on session reset. Does not cancel in-flight tasks."""
        self._cache.clear()
