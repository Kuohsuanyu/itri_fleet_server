@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title Funnel bandwidth probe
echo NOTE: /api/bwtest is disabled by default.
echo Set bwtest.enabled=true in config.yaml for the full test,
echo then set it back to false afterwards.
echo.
set "URL=https://YOUR-NODE.YOUR-TAILNET.ts.net"
set /p URL="Public URL [%URL%]: "
set "PW=itri"
set /p PW="Password [itri]: "
python tools\bw_probe.py %URL% --token %PW% --all --json bw_results.json

pause
