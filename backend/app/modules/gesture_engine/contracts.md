# Gesture Engine contracts

The Gesture Engine defines a provider-neutral boundary for future MediaPipe
Hands integration. MediaPipe is not imported, configured, or executed.

The `GestureEngineInterface` exposes:

- `detect_thumbs_up()`
- `detect_pointing()`
- `find_selected_word()`
- `map_finger_to_ocr()`

`FingerPoint` normalizes coordinates without leaking provider landmark types.
`GestureDetectionRequest` references a shared `Frame`; `OcrMappingRequest`
combines a point with normalized OCR pages. The same-named functions in
`placeholders.py` return static `pending` responses and contain no gesture,
coordinate, image, or OCR-selection logic.