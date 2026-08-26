"""Real pages, read end to end, with every link in the chain asserted.

    python scripts/golden_session.py page.jpg
    python scripts/golden_session.py page.jpg --word impact
    python scripts/golden_session.py pages/ --minutes 2       # every page in a folder
    python scripts/golden_session.py pages/ --silent          # TTS off, reader's own pace

Not a unit test, and deliberately not in `tests/`. Every test in the suite
substitutes something: a fake OCR response, a stub AI engine, a hand-built
pointer. This substitutes nothing but the hardware. It is photographs of real
pages taken with a real hand pointing at a real word, read by the real Vision
parser, merged by the real Merge Engine, explained by the real Groq call, and
narrated by the real playback engine — and then it checks that each stage
actually received what the stage before it produced.

That is the failure it exists for. Every stage of this chain has passing unit
tests and the chain has still been broken twice: once because a retired Groq
model made every explanation an `{"error": ...}` payload that the reading engine
stored without complaint, and once because narration segmented, queued and
discarded every sentence while reporting 55 successful queue refreshes. Both
were invisible to a green suite and visible on the first line of this script's
output.

Cost
----
One Google Vision call per distinct frame on the first run, none after: the
responses are cached under `.taletrace_cache/vision/`, keyed on the preprocessed
bytes, so re-running is free and the numbers are identical. Two Groq calls per
run — one explanation, one end-of-session review — because those are what is
being checked; they are per session, not per frame.

Narration uses `AUDIO_PROVIDER=fake`, which records what it was told to speak
and touches neither the network nor a speaker. `AUDIO_PROVIDER=edge` narrates
out loud, which is worth doing once by ear and pointless in a checked run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

if __package__ is None:  # pragma: no cover - run as a file, not a module
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.core.environment import load_environment
from backend.app.modules.reading_engine.ai_bridge import bridge_for_session
from backend.app.shared.groq_keys import AI_ENGINE_VARIABLE

def _script_for(pages: int) -> tuple[tuple[float, str], ...]:
    """The scripted reader, in session seconds.

    Same shape as `simulated_session`'s and for the same reason, with one
    difference: the Meaning Mode hold is longer, because here a real Groq call has
    to land inside it. A hold that releases before the answer arrives is a green
    run that proves nothing about the AI.

    A multi-page run gets a second lookup. Pages turn every twelve seconds, so by
    30s the reader is two page turns in and the pointer, the queue and Merge Memory
    have each moved more than once — the state a first-page-only script never
    reaches, and the one where a lookup is most likely to land on the wrong word.
    """

    script = [
        (3.0, "reading_update"),
        (8.0, "meaning_on"),
        (20.0, "meaning_off"),
        (26.0, "reading_update"),
    ]
    if pages > 1:
        script += [
            (30.0, "meaning_on"),
            (42.0, "meaning_off"),
            (48.0, "reading_update"),
        ]
    return tuple(script)



class AudioEvents(logging.Handler):
    """The audio engine's own structured log, kept in order.

    Captured because the statistics cannot see order. `holds=1 pauses=1` is the
    same line whether narration resumed on the sentence the reader interrupted or
    restarted the page from its first sentence, and "55 queue refreshes" was the
    headline of a session that spoke nothing at all. The sequence is the claim; a
    count is only evidence that something happened.

    `PlaybackEngine._log` emits `logger.info("[audio:%s] %s%s", session, event,
    detail)`, so the event name and its fields arrive as `record.args` and the
    formatted message never has to be parsed back apart.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.rows: list[tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        args = record.args
        if isinstance(args, tuple) and len(args) == 3:
            self.rows.append((str(args[1]), str(args[2]).strip()))

    def named(self, event: str) -> list[str]:
        return [detail for name, detail in self.rows if name == event]

    def attach(self) -> AudioEvents:
        """Listen to the playback engine without changing what it prints.

        The engine logs at INFO and this script configures WARNING unless `-v`, so
        the module logger's own level has to come down or the records are never
        created for anyone to capture. Keeping them off the console is the root
        handler's job — see the note in `main`.
        """

        from backend.app.modules.audio_engine import playback_engine

        playback_engine.logger.setLevel(logging.INFO)
        playback_engine.logger.addHandler(self)
        return self


def _order_key(detail: str, field: str = "to") -> tuple[int, ...]:
    """The `(page, paragraph, sentence)` carried by `field=(...)` on a log line."""

    inside = detail.split(f"{field}=(", 1)[1].split(")", 1)[0]
    return tuple(int(part) for part in inside.split(","))


class Checks:
    """A PASS/FAIL list, printed as it goes and summarised at the end.

    A plain list rather than pytest because this is a *scenario*: the checks are
    not independent, they are a chain, and the useful output is the whole chain
    at once with the first broken link visible in place. `assert` would stop at
    that link and hide everything after it — which of the remaining stages also
    broke is exactly what an operator needs to know.
    """

    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def __call__(self, ok: object, name: str, detail: object = "") -> bool:
        self.rows.append((bool(ok), name, str(detail)))
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<34}  {detail}")
        return bool(ok)

    @property
    def failed(self) -> list[str]:
        return [name for ok, name, _ in self.rows if not ok]


