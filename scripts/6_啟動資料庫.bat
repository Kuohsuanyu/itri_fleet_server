@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title Start PostgreSQL
python tools\console.py --action pg-start

pause
