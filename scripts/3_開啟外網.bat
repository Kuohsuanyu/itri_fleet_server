@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title Publish to the internet
python tools\console.py --action funnel-on

pause
