"""The scripted rehearsal has to end.

`Timeline`'s steps are keyed by sentence index, and one of them — the gesture
correction on page 2 — moves the reader's index *backwards*. So a step that can
fire whenever its index is current fires again the moment the reader re-reaches
it: back to sentence 13, forward to 15, back to 13, forever. The rehearsal never
reaches `SESSION_FINISHED`, and because every lap logs the same friction events it
looks like a busy session rather than a hung one.

`Timeline._fired` is the guard. This test is the thing that fails if it is ever
removed or narrowed — and it fails by assertion rather than by hanging the suite,
which is why the reader's own `advance` is counted against a ceiling instead of
trusting the loop to stop.

Once, not "eventually once": the two lookups and the single Meaning Mode request
are asserted too, because a step that re-fires on the way back through would
inflate exactly those counts while still terminating if the jump-back itself were
somehow bounded.
"""

from __future__ import annotations

import pytest

from backend.demo.fake_events import SESSION_ID, compressed_timeline
from backend.demo.fake_reader import ScriptedReader, build_content

# The rehearsal's own pacing, so this runs the script the demo runs rather than a
# simplified cousin of it: on pace, struggling, then slow with nothing to blame.
PAGE_PACES = {1: 1.0, 2: 0.45, 3: 0.55}


@pytest.fixture
def rehearsal():
    reader = ScriptedReader(content=build_content(pages=3), baseline_wpm=180.0)
    for page, pace in PAGE_PACES.items():
        reader.set_page_pace(page, pace)
    service, timeline = compressed_timeline(reader)
    return reader, service, timeline


def test_the_scripted_rehearsal_terminates(rehearsal):
    """Each scripted interruption fires once, so the replay reaches the end."""

    reader, service, timeline = rehearsal

    # Twice the script's length. A reader that has to advance more often than that
    # is re-reading ground it already covered, which is the loop.
    ceiling = reader.content.sentence_count * 2
    advance = reader.advance
    jump_back = reader.jump_back
    advances = 0
    jumps = 0

    def counted_advance():
        nonlocal advances
        advances += 1
        assert advances <= ceiling, (
            f"the reader advanced {advances} times through a "
            f"{reader.content.sentence_count}-sentence script — a scripted "
            "interruption re-fired after the jump back and the replay never ends"
        )
        return advance()

    def counted_jump_back(sentences: int = 2):
        nonlocal jumps
        jumps += 1
        return jump_back(sentences)

    reader.advance = counted_advance
    reader.jump_back = counted_jump_back

    timeline.run()
    analytics = service.finish_session(SESSION_ID)

    assert reader.finished, "the rehearsal stopped short of the last sentence"
    assert jumps == 1, "the gesture correction fired more than once"
    assert analytics.lookup_count == 2, "a lookup step re-fired on the way back"
    assert analytics.meaning_requests == 1
