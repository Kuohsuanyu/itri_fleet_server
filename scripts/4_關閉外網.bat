@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title Take offline
python tools\console.py --action funnel-off

pause
