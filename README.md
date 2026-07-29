# TaleTrace

TaleTrace is a hackathon project. The current implementation includes only the
first OCR testing milestone: one synchronous image request at a time.

## Single-image OCR test

See [`backend/app/modules/ocr/README.md`](backend/app/modules/ocr/README.md) for
Google Cloud authentication, local curl and Postman tests, output inspection,
and a one-shot ESP32-CAM multipart upload example.

This milestone has no queue, buffering, background workers, streaming, video,
polling, WebSockets, threading, or multiprocessing. Run exactly one Uvicorn
worker and do not use `--reload`; an overlapping request is rejected with HTTP
`409` until the current transaction finishes. Other domain endpoints remain
placeholders and are not part of the OCR transaction.

Run the focused tests with:

```bash
pytest -q
```
