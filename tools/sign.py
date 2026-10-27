#!/usr/bin/env python3
"""
The release signing key.

    python tools\\sign.py keygen          create the key (once), print the public half
    python tools\\sign.py public          print the public key of the key you have
    python tools\\sign.py sign FILE...    write FILE.sig next to each file

The secret key lives outside the project, in %APPDATA%\\MarinRemote-release\\release.key (or
wherever MARINREMOTE_SIGNING_KEY_FILE points; MARINREMOTE_SIGNING_KEY may hold it as hex). Keep
a backup: without it no new version can update itself — people would have to download the
installer by hand.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from marinremote import signing  # noqa: E402

KEY_FILE = Path(os.environ.get("MARINREMOTE_SIGNING_KEY_FILE") or
                Path(os.environ.get("APPDATA") or Path.home()) / "MarinRemote-release" / "release.key")


def load_key():
    """The secret key as hex, or None."""
    env = (os.environ.get("MARINREMOTE_SIGNING_KEY") or "").strip()
    if env:
        return env
    if KEY_FILE.exists():
        return KEY_FILE.read_text(encoding="utf-8").strip()
    return None


def sign_files(paths, key=None):
    key = key or load_key()
    if not key:
        raise SystemExit(f"Нет ключа подписи ({KEY_FILE}) — создайте: python tools\\sign.py keygen")
    out = []
    for p in map(Path, paths):
        sig = p.with_name(p.name + ".sig")
        sig.write_text(signing.sign(key, p.name, signing.digest(p)), encoding="utf-8")
        out.append(sig)
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "keygen":
        if load_key():
            raise SystemExit(f"Ключ уже есть: {KEY_FILE}. Второй не нужен — выпущенные версии знают только этот.")
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        secret = signing.new_secret()
        KEY_FILE.write_text(secret + "\n", encoding="utf-8")
        print(f"Ключ создан: {KEY_FILE}\nСделайте резервную копию этого файла.\n"
              f"Открытый ключ (он в marinremote/config.py, RELEASE_KEY):\n{signing.public_key(secret)}")
    elif cmd == "public":
        key = load_key()
        print(signing.public_key(key) if key else f"Ключа нет: {KEY_FILE}")
    elif cmd == "sign" and len(sys.argv) > 2:
        for sig in sign_files(sys.argv[2:]):
            print("подписан:", sig)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
