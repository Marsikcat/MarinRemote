"""Paths, ports, instances and persistent settings."""

import copy
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path

APP_NAME = "MarinRemote"
# Installed build (PyInstaller): ROOT is the folder with MarinRemote.exe, bundled files are
# in sys._MEIPASS. From source: ROOT is the project folder.
FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))
VERSION = (BUNDLE / "VERSION").read_text(encoding="utf-8").strip()

# Updates: the GitHub releases of this repository, installers signed with this key (Ed25519; the
# secret half stays on the PC that publishes releases — tools/sign.py)
REPO = "Marsikcat/MarinRemote"
RELEASE_KEY = "901aea90e5de0c1f2d51276a43673babc3776e4c6c6d27724810da5de034c35e"

# A test copy on the same PC (MARINREMOTE_INSTANCE=1, 2…) gets its own ports and a throwaway
# data folder in %TEMP%: the real profile (instance 0) is never touched by it.
INSTANCE = int(os.environ.get("MARINREMOTE_INSTANCE", "0") or 0)
_OFF = INSTANCE * 100


def _data_dir():
    override = os.environ.get("MARINREMOTE_DATA")
    if override:
        return Path(override)
    if INSTANCE:
        return Path(tempfile.gettempdir()) / f"{APP_NAME}-dev-{INSTANCE}"
    return Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME


DATA = _data_dir()

# Ports. MarinCall uses 8890 and 8891 (+100 per instance) and may run at the same time:
# these never collide with it.
DISCOVERY_PORT = 8894              # UDP  LAN announcements (shared by all instances)
PEER_PORT = 8895 + _OFF            # UDP  iroh QUIC: everything of a session

RIGHTS_ALL = ["view", "control", "files", "clipboard"]


def computer_name():
    name = os.environ.get("COMPUTERNAME") or "ПК"
    return f"{name} #{INSTANCE}" if INSTANCE else name


def home_root():
    return {"id": "home", "path": str(Path.home()), "label": "Папка пользователя"}


DEFAULTS = {
    # the ed25519 key, the identity of this device — encrypted for this Windows user on this PC
    # (DPAPI, base64); the key itself never reaches the disk
    "secret_key_dpapi": "",
    "name": "",                 # how this PC is shown to others ("" = the computer name)
    "network_mode": "internet", # internet (relays + NAT traversal) | lan (direct only)
    # ── this PC, as a host ──
    "allow_incoming": True,     # off: every connection is turned away
    "lan_visible": True,        # announce this PC on the local network
    "unattended": True,         # remembered devices / password logins skip the confirmation
    "password_hash": "",        # argon2id string; "" = no password set
    # key -> {name, rights, mode: "contact" | "password" | "ask", added}
    #   contact  — connects right away, no confirmation and no password
    #   password — connects with the password, nobody needs to be at the PC
    #   ask      — remembered (your name for it), but every connection is confirmed
    "trusted": {},
    "blocked": {},              # key -> {name, when}
    "roots": None,              # [{id, path, label}] — folders a remote may reach; None = home
    "files_all_drives": False,  # …and every drive of this PC (D:, a USB stick…) as it comes and goes
    "check_updates": True,      # ask GitHub for a new version at start and twice a day
    "kill_hotkey": {"vk": 0x7B, "mods": ["ctrl", "shift", "alt"]},    # Ctrl+Shift+Alt+F12
    # ── connecting to other PCs ──
    "contacts": {},             # key -> {name, relay, addrs, added, last}
    "want_rights": list(RIGHTS_ALL),
    # ── window ──
    "close_to_tray": True,
    "start_minimized": True,    # started by Windows at logon: straight to the tray, no window
    "window": None,
}


def _read_json(path):
    """The dict in `path`; None if there is no such file; ValueError if it is damaged. Waits a
    little while another program (an antivirus, a backup tool) holds the file."""
    for _ in range(10):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except PermissionError:
            time.sleep(0.3)
            continue
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    raise PermissionError(f"файл занят другой программой: {path}")


def write_json(path, data):
    """Atomic: a crash in the middle never leaves a half-written file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    for attempt in range(5):
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
            return True
        except PermissionError:          # held open by someone for a moment
            time.sleep(0.1 * (attempt + 1))
    print(f"settings: could not write {path}", flush=True)
    return False


class Settings(dict):
    """A dict that knows how to persist itself. Nested dicts (trusted, contacts…) are replaced,
    never changed in place, so another thread can iterate a snapshot safely."""

    def __init__(self, folder=None):
        super().__init__(copy.deepcopy(DEFAULTS))
        self.folder = Path(folder or DATA)
        self.file = self.folder / "settings.json"
        self.backup = self.folder / "settings.backup.json"
        self.lock = threading.RLock()
        self._backed_up = float("-inf")
        self._secret = None
        self.identity_note = None    # "new" | "migrated" | "unreadable" — what happened to the key
        self.update(self._load())
        self._ensure_identity()
        if not self["name"]:
            self["name"] = computer_name()
        if not self["roots"]:
            self["roots"] = [home_root()]
        self.save()

    def _load(self):
        """settings.json — or, if it is damaged, the backup copy (the damaged file is kept aside).
        A file we cannot read at all stops the start: carrying on would make a new identity and
        write it over the real one."""
        for path in (self.file, self.backup):
            try:
                data = _read_json(path)
            except ValueError:
                try:
                    path.replace(path.with_name(f"{path.stem}.damaged-{int(time.time())}.json"))
                except OSError:
                    pass
                continue
            except PermissionError:
                if path == self.file:
                    raise
                continue
            if data:
                return data
        return {}

    def save(self):
        with self.lock:
            data = dict(self)
            targets = [self.file]
            if time.monotonic() - self._backed_up > 60:        # the backup a minute behind at most
                targets.append(self.backup)
                self._backed_up = time.monotonic()
            for target in targets:
                write_json(target, data)

    def set(self, key, value):
        with self.lock:
            self[key] = value
            self.save()

    def put(self, key, item, value):
        """settings[key][item] = value (or removed when value is None), copy-on-write, saved."""
        with self.lock:
            d = dict(self[key] or {})
            if value is None:
                d.pop(item, None)
            else:
                d[item] = value
            self[key] = d
            self.save()

    def _ensure_identity(self):
        """Our ed25519 key: the device ID is its public half (hex). It is kept only encrypted with
        DPAPI. A key from before that (plain hex) is taken over and encrypted. A key that can't be
        decrypted — the file was copied from another PC or Windows user — is not this device's to
        use: it is put aside and a new identity made."""
        import base64

        import iroh

        from . import dpapi
        plain = self.pop("secret_key", "") or ""
        blob = self.get("secret_key_dpapi") or ""
        secret = None
        if blob:
            try:
                secret = dpapi.unprotect(base64.b64decode(blob))
                if len(secret) != 32:
                    raise ValueError("not a key")
            except (OSError, ValueError):
                secret = None
                self["secret_key_unreadable"] = blob
                self.identity_note = "unreadable"
        if secret is None and len(plain) == 64 and not blob:
            secret = bytes.fromhex(plain)
            self.identity_note = "migrated"
        if secret is None:
            secret = secrets.token_bytes(32)
            self.identity_note = self.identity_note or "new"
        if self.identity_note:
            self["secret_key_dpapi"] = base64.b64encode(dpapi.protect(secret)).decode("ascii")
        self._secret = secret.hex()
        self["uid"] = iroh.SecretKey.from_bytes(secret).public().to_bytes().hex()

    @property
    def secret_key(self):
        """The private key, hex — in memory only."""
        return self._secret

    @property
    def uid(self):
        return self["uid"]
