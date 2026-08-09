@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title Stop PostgreSQL
python tools\console.py --action pg-stop

pause
