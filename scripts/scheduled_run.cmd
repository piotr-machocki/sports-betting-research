@echo off
rem One unattended run_once.py session, for Windows Task Scheduler.
rem Output is appended to data\logs\scheduled_run.log.
cd /d "%~dp0.."
if not exist data\logs mkdir data\logs
echo ===== %date% %time% ===== >> data\logs\scheduled_run.log
.venv\Scripts\python.exe scripts\run_once.py --no-monitor >> data\logs\scheduled_run.log 2>&1
echo exit code %errorlevel% >> data\logs\scheduled_run.log
