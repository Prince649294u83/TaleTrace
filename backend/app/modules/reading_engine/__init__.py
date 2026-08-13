"""Reading Engine module boundary.

`ReadingEngine` is the implementation of the runtime chain; the Protocol in
`interfaces.py` remains the contract for the session/persistence side, which is
a separate concern and not yet backed.

Three layers, and it matters which one a caller reaches for:

    ReadingEngine   sequences the modules for one session
    ReadingRuntime  builds them and owns the Gesture->engine queue
    DeviceLoop      drives a runtime from the ESP32 rig

A route or a demo holds a `ReadingRuntime`. Only the hardware entry point holds a
`DeviceLoop`, because it is the only thing that should know a device exists.
"""

from backend.app.modules.reading_engine.ai_bridge import AiBridge, AiOutcome
from backend.app.modules.reading_engine.device_loop import DeviceLoop
from backend.app.modules.reading_engine.engine import ReadingEngine, RuntimeEvent
from backend.app.modules.reading_engine.interfaces import ReadingEngineInterface
from backend.app.modules.reading_engine.runtime import ReadingRuntime

__all__ = [
    "AiBridge",
    "AiOutcome",
    "DeviceLoop",
    "ReadingEngine",
    "ReadingEngineInterface",
    "ReadingRuntime",
    "RuntimeEvent",
]
