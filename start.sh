#!/usr/bin/env bash

echo "Starting TaleTrace..."

if [ ! -f ".env" ]; then
    echo "Copying .env.example to .env..."
    cp .env.example .env
fi

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing dependencies..."
python3 -m pip install --upgrade pip
pip install -r requirements.txt

echo "Starting TaleTrace servers..."
python scripts/dev.py
