#!/usr/bin/env python3
"""assets/icon.ico (for the exe and the installer) from the icon the app draws itself."""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QGuiApplication  # noqa: E402

from marinremote.ui.icons import pixmap  # noqa: E402


def main():
    app = QGuiApplication(sys.argv)  # noqa: F841 — pixmaps need it
    out = ROOT / "assets" / "icon.ico"
    out.parent.mkdir(exist_ok=True)
    if not pixmap(256).save(str(out), "ICO"):
        sys.exit("could not write the icon")
    print(out)


if __name__ == "__main__":
    main()
