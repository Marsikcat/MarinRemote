"""
Updates from the GitHub releases of MarinRemote.

An installed copy downloads MarinRemote-Setup-X.Y.Z.exe with its `.sig`, checks the signature
against the release key built into it (signing.py) and runs the installer silently: the
installer closes MarinRemote, replaces the files and starts the new version. An unsigned or
altered installer is never run. A copy started from the sources, or a test copy, only learns
that a new version is out (and can open its page).

Only GitHub is asked, and only for the latest release: no data about this PC goes anywhere.
"""

import json
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import signing
from .config import FROZEN, INSTANCE, RELEASE_KEY, REPO, VERSION

UA = {"User-Agent": "MarinRemote-updater"}
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
CHUNK = 1 << 18


def parse_version(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def asset_name(version):
    return f"MarinRemote-Setup-{version}.exe"


def can_install():
    """Only an installed build replaces itself — never `python app.py` or a test copy."""
    return FROZEN and not INSTANCE


def _get(url, timeout=15, accept=None):
    headers = dict(UA, Accept=accept) if accept else UA
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read()


def _download(url, progress=None, timeout=60):
    """The file at `url`, with progress(done, total) along the way."""
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        total = int(r.headers.get("Content-Length") or 0)
        out = bytearray()
        while True:
            chunk = r.read(CHUNK)
            if not chunk:
                return bytes(out)
            out += chunk
            if progress is not None:
                progress(len(out), total)


def check():
    """The latest release: {current, latest, available, title, notes, date, url, download, signature, size}."""
    try:
        rel = json.loads(_get(API_LATEST, accept="application/vnd.github+json"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("в репозитории пока нет выпусков") from None
        return _check_without_api()       # 403/429: the API allows 60 requests an hour per address
    latest = str(rel.get("tag_name", "")).lstrip("vV")
    assets = rel.get("assets") or []
    name = asset_name(latest)
    asset = next((a for a in assets if a.get("name") == name), None)
    sig = next((a for a in assets if a.get("name") == name + ".sig"), None)
    return {"current": VERSION, "latest": latest,
            "available": parse_version(latest) > parse_version(VERSION),
            "title": rel.get("name") or f"MarinRemote {latest}", "notes": rel.get("body") or "",
            "date": (rel.get("published_at") or "")[:10], "url": rel.get("html_url") or RELEASES_PAGE,
            "asset": name, "download": asset["browser_download_url"] if asset else "",
            "size": asset.get("size", 0) if asset else 0,
            "signature": sig["browser_download_url"] if sig else ""}


def _check_without_api():
    """github.com/…/releases/latest redirects to …/releases/tag/<tag>, with no hourly limit."""
    req = urllib.request.Request(f"{RELEASES_PAGE}/latest", headers=UA, method="HEAD")
    with urllib.request.urlopen(req, timeout=15) as r:
        final = r.geturl()
    if "/releases/tag/" not in final:
        raise RuntimeError("в репозитории пока нет выпусков")
    tag = final.rstrip("/").rsplit("/", 1)[-1]
    latest = tag.lstrip("vV")
    name = asset_name(latest)
    return {"current": VERSION, "latest": latest, "available": parse_version(latest) > parse_version(VERSION),
            "title": f"MarinRemote {latest}", "notes": "", "date": "", "url": final, "asset": name,
            "download": f"{RELEASES_PAGE}/download/{tag}/{name}", "size": 0,
            "signature": f"{RELEASES_PAGE}/download/{tag}/{name}.sig"}


_installer = None      # (path, sha256 when its signature was checked, version)


def download(info, progress=None):
    """Download the installer of `info` and check its signature. Returns its version."""
    global _installer
    name = asset_name(info["latest"])
    if not info.get("download") or not info.get("signature"):
        raise RuntimeError("выпуск не подписан или в нём нет установщика — скачайте его со страницы выпусков")
    try:
        sig = _get(info["signature"], timeout=30).decode("utf-8", errors="replace")
    except urllib.error.HTTPError:
        raise RuntimeError("выпуск не подписан — скачайте установщик со страницы выпусков") from None
    data = _download(info["download"], progress)
    if info.get("size") and len(data) != info["size"]:
        raise RuntimeError("файл скачался не полностью — попробуйте ещё раз")
    try:
        signing.verify(RELEASE_KEY, name, data, sig)
    except ValueError as e:
        raise RuntimeError(f"подпись обновления не сходится ({e}) — установка отменена") from None
    folder = Path(tempfile.gettempdir()) / "MarinRemote-update"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    _installer = (path, signing.digest(data), info["latest"])
    return info["latest"]


def downloaded():
    """The version of an installer downloaded and checked, or None."""
    return _installer[2] if _installer else None


def install():
    """Start the downloaded installer (silently; it closes this app and starts the new version).
    The caller quits right after. False if there is nothing (intact) to install."""
    if not (can_install() and _installer):
        return False
    path, sha, _ = _installer
    try:
        if signing.digest(path) != sha:       # swapped while it waited in %TEMP%
            return False
    except OSError:
        return False
    subprocess.Popen([str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return True
