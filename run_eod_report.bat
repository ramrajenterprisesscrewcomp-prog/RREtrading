@echo off
cd /d "C:\Users\Admin\Desktop\RRE TRADING BOT"
call .venv\Scripts\activate.bat
python -c "import asyncio; from services.eod_report_service import eod_scan_and_send; asyncio.run(eod_scan_and_send())"
