"""Reading Engine module boundary.

`ReadingEngine` is the implementation of the runtime chain; the Protocol in
`interfaces.py` remains the contract for the session/persistence side, which is
a separate concern and not yet backed.
"""

from backend.app.modules.reading_engine.ai_bridge import AiBridge, AiOutcome
from backend.app.modules.reading_engine.engine import ReadingEngine, RuntimeEvent
from backend.app.modules.reading_engine.interfaces import ReadingEngineInterface
from backend.app.modules.reading_engine.runtime import ReadingRuntime

__all__ = [
    "AiBridge",
    "AiOutcome",
    "ReadingEngine",
    "ReadingEngineInterface",
    "ReadingRuntime",
    "RuntimeEvent",
]
