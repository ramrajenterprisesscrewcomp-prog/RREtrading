@echo off
:: RRE Trading Bot — One-shot Windows VPS setup
:: Run this ONCE on the VPS as Administrator after:
::   1. Installing Python 3.11+ (python.org) — check "Add Python to PATH"
::   2. Installing Git (git-scm.com)
::   3. Downloading NSSM and extracting to C:\nssm\  (nssm.cc/download)
::   4. Creating C:\RRE-BOT\.env with all your secrets
::
:: Usage: Right-click → Run as administrator

echo ============================================================
echo  RRE Trading Bot — VPS Setup
echo ============================================================
echo.

:: ── 1. Verify the repo is at C:\RRE-BOT ─────────────────────────────────────
if not exist "C:\RRE-BOT\main.py" (
    echo [STEP 1] Cloning repo from GitHub...
    cd /d C:\
    git clone https://github.com/ramrajenterprisesscrewcomp-prog/RREtrading.git RRE-BOT
    if errorlevel 1 (
        echo [FAIL] Git clone failed. Make sure Git is installed and you have internet access.
        pause & exit /b 1
    )
    echo [OK] Repo cloned to C:\RRE-BOT
) else (
    echo [OK] Repo already exists at C:\RRE-BOT
)

cd /d C:\RRE-BOT

:: ── 2. Create virtual environment ───────────────────────────────────────────
if not exist ".venv\Scripts\python.exe" (
    echo [STEP 2] Creating Python virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [FAIL] python -m venv failed. Is Python 3.11+ installed and on PATH?
        pause & exit /b 1
    )
    echo [OK] Virtual environment created
) else (
    echo [OK] Virtual environment already exists
)

:: ── 3. Install dependencies ──────────────────────────────────────────────────
echo [STEP 3] Installing dependencies (this may take a few minutes)...
.venv\Scripts\pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [FAIL] pip install failed. Check requirements.txt and internet access.
    pause & exit /b 1
)
echo [OK] Dependencies installed

:: ── 4. Create logs folder if missing ────────────────────────────────────────
if not exist "logs" mkdir logs
echo [OK] logs\ folder ready

:: ── 5. Create data folder if missing ────────────────────────────────────────
if not exist "data" mkdir data
echo [OK] data\ folder ready

:: ── 6. Check .env exists ─────────────────────────────────────────────────────
if not exist ".env" (
    echo.
    echo [WARN] .env file not found at C:\RRE-BOT\.env
    echo        Create it with Notepad before starting the service:
    echo.
    echo        ANGEL_API_KEY=...
    echo        ANGEL_CLIENT_ID=...
    echo        ANGEL_PASSWORD=...
    echo        ANGEL_TOTP_SECRET=...
    echo        OPENAI_API_KEY=...
    echo        TSR_EMAIL=...
    echo        TSR_PASSWORD=...
    echo        TELEGRAM_BOT_TOKEN=...
    echo        TELEGRAM_CHAT_ID=...
    echo        SUPABASE_URL=...
    echo        SUPABASE_KEY=...
    echo.
) else (
    echo [OK] .env file found
)

:: ── 7. Check NSSM ────────────────────────────────────────────────────────────
if not exist "C:\nssm\win64\nssm.exe" (
    echo.
    echo [WARN] NSSM not found at C:\nssm\win64\nssm.exe
    echo        Download from https://nssm.cc/download
    echo        Extract so that C:\nssm\win64\nssm.exe exists, then re-run this script.
    echo.
    pause & exit /b 1
)
echo [OK] NSSM found

:: ── 8. Remove existing service if present ───────────────────────────────────
C:\nssm\win64\nssm.exe status RRE-Bot >nul 2>&1
if not errorlevel 1 (
    echo [STEP 8] Removing existing RRE-Bot service...
    C:\nssm\win64\nssm.exe stop RRE-Bot >nul 2>&1
    C:\nssm\win64\nssm.exe remove RRE-Bot confirm
)

:: ── 9. Install Windows Service via NSSM ─────────────────────────────────────
echo [STEP 9] Installing RRE-Bot Windows Service...
C:\nssm\win64\nssm.exe install RRE-Bot "C:\RRE-BOT\.venv\Scripts\python.exe"
C:\nssm\win64\nssm.exe set RRE-Bot AppDirectory "C:\RRE-BOT"
C:\nssm\win64\nssm.exe set RRE-Bot AppParameters "-m uvicorn main:app --host 0.0.0.0 --port 8000"
C:\nssm\win64\nssm.exe set RRE-Bot AppStdout "C:\RRE-BOT\logs\service_out.log"
C:\nssm\win64\nssm.exe set RRE-Bot AppStderr "C:\RRE-BOT\logs\service_err.log"
C:\nssm\win64\nssm.exe set RRE-Bot AppRotateFiles 1
C:\nssm\win64\nssm.exe set RRE-Bot AppRotateBytes 10485760
C:\nssm\win64\nssm.exe set RRE-Bot Start SERVICE_AUTO_START
if errorlevel 1 (
    echo [FAIL] NSSM service install failed.
    pause & exit /b 1
)
echo [OK] Service installed

:: ── 10. Open firewall port 8000 ──────────────────────────────────────────────
echo [STEP 10] Opening firewall port 8000...
netsh advfirewall firewall delete rule name="RRE-Bot port 8000" >nul 2>&1
netsh advfirewall firewall add rule name="RRE-Bot port 8000" dir=in action=allow protocol=TCP localport=8000
echo [OK] Firewall rule added

:: ── 11. Start the service ────────────────────────────────────────────────────
echo [STEP 11] Starting RRE-Bot service...
C:\nssm\win64\nssm.exe start RRE-Bot
if errorlevel 1 (
    echo [WARN] Service start returned an error — check logs\service_err.log
) else (
    echo [OK] Service started
)

echo.
echo ============================================================
echo  Setup complete!
echo.
echo  Dashboard : http://localhost:8000
echo  Logs      : C:\RRE-BOT\logs\service_out.log
echo              C:\RRE-BOT\logs\service_err.log
echo.
echo  To check status : C:\nssm\win64\nssm.exe status RRE-Bot
echo  To restart      : C:\nssm\win64\nssm.exe restart RRE-Bot
echo  To stop         : C:\nssm\win64\nssm.exe stop RRE-Bot
echo.
echo  After updates (git pull), run:
echo    C:\nssm\win64\nssm.exe restart RRE-Bot
echo ============================================================
pause