async def run(args: argparse.Namespace) -> int:
    # Imported here, after `load_environment` in `main`: `build_session` resolves
    # keys at construction, so importing it earlier is harmless but calling it
    # before the `.env` is read produces a keyless session on a machine that has
    # a perfectly good one.
    from backend.app.modules.database.recording import record_finished_session
    from backend.app.simulated_session import build_session, collect_images

    try:
        images = [
            page for raw in args.images for page in collect_images(Path(raw).expanduser())
        ]
    except (FileNotFoundError, NotADirectoryError) as error:
        print(f"  {error}")
        return 1

    # Fake unless the operator asks for sound. Set before `build_session`, which
    # reads it to pick the provider.
    os.environ.setdefault("AUDIO_PROVIDER", args.audio)

    # Before the engine is built, so the first `start()` is on the record.
    events = AudioEvents().attach()

    script = _script_for(len(images))
    seconds = args.minutes * 60.0
    # Only the holds the session is long enough to reach. A `--minutes` short of the
    # script leaves the later ones unfired, and asserting on a lookup that was never
    # scheduled would fail the run for the operator's arithmetic.
    expected_holds = sum(1 for at, event in script if event == "meaning_on" and at < seconds)

    ai = bridge_for_session()
    print("TaleTrace — golden reading scenario")
    print(f"  pages       {len(images)}  ({', '.join(page.name for page in images)})")
    print(f"  OCR         recorded Vision responses, or one live call per new frame")
    print(f"  AI Engine   {'ready' if ai else f'off — {AI_ENGINE_VARIABLE} not set'}")
    print(
        "  narration   "
        + ("off — silent reading" if args.silent else os.environ["AUDIO_PROVIDER"])
    )
    print()

    loop, clock = build_session(
        images,
        speed=0.0,
        offline=args.offline,
        script=script,
        # `False`, not `None`: None means "caller did not choose" and gets a real
        # engine. See the branch in `build_session`.
        audio=False if args.silent else None,
        ai=ai,
    )
    analytics = await loop.run(max_ticks=int(seconds / loop.tick_seconds))

    engine = loop.runtime.engine
    row_id, note = record_finished_session(
        engine, analytics, name="Golden Session", source_reference=str(images[0])
    )

    check = Checks()
    print(f"  {'':4}  {'—— the page ——':<34}")
    check(analytics.pages_read >= 1, "a page was read", f"pages={analytics.pages_read}")
    check(analytics.words_read > 0, "the page had words in it", f"words={analytics.words_read}")
    check(
        analytics.baseline_wpm > 0 and analytics.session_wpm >= 0,
        "reading speed measured it",
        f"baseline={analytics.baseline_wpm:.1f} session={analytics.session_wpm:.1f}",
    )

    print(f"  {'':4}  {'—— the reader points ——':<34}")
    lookups = list(engine.lookups)
    check(loop.gestures_run > 0, "the gesture ran", f"gestures={loop.gestures_run}")
    check(lookups, "a word was selected", lookups)
    if args.word:
        check(args.word in lookups, f"the word was {args.word!r}", lookups)

    print(f"  {'':4}  {'—— Meaning Mode ——':<34}")
    # Every lookup the script got round to scheduling, not just one: a multi-page run
    # asks a second time after two page turns, and "at least one" would pass while the
    # later lookup silently did nothing.
    check(
        analytics.meaning_requests >= max(expected_holds, 1),
        "every scheduled lookup was made",
        f"requests={analytics.meaning_requests} scheduled={expected_holds}",
    )
    explained = engine.explanation
    # `ok` and a non-empty `full_explanation` are two different facts: a retired
    # model returns a *successful* HTTP call carrying `{"error": ...}`, which is
    # what made this chain look healthy for a whole milestone.
    payload = dict(getattr(explained, "data", {}) or {})
    check(
        explained is not None and getattr(explained, "ok", False),
        "the AI Engine answered",
        getattr(explained, "error", "") or "ok",
    )
    check(
        payload.get("full_explanation"),
        "the answer has an explanation in it",
        str(payload.get("oled_text") or "")[:48],
    )

    if not args.silent:
        print(f"  {'':4}  {'—— narration ——':<34}")
        spoken = engine.audio.final_statistics if engine.audio is not None else None
        check(spoken is not None, "playback ran and was stopped", "statistics present")
        if spoken is not None:
            check(
                spoken.sentences_spoken > 0,
                "sentences were actually spoken",
                f"{spoken.sentences_spoken} sentences, {spoken.words_spoken} words",
            )
            check(
                spoken.queue_refreshes > 0,
                "Merge Memory refreshed the queue",
                f"refreshes={spoken.queue_refreshes} "
                f"stale_rejected={spoken.stale_updates_rejected}",
            )
            # The reader held Meaning Mode, so narration must have stopped for it and
            # then carried on: a hold that never paused, or a pause that never
            # resumed, both leave a sentence count that looks fine.
            check(
                spoken.meaning_mode_count >= max(expected_holds, 1),
                "narration held for every lookup",
                f"holds={spoken.meaning_mode_count} pauses={spoken.pause_count}",
            )
            check(
                spoken.reading_updates > 0,
                "narration followed the pointer",
                f"pointer moves={spoken.reading_updates}",
            )

        # The counts above say narration happened; these say it happened in the right
        # order. Every one of these is a sequence the unit suite proves in isolation
        # with a hand-built pointer — here they have to hold with the real Reading
        # Engine driving, on real OCR text, with Merge Memory refreshing underneath.
        print(f"  {'':4}  {'—— the ordered sequence ——':<34}")
        holds = [line for line in events.named("paused") if "reason='meaning_mode'" in line]
        resumes = events.named("resumed")
        check(
            len(holds) >= max(expected_holds, 1) and len(resumes) >= len(holds),
            "every hold was released",
            f"{len(holds)} holds, {len(resumes)} resumes",
        )
        for index, hold in enumerate(holds):
            if index >= len(resumes):
                break
            # `requeued` and `next_sentence` are each the last field of their line, so
            # the tail after the `=` is the sentence and needs no parsing. Equal means
            # narration carried on where the reader cut it off; a page that restarted
            # from the top would put its first sentence on the right-hand side.
            interrupted = hold.split("requeued=", 1)[-1]
            continued = resumes[index].split("next_sentence=", 1)[-1]
            check(
                interrupted == continued,
                f"hold {index + 1} resumed its own sentence",
                continued[:44],
            )

        moves = [_order_key(line) for line in events.named("pointer_changed")]
        check(moves, "the pointer moved during narration", moves)
        # A seek is allowed to skip forward over text the reader has passed and nothing
        # else. Backwards would mean narration re-reading what was already heard.
        check(
            all(a <= b for a, b in zip(moves, moves[1:])),
            "and never seeked backwards",
            " -> ".join(str(m) for m in moves),
        )

        refreshes = events.named("queue_refreshed")
        in_flight = sum("anchor_in_flight=True" in line for line in refreshes)
        # Both halves of one branch, and the second number is the one that matters: a
        # refresh arriving with nothing being spoken is the case that used to discard
        # the anchor sentence, and it is the case a real OCR page hits most.
        check(
            in_flight and len(refreshes) - in_flight,
            "refresh exercised both anchor paths",
            f"{in_flight} with a sentence in flight, {len(refreshes) - in_flight} without",
        )
    else:
        refreshes = []
        print(f"  {'':4}  {'—— silent reading ——':<34}")
        # TTS switched off is a reader, not a broken engine, and the difference has to
        # be visible downstream: with no playback there is nothing to take `words_read`
        # from, so it comes from the pointer, and `tts_assisted` is the flag the
        # website reads to know which of the two sessions it is looking at.
        check(engine.audio is None, "no playback engine was built", "audio=None")
        check(
            analytics.tts_assisted is False,
            "the session is marked unassisted",
            f"tts_assisted={analytics.tts_assisted}",
        )
        check(not events.rows, "the audio engine never ran", f"{len(events.rows)} events")
        check(
            analytics.words_read >= analytics.pages_read,
            "words were counted from the pointer",
            f"words={analytics.words_read} over {analytics.pages_read} pages",
        )

    if len(images) > 1:
        print(f"  {'':4}  {'—— across pages ——':<34}")
        # Pages the camera got round to offering, not files on disk: a `--minutes`
        # shorter than `pages * 12s` leaves the tail unread, and that is the operator's
        # arithmetic rather than a failure.
        check(
            analytics.pages_read > 1,
            "more than one page was read",
            f"pages={analytics.pages_read} of {len(images)} offered",
        )
        check(
            len(engine.focus_report.paragraphs) > 1 if engine.focus_report else False,
            "focus saw more than one paragraph",
            f"{len(engine.focus_report.paragraphs) if engine.focus_report else 0} paragraphs",
        )
        if refreshes:
            # The anchor is where the reader was when Merge Memory rewrote the text. If
            # every refresh anchors on page 0, the queue never followed the reader off
            # the first page, and whatever was spoken later came from the wrong page.
            #
            # Not `page_changed`: that fires from `_count_spoken`, so it only marks
            # pages narration finished a sentence *on*. A page the reader passed
            # through without a sentence completing is a real page and logs nothing.
            pages = [_order_key(line, "anchor")[0] for line in refreshes]
            check(
                len(set(pages)) > 1,
                "the queue followed the reader across pages",
                f"refresh anchors on pages {sorted(set(pages))}",
            )
            check(
                all(a <= b for a, b in zip(pages, pages[1:])),
                "and never anchored back on an earlier page",
                f"{pages[0]} -> {pages[-1]} over {len(pages)} refreshes",
            )

    print(f"  {'':4}  {'—— the session review ——':<34}")
    review = engine.review
    data = dict(getattr(review, "data", {}) or {})
    check(review is not None and getattr(review, "ok", False), "the review was written",
          getattr(review, "error", "") or "ok")
    check(data.get("session_summary"), "it has a summary", str(data.get("session_summary"))[:48])
    check(data.get("flashcards"), "it has flashcards", f"{len(data.get('flashcards') or [])} cards")
    check(data.get("quiz"), "it has a quiz", f"{len(data.get('quiz') or [])} questions")
    check(data.get("words_learned"), "it has words learned", data.get("words_learned"))

    print(f"  {'':4}  {'—— reading focus ——':<34}")
    focus = engine.focus_report
    check(focus is not None, "a focus report exists", "")
    if focus is not None:
        # The same session, not a second one built alongside it. This is the
        # check that a focus report computed from a *different* session's
        # paragraphs would fail, which no unit test can see.
        check(
            focus.session_id == engine.session_id,
            "it is this session's report",
            f"{focus.session_id} == {engine.session_id}",
        )
        check(focus.paragraphs, "it has paragraphs", f"{len(focus.paragraphs)} paragraphs")

    print(f"  {'':4}  {'—— the website can see it ——':<34}")
    check(row_id, "a session row was written", row_id or note)

    print()
    print(f"  session time    {clock.now():.1f}s over {clock.sleeps} ticks")
    print(f"  frames read     {loop.frames_processed}")
    if check.failed:
        print(f"  {len(check.failed)} of {len(check.rows)} checks failed:")
        for name in check.failed:
            print(f"    - {name}")
        return 1
    print(f"  all {len(check.rows)} checks passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read real pages end to end and assert every stage received the last."
    )
    parser.add_argument(
        "images",
        nargs="+",
        help="photographs of pages with a finger pointing at a word, or directories of them",
    )
    parser.add_argument(
        "--word",
        default=None,
        help="the word the finger is on; asserted against what the gesture selected",
    )
    parser.add_argument(
        "--minutes",
        type=float,
        default=1.0,
        help="session length in reading time, not waiting time (default: 1)",
    )
    parser.add_argument(
        "--audio",
        choices=("fake", "edge", "offline"),
        default="fake",
        help="fake records what it would say; edge speaks out loud (default: fake)",
    )
    parser.add_argument(
        "--silent",
        action="store_true",
        help="no narration at all — a reader with TTS switched off, measuring their own pace",
    )
    parser.add_argument(
        "--live-ocr",
        dest="offline",
        action="store_false",
        help="force a fresh Google Vision call instead of using the cached response",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show module logs")
    parser.set_defaults(offline=True)
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - not a tty
            pass

    level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    # `basicConfig` leaves its handler at level 0, so a record that reaches root by
    # propagation prints whatever root's own level says — and `AudioEvents` has to
    # put the playback logger at INFO to capture the sequence at all. Without this
    # the console gets one refresh line per OCR frame above the results.
    for handler in logging.getLogger().handlers:
        handler.setLevel(level)
    load_environment()

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
