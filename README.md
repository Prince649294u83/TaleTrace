# TaleTrace

TaleTrace is a hackathon project. The current implementation includes only the
first OCR testing milestone: one synchronous image request at a time.

## Setup

```bash
python -m venv .venv
pip install -r requirements.txt
copy .env.example .env
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Set `GOOGLE_CLOUD_VISION_API_KEY` in `.env` after enabling the Google Cloud
Vision API for that key. Credentials are intentionally not stored in Git.

## Single-image OCR test

Send one JPEG as the raw request body:

```bash
curl.exe -X POST http://127.0.0.1:8000/upload_frame ^
  -H "Content-Type: image/jpeg" ^
  --data-binary "@sample.jpg"
```

The request saves `latest.jpg`, writes the preprocessed inspection image to
`output/processed.jpg`, calls Google Cloud Vision with
`DOCUMENT_TEXT_DETECTION`, overwrites `output/output.txt`, and then returns the
recognized full-page text in JSON. If any step fails, processing stops and a
descriptive HTTP error is returned. There are no retries.

This milestone has no queue, buffering, background workers, streaming, video,
polling, WebSockets, threading, or multiprocessing. Run exactly one Uvicorn
worker and do not use `--reload`; an overlapping request is rejected with HTTP
`409` until the current transaction finishes. Other domain endpoints remain
placeholders and are not part of the OCR transaction.

Run the focused tests with:

```bash
pytest -q
```