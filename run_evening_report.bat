@echo off
cd /d "C:\Users\Admin\Desktop\RRE TRADING BOT"
call .venv\Scripts\activate.bat
python -c "import asyncio; from services.evening_analysis_service import evening_scan_and_send; asyncio.run(evening_scan_and_send())"
