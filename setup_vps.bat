@echo off
:: RRE Trading Bot — One-shot Windows VPS setup
:: Works from ANY install path (C:\RRE-BOT, D:\MyBot, wherever you cloned).
::
:: Pre-requisites (do these before running):
::   1. Install Python 3.11+ from python.org  (check "Add Python to PATH")
::   2. Install Git from git-scm.com
::   3. Download NSSM from nssm.cc/download → extract so C:\nssm\win64\nssm.exe exists
::   4. Create a .env file in this folder with your secrets (see below)
::
:: Usage: Right-click → Run as administrator

:: ── Derive install path from this script's own location ──────────────────────
set "BOTDIR=%~dp0"
if "%BOTDIR:~-1%"=="\" set "BOTDIR=%BOTDIR:~0,-1%"

echo ============================================================
echo  RRE Trading Bot — VPS Setup
echo  Install path: %BOTDIR%
echo ============================================================
echo.

cd /d "%BOTDIR%"

:: ── 1. Create virtual environment ────────────────────────────────────────────
if not exist ".venv\Scripts\python.exe" (
    echo [STEP 1] Creating Python virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [FAIL] python -m venv failed. Is Python 3.11+ installed and on PATH?
        pause & exit /b 1
    )
    echo [OK] Virtual environment created
) else (
    echo [OK] Virtual environment already exists
)

:: ── 2. Install dependencies ───────────────────────────────────────────────────
echo [STEP 2] Installing dependencies (this may take a few minutes)...
.venv\Scripts\pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [FAIL] pip install failed. Check requirements.txt and internet access.
    pause & exit /b 1
)
echo [OK] Dependencies installed

:: ── 3. Create folders if missing ─────────────────────────────────────────────
if not exist "logs" mkdir logs
echo [OK] logs\ folder ready
if not exist "data" mkdir data
echo [OK] data\ folder ready

:: ── 4. Check .env exists ──────────────────────────────────────────────────────
if not exist ".env" (
    echo.
    echo [WARN] .env file not found at %BOTDIR%\.env
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
    echo        Then re-run this script.
    pause & exit /b 1
) else (
    echo [OK] .env file found
)

:: ── 5. Check NSSM ─────────────────────────────────────────────────────────────
if not exist "C:\nssm\win64\nssm.exe" (
    echo.
    echo [WARN] NSSM not found at C:\nssm\win64\nssm.exe
    echo        Download from https://nssm.cc/download
    echo        Extract so that C:\nssm\win64\nssm.exe exists, then re-run.
    echo.
    pause & exit /b 1
)
echo [OK] NSSM found

:: ── 6. Remove existing service if present ────────────────────────────────────
C:\nssm\win64\nssm.exe status RRE-Bot >nul 2>&1
if not errorlevel 1 (
    echo [STEP 6] Removing existing RRE-Bot service...
    C:\nssm\win64\nssm.exe stop RRE-Bot >nul 2>&1
    C:\nssm\win64\nssm.exe remove RRE-Bot confirm
)

:: ── 7. Install Windows Service via NSSM ──────────────────────────────────────
echo [STEP 7] Installing RRE-Bot Windows Service...
C:\nssm\win64\nssm.exe install RRE-Bot "%BOTDIR%\.venv\Scripts\python.exe"
C:\nssm\win64\nssm.exe set RRE-Bot AppDirectory "%BOTDIR%"
C:\nssm\win64\nssm.exe set RRE-Bot AppParameters "-m uvicorn main:app --host 0.0.0.0 --port 8000"
C:\nssm\win64\nssm.exe set RRE-Bot AppStdout "%BOTDIR%\logs\service_out.log"
C:\nssm\win64\nssm.exe set RRE-Bot AppStderr "%BOTDIR%\logs\service_err.log"
C:\nssm\win64\nssm.exe set RRE-Bot AppRotateFiles 1
C:\nssm\win64\nssm.exe set RRE-Bot AppRotateBytes 10485760
C:\nssm\win64\nssm.exe set RRE-Bot Start SERVICE_AUTO_START
if errorlevel 1 (
    echo [FAIL] NSSM service install failed.
    pause & exit /b 1
)
echo [OK] Service installed

:: ── 8. Open firewall port 8000 ────────────────────────────────────────────────
echo [STEP 8] Opening firewall port 8000...
netsh advfirewall firewall delete rule name="RRE-Bot port 8000" >nul 2>&1
netsh advfirewall firewall add rule name="RRE-Bot port 8000" dir=in action=allow protocol=TCP localport=8000
echo [OK] Firewall rule added

:: ── 9. Start the service ──────────────────────────────────────────────────────
echo [STEP 9] Starting RRE-Bot service...
C:\nssm\win64\nssm.exe start RRE-Bot
if errorlevel 1 (
    echo [WARN] Service start returned an error — check %BOTDIR%\logs\service_err.log
) else (
    echo [OK] Service started
)

echo.
echo ============================================================
echo  Setup complete!
echo.
echo  Install path : %BOTDIR%
echo  Dashboard    : http://localhost:8000
echo  Logs         : %BOTDIR%\logs\service_out.log
echo                 %BOTDIR%\logs\service_err.log
echo.
echo  To check status : C:\nssm\win64\nssm.exe status RRE-Bot
echo  To restart      : C:\nssm\win64\nssm.exe restart RRE-Bot
echo  To stop         : C:\nssm\win64\nssm.exe stop RRE-Bot
echo.
echo  After updates (git pull), run:
echo    C:\nssm\win64\nssm.exe restart RRE-Bot
echo ============================================================
pause
