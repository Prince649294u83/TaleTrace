"""Session event names shared across modules.

Events are strings rather than an enum because they travel over HTTP and appear in
logs. Every module that reports or handles an event uses these constants rather than
repeating the strings, so a typo becomes a NameError instead of a silent mismatch.
"""

from enum import Enum


class SessionEvent(str, Enum):
    """Events that modules report to each other during a reading session.

    Nobody owns all of these — they are reported by whoever detects them:
    - Gesture reports READING_POINTER_UPDATED, WORD_SELECTED and
      MEANING_REQUESTED. It reports only; it calls no module. A gesture that
      moved the pointer or started a lookup by reaching into another module
      directly is what made the pipeline impossible to test a page turn against.
    - Reading Engine reports SESSION_STARTED, SESSION_PAUSED, SESSION_RESUMED,
      PAGE_CHANGED
    - AI Engine reports MEANING_MODE_ON, MEANING_MODE_OFF, LOOKUP_COMPLETED
    - Audio Engine reports SESSION_FINISHED when playback ends
    - Camera/OCR reports CONTENT_UPDATED when a new frame is merged, and
      CAMERA_ON / CAMERA_OFF when the device starts or stops streaming
    """

    SESSION_STARTED = "SESSION_STARTED"
    SESSION_PAUSED = "SESSION_PAUSED"
    SESSION_RESUMED = "SESSION_RESUMED"
    SESSION_FINISHED = "SESSION_FINISHED"

    READING_POINTER_UPDATED = "READING_POINTER_UPDATED"
    PAGE_CHANGED = "PAGE_CHANGED"

    # Gesture resolved a fingertip to a specific word. Distinct from
    # READING_POINTER_UPDATED: that one says the reader has *moved on*, this one
    # says they singled a word out. The Reading Engine advances on the first and
    # ignores the second — pointing at a word is not the same as reading past it.
    WORD_SELECTED = "WORD_SELECTED"

    # The reader asked what a word means, by gesture. Reported by Gesture; the
    # Reading Engine decides whether that turns into MEANING_MODE_ON, because
    # entering Meaning Mode stops the reading clock and that is not Gesture's
    # call to make.
    MEANING_REQUESTED = "MEANING_REQUESTED"

    MEANING_MODE_ON = "MEANING_MODE_ON"
    MEANING_MODE_OFF = "MEANING_MODE_OFF"
    LOOKUP_COMPLETED = "LOOKUP_COMPLETED"

    CONTENT_UPDATED = "CONTENT_UPDATED"

    # The camera is the only source of both gesture and OCR input, so losing it
    # silences two modules at once. Reported separately from the session flags
    # because a camera outage is not a pause: the reader keeps reading, the
    # system simply stops being able to see them.
    CAMERA_ON = "CAMERA_ON"
    CAMERA_OFF = "CAMERA_OFF"
