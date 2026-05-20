@echo off
cd /d "C:\Users\Admin\Desktop\RRE TRADING BOT"
call .venv\Scripts\activate.bat
python -c "import asyncio; from services.pm_report_service import pm_scan_and_send; asyncio.run(pm_scan_and_send())"
