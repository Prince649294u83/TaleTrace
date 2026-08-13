"""Everything, in order, in one command.

    python -m scripts.verify_all "C:/path/to/page.jpg"
    python -m scripts.verify_all page.jpg --offline    # no network at all
    python -m scripts.verify_all page.jpg --hardware   # also probe the ESP32 rig

Four things have to hold before this ships, and each is checked by a different
tool for a different reason:

  1. unit suite          every module does what that module intends
  2. acceptance run      one real photograph, all nine stages, end to end
  3. differential probe  the migrated chain does what `OCRandGESTURE/` did
  4. hardware check      the rig is reachable and the device loop can start

Run separately they are four commands with four sets of flags and four ways to
forget one. Run here they are one command with one exit status, and the summary
at the end says which of the four is the reason for it.

Cost
----
The same as running the four by hand, which is: one Google Vision call the first
time this image is ever seen and none afterwards, and a handful of Groq calls
split across the two keys. `--offline` drops every network call and still runs
every code path — worth doing first, because a failure there is a failure that
does not need a key to reproduce.

The stages are ordered cheapest-first and stop on the first failure unless
`--keep-going` is passed. There is no point paying for a Vision call to find out
whether the Audio Engine works when the unit suite already says it does not.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Outcome:
    """One stage: what it was called, what it ran, and how it went."""

    name: str
    command: list[str]
    code: int
    seconds: float
    skipped: bool = False
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.skipped or self.code == 0


def run(name: str, command: list[str], *, echo: bool = True) -> Outcome:
    """Run one stage, streaming its output so a long one does not look hung.

    Streamed rather than captured because these take minutes and print their own
    progress: a captured run shows nothing until it finishes, and the first
    question anyone asks of a silent terminal is whether it has crashed.
    """

    banner = f"  {name}  "
    print(f"\n\n{'=' * 74}")
    print(f"{banner:=^74}")
    print(f"{'=' * 74}")
    print(f"  $ {' '.join(command)}\n", flush=True)

    started = time.perf_counter()
    completed = subprocess.run(command, cwd=ROOT)
    elapsed = time.perf_counter() - started

    if echo:
        print(f"\n  -> exit {completed.returncode} in {elapsed:.1f}s", flush=True)
    return Outcome(name=name, command=command, code=completed.returncode, seconds=elapsed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "image",
        nargs="?",
        type=Path,
        help="a photograph of a page with a finger pointing at a word. Without "
        "one, only the unit suite runs.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="no network: the cached Vision response, and no Groq call on either key",
    )
    parser.add_argument(
        "--hardware",
        action="store_true",
        help="also probe the ESP32 rig (`live_session --check`); skipped by default "
        "because it fails on any machine that is not plugged into the device",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="run every stage even after one fails, instead of stopping at the first",
    )
    parser.add_argument(
        "--fresh-ocr",
        action="store_true",
        help="call Google Vision again even though a cached response exists",
    )
    parser.add_argument(
        "--json",
        default=".taletrace_cache/last_run.json",
        help="where the acceptance run writes its full result",
    )
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    python = sys.executable

    print("TaleTrace — full verification")
    print(f"  image     {args.image if args.image else 'none (unit suite only)'}")
    print(f"  network   {'disabled' if args.offline else 'live Vision cache + Groq'}")
    print(f"  hardware  {'probed' if args.hardware else 'skipped'}")

    stages: list[tuple[str, list[str]]] = [
        ("1. unit suite", [python, "-m", "pytest", "-q"]),
    ]

    if args.image:
        acceptance = [
            python,
            "-m",
            "scripts.validate_pipeline",
            str(args.image),
            "--json",
            args.json,
        ]
        probe = [python, "-m", "scripts.equivalence_probe", str(args.image)]
        if args.offline:
            acceptance.append("--no-groq")
            probe.append("--no-groq")
        if args.fresh_ocr:
            acceptance.append("--fresh-ocr")
        # The acceptance run first, so the Vision response is in the cache before
        # the probe wants it. Reversed, the probe would pay for the call and the
        # acceptance run would get it free — same total, but the stage that gets
        # billed would depend on the order, which makes the cost harder to reason
        # about than it needs to be.
        stages.append(("2. acceptance — one photograph, nine stages", acceptance))
        stages.append(("3. differential — migrated vs OCRandGESTURE", probe))

    if args.hardware:
        stages.append(
            ("4. hardware — ESP32 rig", [python, "-m", "backend.app.live_session", "--check"])
        )

    outcomes: list[Outcome] = []
    for name, command in stages:
        if outcomes and not args.keep_going and not all(o.passed for o in outcomes):
            outcomes.append(
                Outcome(
                    name=name,
                    command=command,
                    code=0,
                    seconds=0.0,
                    skipped=True,
                    reason="an earlier stage failed",
                )
            )
            continue
        outcomes.append(run(name, command))

    print(f"\n\n{'=' * 74}")
    print(f"{'  VERIFICATION SUMMARY  ':=^74}")
    print(f"{'=' * 74}")
    for outcome in outcomes:
        if outcome.skipped:
            mark, detail = "SKIP", outcome.reason
        elif outcome.passed:
            mark, detail = "PASS", f"{outcome.seconds:.1f}s"
        else:
            mark, detail = "FAIL", f"exit {outcome.code} after {outcome.seconds:.1f}s"
        print(f"  {mark}   {outcome.name.ljust(46)} {detail}")

    failed = [o for o in outcomes if not o.passed]
    if failed:
        print(f"\n  {len(failed)} stage(s) failed. Rerun just that stage with:")
        for outcome in failed:
            print(f"    {' '.join(outcome.command)}")
        return 1

    if args.image and not args.hardware:
        print("\n  everything passed. Hardware was not probed — add --hardware with the rig connected.")
    else:
        print("\n  everything passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
