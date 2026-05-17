@echo off
cd /d "%~dp0"
echo Starting L-DCM Crash Risk Analysis Dashboard...
echo Open http://localhost:8000 in your browser
echo Press Ctrl+C to stop
echo.
.venv\Scripts\uvicorn app:app --reload --port 8000
