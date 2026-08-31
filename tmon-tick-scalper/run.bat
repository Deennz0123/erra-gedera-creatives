@echo off
rem Zapusk vsego cikla dvoynym klikom: obnovlenie koda, sbor dannyh, analiz.
rem Fayl nado polozhit' v papku tmon-tick-scalper i zapustit'.
rem Rezul'tat - report.txt ryadom s etim faylom.

chcp 65001 >nul
cd /d "%~dp0"

if not exist "research" (
    echo.
    echo ERROR: zapuskat' nado iz papki tmon-tick-scalper.
    echo Polozhite run.bat ryadom s papkami research i tmon_bot.
    echo.
    pause
    exit /b 1
)

set BASE=https://raw.githubusercontent.com/Deennz0123/erra-gedera-creatives/claude/tmon-tick-trading-automation-ur95a1/tmon-tick-scalper

echo Zagruzka scenariya...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Invoke-WebRequest '%BASE%/run.ps1' -OutFile run.ps1 -UseBasicParsing"

if not exist "run.ps1" (
    echo.
    echo ERROR: ne udalos' skachat' run.ps1. Proverte internet.
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" -Minutes 15

echo.
echo ================================================
echo  Gotovo. Fayl report.txt lezhit v etoy zhe papke.
echo ================================================
echo.
pause
