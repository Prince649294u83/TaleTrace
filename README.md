# TaleTrace

TaleTrace is a hackathon project. This repository currently contains architecture only; feature and application logic will be added later.

## Setup

```bash
python -m venv .venv
pip install -r requirements.txt
copy .env.example .env
uvicorn backend.app.main:app --reload
```

The backend uses independent domain modules under `backend/app/modules`. Shared configuration, logging, API composition, and utilities are kept separate. The routers are placeholders and contain no endpoints or product behavior.