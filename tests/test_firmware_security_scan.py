"""Firmware Static Security Scan (Phase H3 Architectural Requirement).

Asserts that no executable cloud credentials, tokens, or external API endpoints
are hardcoded into ESP32 firmware files (.ino, .cpp, .h).
Distinguishes EXECUTABLE_CODE from COMMENT_OR_EXAMPLE.
"""

from __future__ import annotations

import re
from pathlib import Path

# Targeted forbidden cloud patterns
FORBIDDEN_CLOUD_PATTERNS = [
    r"api\.groq\.com",
    r"api\.ocr\.space",
    r"vision\.googleapis\.com",
    r"speech\.platform\.bing\.com",
    r"GROQ_API_KEY",
    r"FREESOUND_API_KEY",
    r"OCR_API_KEY",
    r"OPENAI_API_KEY",
    r"GOOGLE_APPLICATION_CREDENTIALS",
    r"AWS_ACCESS_KEY",
    r"AZURE_",
    r"Authorization:\s*Bearer\s+[A-Za-z0-9_\-\.]+",
]

FIRMWARE_SEARCH_PATHS = [
    Path("buttons_and_oled.ino"),
    Path("backend/app/OCRandGESTURE/espcam"),
]


def classify_line(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
        return "COMMENT_OR_EXAMPLE"
    return "EXECUTABLE_CODE"


def test_firmware_has_zero_executable_cloud_credentials() -> None:
    violations: list[dict[str, str]] = []

    for path in FIRMWARE_SEARCH_PATHS:
        if path.is_file():
            files = [path]
        elif path.is_dir():
            files = list(path.glob("**/*.ino")) + list(path.glob("**/*.cpp")) + list(path.glob("**/*.h"))
        else:
            continue

        for fpath in files:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
            for line_idx, line in enumerate(content.splitlines(), start=1):
                for pattern in FORBIDDEN_CLOUD_PATTERNS:
                    if re.search(pattern, line, re.IGNORECASE):
                        classification = classify_line(line)
                        if classification == "EXECUTABLE_CODE":
                            violations.append({
                                "file": str(fpath),
                                "line_number": str(line_idx),
                                "line": line.strip(),
                                "pattern": pattern,
                                "classification": classification,
                            })

    assert len(violations) == 0, f"Found executable cloud credentials/endpoints in firmware: {violations}"
