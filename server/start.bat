@echo off
cd /d %~dp0
start "1 - Subscriber" cmd /k python subscriber.py
timeout /t 2 >nul
start "2 - Web" cmd /k python app.py
start "3 - UAV Sim" cmd /k echo Ready: python simulator.py --mode F --duration 300
start "4 - Ground Sim" cmd /k echo Ready: python ground_sim.py