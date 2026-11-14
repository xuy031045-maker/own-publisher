@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Project environment is missing. Please install dependencies first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -X utf8 start_workbench.py
pause
