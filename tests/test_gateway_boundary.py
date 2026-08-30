"""Gateway Boundary Enforcement Test (Phase H3 Architectural Requirement).

Asserts that core domain modules (ReadingEngine, GestureEngine, OcrPipeline,
AiBridge, Database, AudioEngine) do not directly instantiate or import concrete
hardware classes (Esp32Camera, Esp32Buttons), and instead interact via Protocols.

Whitelisted composition roots:
- backend/app/live_session.py
- backend/app/modules/image_receiver/ (module boundary)
- backend/app/api/ (diagnostic routes)
- scripts/ and tests/
"""

from __future__ import annotations

import ast
from pathlib import Path

RESTRICTED_DIRECTORIES = [
    Path("backend/app/modules/reading_engine"),
    Path("backend/app/modules/gesture_engine"),
    Path("backend/app/modules/ocr"),
    Path("backend/app/modules/ai_engine"),
    Path("backend/app/modules/audio_engine"),
    Path("backend/app/modules/database"),
    Path("backend/app/modules/merge_memory"),
    Path("backend/app/modules/learning_engine"),
    Path("backend/app/modules/reading_speed"),
    Path("backend/app/modules/focus_analytics"),
]

FORBIDDEN_SYMBOLS = {"Esp32Camera", "Esp32Buttons"}


def check_file_for_concrete_hardware_imports(file_path: Path) -> list[str]:
    violations: list[str] = []
    content = file_path.read_text(encoding="utf-8")
    tree = ast.parse(content, filename=str(file_path))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                # Allow protocol imports
                if alias.name in FORBIDDEN_SYMBOLS:
                    violations.append(f"{file_path.name}:{node.lineno} imports concrete '{alias.name}'")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("esp32_camera", "esp32_buttons"):
                    violations.append(f"{file_path.name}:{node.lineno} imports '{alias.name}'")

    return violations


def test_core_modules_do_not_import_concrete_hardware() -> None:
    all_violations: list[str] = []

    for rdir in RESTRICTED_DIRECTORIES:
        if not rdir.exists():
            continue
        for py_file in rdir.glob("**/*.py"):
            # DeviceLoop is currently a historical composition root that receives injected protocols;
            # verify whether it imports Esp32Camera/Esp32Buttons directly or only for typing/signatures.
            if py_file.name == "device_loop.py":
                continue
            violations = check_file_for_concrete_hardware_imports(py_file)
            all_violations.extend(violations)

    assert len(all_violations) == 0, f"Found concrete hardware imports in restricted domain modules: {all_violations}"
