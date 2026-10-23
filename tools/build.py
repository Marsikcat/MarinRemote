#!/usr/bin/env python3
"""
Build the Windows app and its installer.

    python tools\\build.py      (inside the venv with requirements.txt + pyinstaller)

Result:
    dist\\MarinRemote\\                  MarinRemote.exe, _internal\\
    dist\\MarinRemote-Setup-X.Y.Z.exe    installer (Inno Setup 6), per-user, no admin rights
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
APP_DIR = DIST / "MarinRemote"


def version():
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def write_version_info(ver):
    """Windows 'Details' tab of the exe: product name, version, copyright."""
    nums = ([int(x) for x in ver.split(".")] + [0, 0, 0, 0])[:4]
    tup = ", ".join(map(str, nums))
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({tup}), prodvers=({tup}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041904B0', [
      StringStruct('CompanyName', 'Marsikcat'),
      StringStruct('FileDescription', 'MarinRemote — удалённый доступ к ПК без серверов'),
      StringStruct('FileVersion', '{ver}'),
      StringStruct('InternalName', 'MarinRemote'),
      StringStruct('LegalCopyright', 'github.com/Marsikcat/MarinRemote'),
      StringStruct('OriginalFilename', 'MarinRemote.exe'),
      StringStruct('ProductName', 'MarinRemote'),
      StringStruct('ProductVersion', '{ver}')])]),
    VarFileInfo([VarStruct('Translation', [0x0419, 1200])])
  ]
)
"""
    BUILD.mkdir(exist_ok=True)
    (BUILD / "version_info.txt").write_text(text, encoding="utf-8")


def icon():
    if not (ROOT / "assets" / "icon.ico").exists():
        subprocess.run([sys.executable, str(ROOT / "tools" / "make_icon.py")], check=True, cwd=ROOT)


def pyinstaller():
    subprocess.run([sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "MarinRemote.spec"),
                    "--noconfirm", "--clean", "--log-level", "WARN",
                    "--distpath", str(DIST), "--workpath", str(BUILD / "pyinstaller")],
                   check=True, cwd=ROOT)


def find_iscc():
    for base, sub in ((os.environ.get("LOCALAPPDATA"), "Programs"), (os.environ.get("ProgramFiles(x86)"), ""),
                      (os.environ.get("ProgramFiles"), "")):
        if base:
            p = Path(base) / sub / "Inno Setup 6" / "ISCC.exe"
            if p.exists():
                return p
    return shutil.which("ISCC")


def installer(ver):
    iscc = find_iscc()
    if not iscc:
        sys.exit("Не найден Inno Setup 6. Установите: winget install JRSoftware.InnoSetup")
    subprocess.run([str(iscc), "/Q", f"/DAppVersion={ver}", str(ROOT / "packaging" / "installer.iss")],
                   check=True, cwd=ROOT / "packaging")
    return DIST / f"MarinRemote-Setup-{ver}.exe"


def size_mb(path):
    if path.is_file():
        return path.stat().st_size / 1e6
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6


def main():
    ver = version()
    print(f"MarinRemote {ver}: сборка exe…")
    write_version_info(ver)
    icon()
    pyinstaller()
    print("Установщик…")
    setup = installer(ver)
    print(f"\nГотово:\n  {APP_DIR}  ({size_mb(APP_DIR):.0f} МБ)\n  {setup}  ({size_mb(setup):.0f} МБ)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
