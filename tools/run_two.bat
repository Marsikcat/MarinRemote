@echo off
:: Two test copies on this PC: instance 1 and 2, each with its own ports (8995 / 9095)
:: and a throwaway data folder in %TEMP% — the real profile is not touched.
cd /d "%~dp0\.."
set MARINREMOTE_INSTANCE=1
start "MarinRemote 1" venv\Scripts\pythonw.exe app.py
set MARINREMOTE_INSTANCE=2
start "MarinRemote 2" venv\Scripts\pythonw.exe app.py
