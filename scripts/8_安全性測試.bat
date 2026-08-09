@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title Security tests
echo Running 36 end-to-end checks.
echo Needs the server and database running, and sim_creds.json present.
echo.
python tools\test_enroll.py
echo.
python tools\test_broker_acl.py --credentials sim_creds.json

pause
