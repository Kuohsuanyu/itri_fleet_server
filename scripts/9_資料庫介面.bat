@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title pgAdmin 4
echo Launching pgAdmin 4 - PostgreSQL's own admin GUI.
echo First start takes ~30s and opens your browser.
echo.
echo Connection details:
echo   Host      127.0.0.1
echo   Port      5432
echo   Database  itri_fleet
echo   Username  itri
echo   Password  itri_fleet_dev
echo.
python tools\console.py --action pgadmin
pause
