@echo off
:: RRE Trading Bot — Server entry point (called by NSSM Windows Service)
:: Works from any install path — do NOT hardcode the directory here.
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m uvicorn main:app --host 0.0.0.0 --port 8000
