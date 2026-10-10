@echo off
cd /d "%~dp0"

if not exist venv\Scripts\python.exe (
    echo [JASIM] Creating virtual environment...
    python -m venv venv
)

echo [JASIM] Installing dependencies...
venv\Scripts\pip install -r requirements.txt --quiet

echo [JASIM] Starting monitor...
venv\Scripts\python run.py
pause
