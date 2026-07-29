# OCR module contracts

The OCR module accepts image data previously prepared by OpenCV and exposes a
normalized document hierarchy for future consumers. It does not perform image
processing or call Google Cloud Vision yet.

## Boundaries

- `OcrProcessorInterface` accepts `OcrProcessRequest` and represents the future
  Google Cloud Vision adapter boundary.
- `OcrParserInterface` translates a future provider response into
  `OcrProcessResponse`.
- `ProcessedImage` carries either encoded image bytes or an external image
  reference without depending on OpenCV types.
- `OcrProcessRequest.detection_type` is fixed to
  `DOCUMENT_TEXT_DETECTION` as the intended future provider operation.

## Normalized response hierarchy

`OcrProcessResponse` contains `OCRPage` values. Pages contain `OCRParagraph`
values, and paragraphs contain `OCRWord` values. The shared models carry
bounding boxes and optional confidence scores where recognition metadata is
available.

Provider SDK objects must remain behind the parser and processor boundaries so
the rest of TaleTrace depends only on shared Pydantic contracts.