#!/usr/bin/env python3
"""
Publish a signed GitHub release of MarinRemote — from this PC, the one that holds the release
key (it never goes to GitHub).

    python tools\\build.py                 build dist\\MarinRemote-Setup-X.Y.Z.exe first
    python tools\\release.py               release VERSION from HEAD (committed and pushed)
    python tools\\release.py --draft       a draft to look at on GitHub first
    python tools\\release.py --dry-run     check and print the notes, publish nothing

Release notes come from the "## X.Y.Z" section of CHANGELOG.md. Assets:

    MarinRemote-Setup-X.Y.Z.exe       the installer people download and the app updates itself with
    MarinRemote-Setup-X.Y.Z.exe.sig   its signature — the app installs only signed updates

Needs git and the GitHub CLI logged in (gh auth login).
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sign  # noqa: E402
from marinremote import signing  # noqa: E402
from marinremote.config import RELEASE_KEY, REPO  # noqa: E402


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


def notes_for(version):
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## \[?{re.escape(version)}\]?[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else None


def main():
    ap = argparse.ArgumentParser(description="Выпустить подписанный релиз MarinRemote на GitHub")
    ap.add_argument("--draft", action="store_true", help="создать черновик")
    ap.add_argument("--dry-run", action="store_true", help="только проверить и показать описание")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    sha = git("rev-parse", "HEAD")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    tag = f"v{version}"
    notes = notes_for(version)
    if not notes:
        sys.exit(f"В CHANGELOG.md нет раздела «## {version}» — допишите, что изменилось.")
    if git("status", "--porcelain", "--untracked-files=no"):
        sys.exit("Есть незакоммиченные изменения — сначала сделайте коммит.")
    if not git("branch", "-r", "--contains", sha):
        sys.exit("Коммит ещё не на GitHub — сначала git push.")
    installer = ROOT / "dist" / f"MarinRemote-Setup-{version}.exe"
    if not installer.exists():
        sys.exit(f"Нет {installer.name} — соберите установщик: python tools\\build.py")
    key = sign.load_key()
    if not key:
        sys.exit(f"Нет ключа подписи ({sign.KEY_FILE}) — без подписи приложение не установит обновление.")
    if signing.public_key(key) != RELEASE_KEY:
        sys.exit("Ключ подписи не тот, что встроен в программу (config.RELEASE_KEY) — обновление не встанет.")
    print(f"{tag} ({sha[:7]}): {installer.name}, {installer.stat().st_size / 1e6:.1f} МБ\n\n{notes}\n")
    if args.dry_run:
        return

    [sig] = sign.sign_files([installer], key)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as f:
        f.write(notes)
    cmd = ["gh", "release", "create", tag, str(installer), str(sig), "--repo", REPO, "--target", sha,
           "--title", f"MarinRemote {version}", "--notes-file", f.name]
    if args.draft:
        cmd.append("--draft")
    subprocess.run(cmd, check=True)
    print(f"\n{tag} выпущен.")


if __name__ == "__main__":
    main()
