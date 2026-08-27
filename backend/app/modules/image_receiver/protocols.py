"""What the runtime requires of a device, stated once.

`DeviceLoop` has always accepted its camera and buttons as injected fields, so it
never depended on the ESP32 concretely — but the requirement was implicit, spelled
out only by whichever attributes the loop happened to touch. A test's fake was
correct by luck: it matched because someone read the loop and copied the surface.

These Protocols write that surface down. They add no behaviour and change no
call — `Esp32Camera` and `Esp32Buttons` already satisfy them as written, which is
the point. What they buy is that a virtual device can be checked against the same
contract the hardware meets, so "the runtime cannot tell the difference" becomes a
claim a type checker and a test can verify rather than one a reviewer has to trust.

    CameraSource   frame() -> bytes | None   decode(bytes) -> array | None
    ButtonSource   poll() -> [SessionEvent]  meaning_mode -> bool

Structural, not nominal: `Protocol` means an implementation does not import or
subclass anything here. A device is a device because it has the methods, which is
what lets the hardware classes stay unaware that an interface was extracted from
them.

Why `frame()` may return None
-----------------------------
Not an oversight worth tightening. The reference returned nothing rather than
raising when the device did not answer, because a reading session cannot end
because one frame over Wi-Fi was dropped. Any virtual camera must be able to say
"no frame right now" too — a timed stream that has not reached its next frame is
in exactly the state a real camera is in between captures, and a simulation that
could not express it would be easier than the hardware rather than equivalent to
it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from backend.app.shared.events import SessionEvent


@runtime_checkable
class CameraSource(Protocol):
    """A source of single frames, for OCR and for pointing.

    Two methods rather than one because the two consumers want different things
    from the same capture: OCR wants the bytes it will base64-encode for Vision,
    Gesture wants a decoded BGR array. Decoding centrally and re-encoding for
    Vision cost image quality for nothing, so the split is preserved.
    """

    source_name: str

    def frame(self) -> bytes | None:
        """One frame as encoded bytes, or None if none is available right now.

        None is a normal answer, not an error: a dropped Wi-Fi capture and a timed
        stream between frames both report it, and every caller is a loop that tries
        again on the next tick.
        """
        ...

    def decode(self, jpeg_bytes: bytes) -> Any | None:
        """The same frame as a BGR array for the Gesture Engine, or None.

        None means the bytes would not decode — a partial capture over Wi-Fi does
        produce that, so the loop treats it as a missed gesture rather than a
        crash.
        """
        ...


@runtime_checkable
class ButtonSource(Protocol):
    """A source of session events, polled once per tick.

    `poll()` reports *edges*, never levels. The wire says "the toggle is on"; the
    runtime needs "the toggle just turned on", because publishing on level would
    republish MEANING_MODE_ON for as long as the reader held the switch. Every
    implementation owes that conversion — a virtual source that fires on level
    would drive the runtime into a state the hardware never produces.
    """

    def poll(self) -> list[SessionEvent]:
        """The events this poll's state change implies, in application order.

        Empty when nothing changed, which is what makes this safe to call on every
        tick.
        """
        ...

    @property
    def meaning_mode(self) -> bool:
        """Whether the latching switch is currently held on.

        A level, deliberately, and the one the runtime is allowed to read: the
        loop skips frame capture while this is true, which is how the reference's
        nested hold-loop survives without a nested loop. Derived from the last
        poll rather than tracked separately, so it cannot disagree with the edges.
        """
        ...


@runtime_checkable
class DisplayTarget(Protocol):
    """A target that can show text on a physical display.

    The ESP32's OLED is the production implementation.  A ``FakeDisplay`` that
    records calls is the test one.  Failures are non-fatal: a missing or
    unreachable display logs a warning and reading continues.
    """

    async def show(self, text: str) -> None:
        """Send text to the display.  Must not raise on failure."""
        ...

