@echo off
rem Zapusk vsego cikla dvoynym klikom. Fayl sam nahodit papku proekta,
rem poetomu ego mozhno ostavit' v Downloads i nikuda ne perekladyvat'.
rem Rezul'tat - report.txt v papke proekta.

chcp 65001 >nul
setlocal

set PROJ=
if exist "%~dp0research\tinvest_collect.py" set PROJ=%~dp0
if not defined PROJ if exist "%~dp0tmon-tick-scalper\research\tinvest_collect.py" set PROJ=%~dp0tmon-tick-scalper\
if not defined PROJ if exist "%USERPROFILE%\Downloads\tmon-tick-scalper\research\tinvest_collect.py" set PROJ=%USERPROFILE%\Downloads\tmon-tick-scalper\
if not defined PROJ if exist "%USERPROFILE%\Desktop\tmon-tick-scalper\research\tinvest_collect.py" set PROJ=%USERPROFILE%\Desktop\tmon-tick-scalper\

if not defined PROJ (
    echo.
    echo ERROR: papka tmon-tick-scalper ne naydena.
    echo Iskal ryadom s etim faylom, v Downloads i na Rabochem stole.
    echo Polozhite run.bat v papku proekta i zapustite snova.
    echo.
    pause
    exit /b 1
)

cd /d "%PROJ%"
echo Papka proekta: %PROJ%
echo.

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

powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJ%run.ps1" -Minutes 15

echo.
echo ==========================================================
echo  Gotovo. Fayl report.txt lezhit v papke:
echo  %PROJ%
echo ==========================================================
echo.
pause
