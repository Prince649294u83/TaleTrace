"""Pytest bootstrap for the TaleTrace project.

The gesture engine lives in Gesture/ and the OCR memory engine in
OCR_dynamicMem/, and both are imported as top-level packages (mirroring the
sys.path setup main_controller.py performs at runtime). Adding them here lets
the suite run from the repository root as well as from inside Gesture/.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))

for _subdir in ("Gesture", "OCR_dynamicMem"):
    _path = os.path.join(_ROOT, _subdir)
    if _path not in sys.path:
        sys.path.insert(0, _path)
