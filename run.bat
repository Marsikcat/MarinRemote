@echo off
title MarinRemote
cd /d "%~dp0"
if not exist "venv\Scripts\pythonw.exe" (
    echo Creating the environment...
    python -m venv venv || (echo Python 3.12 is needed: https://www.python.org/downloads/ & pause & exit /b 1)
    venv\Scripts\python -m pip install -q -r requirements.txt || (pause & exit /b 1)
)
start "" venv\Scripts\pythonw.exe app.py %*
