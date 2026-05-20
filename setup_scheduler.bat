@echo off
:: RRE Trading Bot — Task Scheduler Setup
:: Right-click this file and choose "Run as administrator"

echo Setting up RRE Trading Bot scheduled tasks...

set BAT_DIR=C:\Users\Admin\Desktop\RRE TRADING BOT

:: Pre-Market Report — 9:30 AM Mon-Fri
schtasks /create /tn "RRE Pre-Market 9:30AM" ^
  /tr "cmd.exe /c \"%BAT_DIR%\run_pre_market.bat\"" ^
  /sc WEEKLY /d MON,TUE,WED,THU,FRI /st 09:30 ^
  /f /rl HIGHEST /ru "%USERNAME%"
if %errorlevel%==0 (
    echo [OK] Pre-Market task created — fires at 9:30 AM weekdays
) else (
    echo [FAIL] Pre-Market task — check you ran as Administrator
)

:: PM Report — 2:45 PM Mon-Fri
schtasks /create /tn "RRE PM Report 2:45PM" ^
  /tr "cmd.exe /c \"%BAT_DIR%\run_pm_report.bat\"" ^
  /sc WEEKLY /d MON,TUE,WED,THU,FRI /st 14:45 ^
  /f /rl HIGHEST /ru "%USERNAME%"
if %errorlevel%==0 (
    echo [OK] PM Report task created — fires at 2:45 PM weekdays
) else (
    echo [FAIL] PM Report task — check you ran as Administrator
)

:: EOD Report — 4:00 PM Mon-Fri
schtasks /create /tn "RRE EOD Report 4:00PM" ^
  /tr "cmd.exe /c \"%BAT_DIR%\run_eod_report.bat\"" ^
  /sc WEEKLY /d MON,TUE,WED,THU,FRI /st 16:00 ^
  /f /rl HIGHEST /ru "%USERNAME%"
if %errorlevel%==0 (
    echo [OK] EOD Report task created — fires at 4:00 PM weekdays
) else (
    echo [FAIL] EOD Report task — check you ran as Administrator
)

echo.
echo Done. To verify: open Task Scheduler and look for "RRE" tasks.
echo To test now: schtasks /run /tn "RRE Pre-Market 9:30AM"
echo              schtasks /run /tn "RRE PM Report 2:45PM"
pause
