# 05. OCR Findings

## Question 5: Does the physical-camera coordinate space actually match the OCR/selector coordinate space?

### Evidence
The repository has an explicit orientation problem, confirmed by the mismatch between `7.jpeg`'s physical EXIF orientation and the test harness's expectation. Furthermore, a review of the OCR provider integrations (e.g. Google Vision / OCR Space) reveals that the `detect_orientation=True` configuration reported by the test harness is NOT consistently applied to the actual production OCR API calls.

### Analysis
Without a single, authoritative rule dictating which pixel coordinate space is canonical (e.g. enforcing EXIF unwrapping at image capture before any downstream processing), the coordinate space utilized by the Camera, the OCR engine, the Gesture Engine, and the UI overlay can completely misalign. 

### Additional Findings
- **Meaning Context Ambiguity**: `ReadingRuntime._locate_line()` requires only two shared words to match context (`best_overlap < 2`). This is susceptible to misidentifying lines on long pages.
- **Normalizer Disconnected**: The new idempotent text normalizer (`backend/app/modules/preprocessing/text_normalizer.py`) is successfully isolated in tests but `MergeMemory` directly calls its own reconstruction methods, meaning the production pipeline does not actually route through the new standardizer.
- **`alds` Hardcoded Rule**: `RecognizedWord` fragment detection currently hardcodes the string `"alds"`. This is highly brittle and masks broader OCR parsing failures.
