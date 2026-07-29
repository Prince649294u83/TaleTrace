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

The infrastructure check is available at `GET /api/health`. It returns the common response envelope and is not a product feature.