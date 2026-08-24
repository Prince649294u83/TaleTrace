"""The stress harness and its synthetic pages, tested as the tools they are.

Two things are pinned here, and the first matters more than it looks.

**The synthetic pages must parse the way a real Vision response does.** The
generator's first version omitted `detectedBreak` from its symbols, which is
absent from no real Vision response ever returned. Nothing raised. The parser
simply read `space_after=False` for every word, joined the page into one
unspaced token, and every harness downstream reported a page of 57 words as a
page of 11 — while passing all of its own checks, because a stable wrong number
is still stable. A fixture that lies quietly is worse than one that breaks.

**The stress checks must be able to fail.** A harness whose assertions cannot
fire is a harness that reports green on a broken product, so the invariants are
tested against fabricated bad sessions as well as good ones.
"""

from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.modules.ocr.providers import GoogleVisionProvider
from scripts.stress_session import Sample, StressReport, assess
from scripts.synthetic_page import PAGES, render_page, vision_response


def _args(**overrides):
    """The harness defaults, overridable per test."""

    base = dict(word_drift=0, max_pending=64, max_queue=200, heap_kb_per_tick=1.0)
    base.update(overrides)
    return Namespace(**base)


class _Loop:
    """Only the two attributes `assess` reads. A real DeviceLoop would drag the
    whole runtime into a test about arithmetic."""

    def __init__(self, frames_processed: int = 100):
        self.frames_processed = frames_processed


def _samples(count: int = 10, **series) -> list[Sample]:
    """A clean session: everything flat, which every check should accept.

    Individual fields are overridden per test by passing a list, so each test
    changes exactly the one quantity it is about.
    """

    out = []
    for index in range(count):
        sample = Sample(tick=index * 100, session_seconds=index * 10.0)
        sample.memory_words = 264
        sample.memory_version = index
        sample.page_index = 1
        sample.audio_spoken = 4
        for name, values in series.items():
            setattr(sample, name, values[index])
        out.append(sample)
    return out


def _verdict(report: StressReport, fragment: str) -> bool:
    for check in report.checks:
        if fragment in check.label:
            return check.passed
    raise AssertionError(f"no check matching {fragment!r} in {[c.label for c in report.checks]}")


class TestSyntheticPages:
    """The fixture has to be indistinguishable from a real response to the parser."""

    def test_every_word_drawn_is_a_word_the_parser_recovers(self):
        image, grouped = render_page(PAGES[0])
        drawn = [word["text"] for paragraph in grouped for word in paragraph]

        parsed = GoogleVisionProvider.parse_response(vision_response(grouped))

        assert [word.text for word in parsed] == drawn
        assert image.shape[2] == 3

    def test_words_carry_a_space_after_them(self):
        """The regression. Without `detectedBreak` the page collapses to one token.

        Asserted on the parser's output rather than on the generated JSON,
        because the JSON being well-formed is not the property that failed —
        the property that failed is what the parser made of it.
        """

        _image, grouped = render_page(PAGES[0])

        parsed = GoogleVisionProvider.parse_response(vision_response(grouped))

        assert all(word.space_after for word in parsed), (
            "a word with no detected break is joined to the next one, which turns "
            "a 57-word page into an 11-word page without raising anything"
        )

    def test_paragraph_structure_survives_into_the_parse(self):
        """Merge Memory segments on these indices, so they are load-bearing."""

        _image, grouped = render_page(PAGES[1])

        parsed = GoogleVisionProvider.parse_response(vision_response(grouped))

        assert sorted({word.paragraph_index for word in parsed}) == list(range(len(grouped)))

    def test_boxes_describe_where_the_word_was_actually_drawn(self):
        """A response whose boxes disagree with the pixels makes every pointer
        assertion downstream meaningless, so the box is checked against the
        drawing order rather than merely for being present."""

        _image, grouped = render_page(PAGES[0])
        first_line = grouped[0][:5]

        lefts = [word["box"][0] for word in first_line]
        assert lefts == sorted(lefts), "words on one line must run left to right"
        for word in first_line:
            left, top, right, bottom = word["box"]
            assert right > left and bottom > top, f"{word['text']} has an inverted box"


