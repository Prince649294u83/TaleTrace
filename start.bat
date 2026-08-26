@echo off
echo Starting TaleTrace...

IF NOT EXIST ".env" (
    echo Copying .env.example to .env...
    copy .env.example .env
)

IF NOT EXIST ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing dependencies...
python -m pip install --upgrade pip
pip install -r requirements.txt

echo Starting TaleTrace servers...
python scripts\dev.py
