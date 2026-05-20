@echo off
cd /d "C:\Users\Admin\Desktop\RRE TRADING BOT"
call .venv\Scripts\activate.bat
python -c "import asyncio; from services.weekly_report_service import weekly_scan_and_send; asyncio.run(weekly_scan_and_send())"
