"""Image receiver module boundary.

The devices the runtime polls, in two families that satisfy one pair of contracts:

    CameraSource    Esp32Camera      single JPEG frames from the rig
                    VirtualCamera    the same, from files on disk

    ButtonSource    Esp32Buttons     pin states turned into session events
                    VirtualButtons   the same events, pressed by a script

Both hardware sources are publish-only, never call another module, and are built
without touching the network so the runtime can start with no hardware present.
The virtual sources exist so that last property can be *used*: a full session,
every module, no rig on the desk. Which pair is in play is a construction-time
choice, and nothing downstream of `DeviceLoop` is told which it got.
"""

from backend.app.modules.image_receiver.esp32_buttons import ButtonState, Esp32Buttons
from backend.app.modules.image_receiver.esp32_camera import Esp32Camera
from backend.app.modules.image_receiver.protocols import ButtonSource, CameraSource
from backend.app.modules.image_receiver.virtual_buttons import VirtualButtons
from backend.app.modules.image_receiver.virtual_camera import VirtualCamera

__all__ = [
    "ButtonSource",
    "ButtonState",
    "CameraSource",
    "Esp32Buttons",
    "Esp32Camera",
    "VirtualButtons",
    "VirtualCamera",
]
