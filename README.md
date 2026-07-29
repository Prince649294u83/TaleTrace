# TaleTrace

TaleTrace is a hackathon project. This repository currently contains architecture only; feature and application logic will be added later.

## Setup

```bash
python -m venv .venv
pip install -r requirements.txt
copy .env.example .env
uvicorn backend.app.main:app --reload
```

The backend uses independent domain modules under `backend/app/modules`. Shared configuration, environment loading, logging, dependency providers, response models, API composition, and utilities are kept separate. The module routers are placeholders and contain no product behavior.

Placeholder contracts are exposed at `/upload_frame`, `/ocr/process`, `/gesture/select`, `/ai/explain`, `/session/start`, `/session/end`, `/session/current`, and `/health`. They do not perform processing.