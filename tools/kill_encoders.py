#!/usr/bin/env python3
"""
Ends encoder processes of this project left running (after tests or a crash). Normally they
die with the app on their own (a job object); this is the safety net. Only processes whose
command line has this project's app.py (or MarinRemote.exe) with --encoder are touched.

    python tools\\kill_encoders.py
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*--encoder*' } | "
          "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    killed = 0
    root = str(ROOT).lower()
    for line in out.splitlines():
        pid, _, cmd = line.partition("\t")
        low = cmd.lower()
        if not pid.strip().isdigit() or not (f"{root}\\app.py" in low or "marinremote.exe" in low):
            continue
        subprocess.run(["taskkill", "/F", "/PID", pid.strip()], capture_output=True)
        killed += 1
        print(f"ended {pid.strip()}: {cmd[:120]}")
    print(f"encoders ended: {killed}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
