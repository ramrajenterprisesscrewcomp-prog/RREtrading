@echo off
:: RRE Trading Bot — Server entry point (called by NSSM Windows Service)
:: Do NOT run this directly for local dev; use: .venv\Scripts\uvicorn main:app --reload
cd /d C:\RRE-BOT
call .venv\Scripts\activate.bat
python -m uvicorn main:app --host 0.0.0.0 --port 8000
