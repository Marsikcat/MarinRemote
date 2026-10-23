"""Tests never touch a real profile: a throwaway data folder before anything is imported."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["MARINREMOTE_DATA"] = tempfile.mkdtemp(prefix="marinremote-tests-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# one application object for the whole run (offscreen: its own clipboard, nothing on the screen),
# made before any test can make a lesser one (QCoreApplication / QGuiApplication)
from PySide6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])
