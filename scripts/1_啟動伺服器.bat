@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title ITRI Fleet Server
echo Starting MQTT broker + web server.  Ctrl-C to stop.
echo Dashboard: http://localhost:8080
echo.
python -m server.main

pause
