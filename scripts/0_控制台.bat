@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title ITRI Fleet Console
python tools\console.py
if errorlevel 1 (
  echo.
  echo Console exited with an error. Is Python on PATH?
  pause
)
