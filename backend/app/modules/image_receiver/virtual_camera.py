"""A camera backed by files instead of by an ESP32.

Satisfies `CameraSource` exactly, so `DeviceLoop` cannot tell it from the rig:
`frame()` hands back encoded bytes, `decode()` hands back a BGR array, and both
return None in the situations the hardware returns None. Nothing downstream is
told which kind of camera it has.

Three shapes, because three questions get asked:

    single      one image, forever              does this photograph select the right word
    sequence    a list, one per frame           does the pointer survive a page turn
    stream      a list, paced by the clock      does a fifteen-minute session stay stable

Bytes from disk, not re-encoded
-------------------------------
`frame()` returns the file's bytes verbatim rather than decoding and re-encoding
them. Re-encoding would put a second lossy JPEG pass between the photograph and
Vision, so the OCR under test would be reading a slightly worse image than the one
on disk, and a word that failed to select would leave the question of whether the
harness caused it. It also means the Vision cache keys — SHA-256 of the
preprocessed bytes — match those a real capture of the same file would produce, so
a simulated run and an acceptance run share cache entries instead of each paying
for their own call.

Exhaustion is None, not an error
--------------------------------
A sequence that has run out reports None, the same answer the hardware gives when
the device does not respond. A loop driven past the end of its images therefore
idles rather than crashing, which is what makes `run(max_ticks=...)` safe to
over-provision: a test that wants "at least eight frames" can ask for twenty ticks
without having to supply twenty images.

The timeline starts at the first read
-------------------------------------
In `streaming` mode, elapsed time is measured from the first `frame()` call rather
than from construction — the same anchor `ScriptedButtons` uses for its schedule,
and for the same reason. A session is assembled before it is started: images are
read from disk, credentials resolved, a runtime built. Anchoring at construction
would let that setup time turn pages before the loop had ticked once, so a session
would open somewhere in the middle of its own book.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from backend.app.shared.clock import Clock, RealClock

logger = logging.getLogger(__name__)


class VirtualCamera:
    """Frames from disk or from memory, in place of the ESP32-CAM.

    Construct through one of the three classmethods rather than directly; the
    constructor takes the general form they all reduce to.
    """

    source_name = "virtual_cam"

    def __init__(
        self,
        frames: Sequence[bytes],
        *,
        repeat_last: bool = False,
        interval_seconds: float = 0.0,
        clock: Clock | None = None,
        paths: Sequence[Path] | None = None,
    ) -> None:
        self._frames = list(frames)
        self._repeat_last = repeat_last
        self._interval = float(interval_seconds)
        self._clock = clock if clock is not None else RealClock()
        # Kept so a failure can name the file rather than the index. "frame 34 of
        # 100" sends someone counting; "page_034.jpg" they can open.
        self._paths = list(paths) if paths is not None else []

        self._index = 0
        self._reads = 0
        self._started_at: float | None = None

    # ------------------------------------------------------------- constructors

    @classmethod
    def from_image(cls, path: str | Path, **kwargs: Any) -> VirtualCamera:
        """One photograph, returned on every frame.

        `repeat_last` is on because this models a reader holding a page still: the
        camera keeps capturing and keeps seeing the same page, which is precisely
        the condition Merge Memory's same-page detection has to survive without
        duplicating text.
        """

        resolved = Path(path)
        return cls([_read(resolved)], repeat_last=True, paths=[resolved], **kwargs)

    @classmethod
    def from_images(cls, paths: Sequence[str | Path], **kwargs: Any) -> VirtualCamera:
        """A list of photographs, one per frame, in order.

        Exhausts rather than repeating: the sequence is the script, and a caller
        that wants the last page held should say so with `repeat_last=True`.
        """

        resolved = [Path(p) for p in paths]
        return cls([_read(p) for p in resolved], paths=resolved, **kwargs)

    @classmethod
    def from_directory(
        cls, directory: str | Path, *, pattern: str = "*", limit: int | None = None, **kwargs: Any
    ) -> VirtualCamera:
        """Every image in a folder, sorted by name.

        Sorted because "the order the filesystem returned them" is not
        reproducible across machines, and a hundred-image OCR run that fails on
        image 34 has to fail on the same image next time to be worth anything.
        """

        root = Path(directory)
        if not root.is_dir():
            raise NotADirectoryError(f"not a directory: {root}")

        found = sorted(
            (p for p in root.glob(pattern) if p.suffix.lower() in _IMAGE_SUFFIXES),
            key=lambda p: p.name,
        )
        if not found:
            raise FileNotFoundError(f"no images matching {pattern!r} under {root}")
        if limit is not None:
            found = found[:limit]

        return cls.from_images(found, **kwargs)

    @classmethod
    def streaming(
        cls,
        paths: Sequence[str | Path],
        *,
        interval_seconds: float,
        clock: Clock,
        repeat_last: bool = True,
        **kwargs: Any,
    ) -> VirtualCamera:
        """Images paced by the clock, as a real camera in front of a real reader.

        The pacing matters for anything that measures time. A loop ticking ten
        times a second against a camera that hands over a new page every tick has
        the reader turning pages ten times a second, so every reading-speed and
        Reading Difficulty number computed from it is nonsense. With an interval,
        a page is held for as long as a reader would hold it, and the frames
        between are the same page again — which is also the load Merge Memory's
        same-page path actually sees.
        """

        resolved = [Path(p) for p in paths]
        return cls(
            [_read(p) for p in resolved],
            paths=resolved,
            interval_seconds=interval_seconds,
            clock=clock,
            repeat_last=repeat_last,
            **kwargs,
        )

    # -------------------------------------------------------------- the surface

    @property
    def configured(self) -> bool:
        """Always true if there is at least one frame. Mirrors `Esp32Camera`."""

        return bool(self._frames)

    @property
    def frames_read(self) -> int:
        return self._reads

    @property
    def failures(self) -> int:
        """Zero, always: a file that opened cannot fail to be read again.

        Present because `Esp32Camera` has it and the monitor reads it. A virtual
        camera that reported failures would be simulating a broken network, which
        is a different job — see `flaky` below for that.
        """

        return 0

    @property
    def exhausted(self) -> bool:
        """Whether every frame has been handed out and none will repeat.

        Never true in interval mode: the last page is held in front of the camera,
        which is what a reader who has stopped turning pages looks like.
        """

        if self._interval > 0.0 or self._repeat_last:
            return False
        return self._index >= len(self._frames)

    @property
    def current_index(self) -> int:
        """Which image is in front of the camera, 0-based.

        The two pacing modes track `_index` differently — in interval mode it is
        the frame showing now, in script mode the frame to hand over next — so
        anything that wants "the page being read" asks here rather than reading
        `_index` and getting it right half the time.
        """

        if self._interval > 0.0:
            return self._index
        return max(0, min(self._index - 1, len(self._frames) - 1))

    @property
    def current_path(self) -> Path | None:
        """The file behind the frame in front of the camera, if it came from one.

        Kept so a failure can name the file rather than the index. "frame 34 of
        100" sends someone counting; "page_034.jpg" they can open.
        """

        if not self._paths:
            return None
        return self._paths[min(self.current_index, len(self._paths) - 1)]

    def frame(self) -> bytes | None:
        """The frame in front of the camera right now, or None if there is none.

        The interval decides when the page *turns*, not whether the camera answers.
        That distinction is the whole of the fidelity here: a real ESP32-CAM
        responds to every capture request, and what changes slowly is the page in
        front of it. An earlier version returned None between scheduled frames, and
        the result was that gesture captures — which ask "what is in front of me
        right now", not "what is next" — failed in simulation in a way they never
        do on the rig. A virtual device that is *less* available than the hardware
        is not a conservative simulation; it exercises paths the product never
        reaches and hides the ones it does.
        """

        if not self._frames:
            return None

        if self._interval > 0.0:
            now = self._clock.now()
            if self._started_at is None:
                # The stream's timeline begins at the first read, not at
                # construction — the same anchor `ScriptedButtons` uses for its
                # schedule. A session is built before it is started (images read
                # from disk, credentials resolved), and anchoring at construction
                # would let that setup time turn pages before the loop had ticked.
                self._started_at = now

            # Derived from elapsed time rather than stepped once per call. Stepping
            # would make the current page depend on how often the camera happened to
            # be polled instead of on how much time had passed, so a loop that
            # skipped ahead — a stress run, a tick that ran long — would land on an
            # earlier page than the clock says. Recomputing from the origin also
            # cannot accumulate drift the way a rolling deadline can.
            elapsed = now - self._started_at
            self._index = min(int(elapsed // self._interval), len(self._frames) - 1)

            self._reads += 1
            return self._frames[self._index]

        # No interval: one image per call, the sequence as a script.
        if self._index >= len(self._frames):
            if not self._repeat_last:
                return None
            self._reads += 1
            return self._frames[-1]

        frame = self._frames[self._index]
        self._index += 1
        self._reads += 1
        return frame

    @staticmethod
    def decode(jpeg_bytes: bytes) -> Any | None:
        """Decode to BGR, through the same OpenCV path the hardware uses.

        Deliberately not a shortcut around the decode. The Gesture Engine's
        fingertip detection runs on whatever this returns, so a virtual camera that
        produced arrays some other way would be testing the detector against a
        different image than the rig would give it.
        """

        try:
            import cv2
            import numpy as np
        except ImportError:  # pragma: no cover - exercised by absence
            logger.warning("OpenCV is required to decode a camera frame")
            return None

        array = np.frombuffer(jpeg_bytes, np.uint8)
        return cv2.imdecode(array, cv2.IMREAD_COLOR)

    def reset(self) -> None:
        """Back to the first frame, counters cleared.

        For the stress harness, which replays one set of images many times over and
        should not need to re-read them from disk each pass.
        """

        self._index = 0
        self._reads = 0
        self._started_at = None


_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})


def _read(path: Path) -> bytes:
    """The file's bytes, or a readable error naming the file.

    Read eagerly at construction rather than lazily per frame so a mistyped path
    fails before the session starts. A hundred-image run that dies on image 60
    because of a typo has spent sixty images' worth of API calls to tell you
    something a directory listing knew.
    """

    if not path.is_file():
        raise FileNotFoundError(f"no such image: {path}")
    data = path.read_bytes()
    if not data:
        raise ValueError(f"image file is empty: {path}")
    return data
