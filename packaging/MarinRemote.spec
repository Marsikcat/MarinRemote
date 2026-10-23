# PyInstaller build of MarinRemote: one folder with MarinRemote.exe — both roles (this PC and
# connecting out) and the screen encoder (MarinRemote.exe --encoder) in the same program.
# No ffmpeg.exe: the encoder uses FFmpeg's libraries through PyAV. Run through tools/build.py,
# which also writes the version resource this spec expects and wraps the result into an installer.
# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent
ICON = str(ROOT / "assets" / "icon.ico")
VERSION_INFO = str(ROOT / "build" / "version_info.txt")

datas = [(str(ROOT / "VERSION"), ".")]
binaries = []
hiddenimports = []
# iroh loads its native library itself (ctypes); PyAV and PyNaCl (libsodium) ship theirs next to
# the package. collect_all picks all of that up.
for pkg in ("iroh", "av", "nacl"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
# PyNaCl's libsodium binding is a cffi module: it needs _cffi_backend, which nothing imports visibly
hiddenimports += ["_cffi_backend"]

# Qt modules we never import, to keep the download small
EXCLUDE_QT = ["PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtPdf",
              "PySide6.QtPdfWidgets", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
              "PySide6.QtDBus", "PySide6.QtTest", "PySide6.QtSql", "PySide6.QtXml",
              "PySide6.QtConcurrent", "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools"]

app = Analysis([str(ROOT / "app.py")], pathex=[str(ROOT)], binaries=binaries, datas=datas,
               hiddenimports=hiddenimports, excludes=["tkinter", "pytest"] + EXCLUDE_QT, noarchive=False)

# never used, but pulled in by the packages' hooks: software OpenGL; Qt's TLS backends with their
# own copy of OpenSSL (Qt makes no TLS connections); Qt's translations (the UI is Russian text);
# platform and image plugins we don't need
DROP = ("opengl32sw.dll", "libcrypto-3-x64", "libssl-3-x64", "qopensslbackend", "qcertonlybackend",
        "qschannelbackend", "qnetworklistmanager", "qdirect2d", "qminimal", "qicns", "qtga", "qtiff",
        "qwbmp", "qwebp", "qpdf")
app.binaries = [b for b in app.binaries if not any(d in b[0].lower() for d in DROP)]
app.datas = [d for d in app.datas if "translations" not in d[0].lower().replace("\\", "/").split("/")]

exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="MarinRemote",
          console=False, icon=ICON, version=VERSION_INFO, upx=False)

COLLECT(exe, app.binaries, app.datas, name="MarinRemote", upx=False)
