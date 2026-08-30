# 02. Corpus Truth Findings

## Question 1: Why does the repository claim 7.jpeg is a hand image when the actual supplied 7.jpeg is not?

### Evidence
The repository's manifest file (`tests/hardware_corpus/manifest.json`, line 73) defines `7.jpeg` with:
- `canonical_corpus_name: "page_17_hand.jpg"`
- `layout_classification: "pointing_hand_frame"`
- `expected_raw_detection: "FINGER_DETECTED"`
- `expected_final_gesture: "SUCCESS"`
- Expected dimensions: `[1200, 1600]`

However, the actual external user-supplied `7.jpeg` file does not contain a finger and has dimensions `1600x1200` with EXIF orientation. 

### The Root Cause
The shadow test harness (`scripts/test_test_folder_software.py`) searches for `img_path = self.corpus_dir / filename`. Because the external test directory contains an actual `7.jpeg`, the test harness mistakenly feeds this actual image into the pipeline while asserting against the manifest's expectations for a completely different file (`page_17_hand.jpg`).

This divergence creates a false dichotomy where the system is evaluated against the wrong ground truth, masking true hardware integration failures and invalidating the external image's true properties (like EXIF orientation).

### Mitigation Strategy
- Disambiguate external test corpus files from canonical tracked test fixtures.
- Parse and enforce EXIF orientation at the absolute earliest point in the image pipeline so that OpenCV dimensions match the physical reality of the supplied file.
