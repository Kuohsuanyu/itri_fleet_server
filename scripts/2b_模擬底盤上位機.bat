@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title ITRI Chassis Simulator
echo Simulating a chassis vendor's onboard computer.
echo Publishes 19 vendor-style topics to the LOCAL broker 127.0.0.1:1883
echo.
python tools\sim_chassis.py --host 127.0.0.1 --port 1883

pause
