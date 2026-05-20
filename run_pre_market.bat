@echo off
cd /d "C:\Users\Admin\Desktop\RRE TRADING BOT"
call .venv\Scripts\activate.bat
python -c "import asyncio; from services.pre_market_service import pre_market_scan_and_send; asyncio.run(pre_market_scan_and_send())"
