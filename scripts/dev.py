"""Both servers, one command.

    python scripts/dev.py

Starts the FastAPI backend on http://127.0.0.1:8000 and the Vite dev server on
http://localhost:5173, streams both logs to this terminal, and stops both on
Ctrl-C. Open the URL Vite prints — it proxies `/api` to the backend, so the
browser only ever talks to one origin and CORS stays a deployment concern.

Why a script rather than two terminals
--------------------------------------
Two terminals is two things to remember to stop. The failure that costs an
afternoon is a backend left running from yesterday on a stale database while the
new one silently fails to bind, so this owns both processes and takes both down
together — including when one of them dies on its own, which is the case two
terminals handle worst of all.

Reading still happens on the rig. This serves the *website*, which reviews what
the rig produced; it starts no session and touches no hardware. For a reading
run see `python -m backend.app.live_session --check` first, then
`python -m backend.app.live_session`.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

BACKEND_HOST, BACKEND_PORT = "127.0.0.1", 8000
BACKEND_URL = f"http://{BACKEND_HOST}:{BACKEND_PORT}"
FRONTEND_URL = "http://localhost:5173"


def _require_free_port() -> None:
    """Refuse to start on top of a backend that is already running.

    uvicorn's own message for an occupied port on Windows is
    `[WinError 10013] An attempt was made to access a socket in a way forbidden by
    its access permissions`, which reads like a firewall problem and is not one.
    The usual cause is a backend left over from earlier — the exact thing this
    script exists to prevent — so it is worth one socket and a sentence.

    Only the backend is checked. Vite picks the next free port by itself and says
    so, and its proxy follows it.
    """

    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((BACKEND_HOST, BACKEND_PORT))
        except OSError:
            sys.exit(
                f"Something is already listening on {BACKEND_URL}, most likely a "
                f"backend from an earlier run.\n"
                f"  Windows:  netstat -ano | findstr :{BACKEND_PORT}   then  "
                f"taskkill /F /PID <pid>\n"
                f"  macOS/Linux:  lsof -ti :{BACKEND_PORT} | xargs kill"
            )


def _npm() -> str:
    """The npm executable, or a message saying why there isn't one.

    Resolved through `shutil.which` because on Windows npm is `npm.cmd`, a shim
    that `subprocess` will not find by the bare name without a shell — and passing
    `shell=True` instead would hand the whole command line to cmd.exe for
    re-parsing.
    """

    npm = shutil.which("npm")
    if npm is None:
        sys.exit(
            "npm not found on PATH. Install Node.js (https://nodejs.org). The "
            "backend alone will run with:\n"
            "  python -m uvicorn backend.app.main:app --reload"
        )
    return npm


def _stop(name: str, process: subprocess.Popen) -> None:
    """End a child and everything it started.

    `terminate()` is not enough on Windows: npm is a `.cmd` shim whose real work
    happens in a node process it spawned, and killing the shim leaves node holding
    port 5173 — the next run then fails to bind for reasons that look nothing like
    this. `taskkill /T` walks the tree.
    """

    if process.poll() is not None:
        return
    print(f"stopping {name}...")
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def main() -> int:
    npm = _npm()
    _require_free_port()

    if not (FRONTEND / "node_modules").exists():
        print("installing frontend dependencies (first run only)...")
        if subprocess.run([npm, "install"], cwd=FRONTEND, check=False).returncode:
            return 1

    # Children inherit this terminal's stdout and stderr rather than being piped
    # through a reader thread: uvicorn's reload notices and Vite's build errors
    # arrive as they happen, in colour, and a crash traceback is not swallowed by
    # a pipe nobody is draining.
    processes: list[tuple[str, subprocess.Popen]] = []
    try:
        processes.append((
            "backend",
            subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--reload"],
                cwd=ROOT,
            ),
        ))
        processes.append((
            "frontend",
            subprocess.Popen([npm, "run", "dev"], cwd=FRONTEND),
        ))

        # Vite's own URL is printed by Vite, not repeated here: it takes the next
        # free port when 5173 is busy, and a banner insisting on 5173 would send
        # the reader to whatever else is on it.
        print(f"\n  backend   {BACKEND_URL}")
        print(f"  website   the Local: URL Vite prints (normally {FRONTEND_URL})")
        print("\nCtrl-C to stop both.\n")

        # Poll rather than `wait()` on one of them: if the backend dies at 02:00
        # the frontend must not be left serving a site whose every request 502s.
        while True:
            for name, process in processes:
                if process.poll() is not None:
                    print(f"\n{name} exited with {process.returncode}")
                    return process.returncode or 1
            time.sleep(0.3)
    except KeyboardInterrupt:
        print()
        return 0
    finally:
        for name, process in reversed(processes):
            _stop(name, process)


if __name__ == "__main__":
    raise SystemExit(main())
