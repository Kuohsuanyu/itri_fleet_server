@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title ITRI Fleet Simulator
if not exist sim_creds.json (
  echo No credentials yet - enrolling 12 simulated robots first.
  python tools\provision_sim.py -n 12 --password itri --out sim_creds.json
  echo.
)
python tools\sim_robots.py --credentials sim_creds.json --hz 2

pause