class TestStressInvariants:
    """Each check, proven to fire. A green that cannot go red is not evidence."""

    def test_a_clean_session_passes_everything(self):
        report = StressReport(samples=_samples())

        assess(report, _Loop(), None, _args())

        assert all(check.passed for check in report.checks), [
            check.label for check in report.checks if not check.passed
        ]

    def test_merge_memory_that_keeps_growing_on_a_still_page_fails(self):
        """The duplication bug: the same page appended to itself, frame after frame."""

        words = [264] * 5 + [264 + 40 * n for n in range(5)]
        report = StressReport(samples=_samples(memory_words=words))

        assess(report, _Loop(), None, _args())

        assert not _verdict(report, "Merge Memory stops growing")

    def test_a_version_going_backwards_fails(self):
        versions = [0, 1, 2, 3, 4, 3, 6, 7, 8, 9]
        report = StressReport(samples=_samples(memory_version=versions))

        assess(report, _Loop(), None, _args())

        assert not _verdict(report, "version never goes backwards")

    def test_a_pointer_turning_back_a_page_fails(self):
        pages = [1, 1, 2, 2, 3, 2, 3, 3, 4, 4]
        report = StressReport(samples=_samples(page_index=pages))

        assess(report, _Loop(), None, _args())

        assert not _verdict(report, "never turns back to an earlier page")

    def test_a_pointer_moving_within_a_page_is_allowed(self):
        """A gesture legitimately moves the reader back a paragraph. Asserting
        monotonic paragraphs would fail every real session that used one."""

        paragraphs = [0, 1, 2, 1, 0, 2, 3, 1, 2, 3]
        report = StressReport(samples=_samples(paragraph_index=paragraphs))

        assess(report, _Loop(), None, _args())

        assert _verdict(report, "never turns back to an earlier page")

    def test_a_pending_backlog_that_never_drains_fails(self):
        pending = [0, 2, 8, 20, 45, 90, 140, 200, 260, 320]
        report = StressReport(samples=_samples(pending_events=pending))

        assess(report, _Loop(), None, _args())

        assert not _verdict(report, "pending events drain")

    def test_an_audio_queue_growing_with_tick_count_fails(self):
        queued = [0, 20, 60, 110, 170, 230, 300, 380, 470, 570]
        report = StressReport(samples=_samples(audio_queued=queued))

        assess(report, _Loop(), None, _args())

        assert not _verdict(report, "audio queue is bounded")

    def test_a_silent_session_reports_the_queue_as_untested_not_bounded(self):
        """Zero depth means two opposite things. Claiming the bound was proven
        when nothing was ever queued is the false green this guards."""

        report = StressReport(samples=_samples(audio_spoken=[0] * 10))

        assess(report, _Loop(), None, _args())

        with pytest.raises(AssertionError):
            _verdict(report, "audio queue is bounded")
        assert any("untested" in note for note in report.notes)

    def test_playback_stalled_with_sentences_queued_fails(self):
        """The defect this harness actually found: the playback loop had returned
        while sentences were still queued, so nothing would ever speak them."""

        report = StressReport(
            samples=_samples(
                audio_queued=[0, 0, 1, 1, 1, 1, 1, 1, 1, 0],
                audio_state=["playing"] * 2 + ["finished"] * 8,
            )
        )

        assess(report, _Loop(), None, _args())

        assert not _verdict(report, "never sits finished with sentences still queued")

    def test_a_drained_queue_at_shutdown_is_not_read_as_a_stall(self):
        """`finish()` legitimately ends finished-and-empty, and the final sample is
        taken after it — flagging that would fail every clean session."""

        report = StressReport(
            samples=_samples(
                audio_queued=[0] * 9 + [3],
                audio_state=["playing"] * 9 + ["finished"],
            )
        )

        assess(report, _Loop(), None, _args())

        assert _verdict(report, "never sits finished with sentences still queued")

    def test_a_heap_growing_faster_late_than_early_fails(self):
        heap = [100.0, 110.0, 118.0, 124.0, 128.0, 300.0, 600.0, 1000.0, 1500.0, 2100.0]
        report = StressReport(samples=_samples(heap_kb=heap))

        assess(report, _Loop(), None, _args())

        assert not _verdict(report, "heap stops growing")

    def test_a_session_that_read_nothing_fails_rather_than_passing_vacuously(self):
        """Without this, every check above is satisfied by a session that never ran."""

        report = StressReport(samples=_samples(memory_words=[0] * 10))

        assess(report, _Loop(frames_processed=0), None, _args())

        assert not _verdict(report, "actually read something")

    def test_too_few_samples_is_reported_rather_than_judged(self):
        report = StressReport(samples=_samples(count=3))

        assess(report, _Loop(), None, _args())

        assert not _verdict(report, "enough samples")
        assert len(report.checks) == 1, "no invariant should be judged on three samples"
