"""Tests never touch a real profile: a throwaway data folder before anything is imported."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["MARINREMOTE_DATA"] = tempfile.mkdtemp(prefix="marinremote-tests-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
