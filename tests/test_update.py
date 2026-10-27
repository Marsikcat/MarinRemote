"""
Updates: a release is noticed, its installer is taken only with a valid signature of the release
key (not another key's, not for another file, not altered), and a copy that isn't an installed
build never runs an installer. No network: GitHub is faked.
"""
import json

import pytest

from marinremote import signing, updater


@pytest.fixture
def key(monkeypatch):
    secret = signing.new_secret()
    monkeypatch.setattr(updater, "RELEASE_KEY", signing.public_key(secret))
    return secret


def test_signature_round_trip_and_what_it_refuses(key):
    data = b"MZ installer"
    sig = signing.sign(key, "MarinRemote-Setup-9.9.9.exe", signing.digest(data))
    public = signing.public_key(key)
    signing.verify(public, "MarinRemote-Setup-9.9.9.exe", data, sig)
    with pytest.raises(ValueError, match="отличается"):
        signing.verify(public, "MarinRemote-Setup-9.9.9.exe", data + b"!", sig)
    with pytest.raises(ValueError, match="другого файла"):
        signing.verify(public, "MarinRemote-Setup-1.0.0.exe", data, sig)       # an old one passed off as new
    with pytest.raises(ValueError, match="не от ключа"):
        signing.verify(signing.public_key(signing.new_secret()), "MarinRemote-Setup-9.9.9.exe", data, sig)
    with pytest.raises(ValueError, match="повреждён"):
        signing.verify(public, "MarinRemote-Setup-9.9.9.exe", data, "nope")


def release(version, signed=True):
    name = f"MarinRemote-Setup-{version}.exe"
    assets = [{"name": name, "size": 12, "browser_download_url": f"https://example/{name}"}]
    if signed:
        assets.append({"name": name + ".sig", "browser_download_url": f"https://example/{name}.sig"})
    return {"tag_name": f"v{version}", "name": f"MarinRemote {version}", "body": "notes",
            "published_at": "2026-10-27T10:00:00Z", "html_url": "https://example/tag", "assets": assets}


def test_a_newer_release_is_noticed(monkeypatch):
    monkeypatch.setattr(updater, "VERSION", "0.2.0")
    monkeypatch.setattr(updater, "_get", lambda url, **kw: json.dumps(release("0.10.0")).encode())
    info = updater.check()
    assert info["available"] and info["latest"] == "0.10.0"           # 10 > 2, not "1" < "2"
    assert info["download"].endswith("MarinRemote-Setup-0.10.0.exe") and info["signature"].endswith(".sig")
    monkeypatch.setattr(updater, "_get", lambda url, **kw: json.dumps(release("0.2.0")).encode())
    assert not updater.check()["available"]


def fake_github(monkeypatch, tmp_path, data, sig):
    monkeypatch.setattr(updater, "_get", lambda url, **kw: sig.encode())
    monkeypatch.setattr(updater, "_download", lambda url, progress=None: data)
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))


def test_only_a_signed_installer_is_taken(monkeypatch, tmp_path, key):
    data = b"MZ installer"
    info = {"latest": "9.9.9", "download": "https://x/a.exe", "signature": "https://x/a.exe.sig", "size": 12}
    good = signing.sign(key, "MarinRemote-Setup-9.9.9.exe", signing.digest(data))
    fake_github(monkeypatch, tmp_path, data, good)
    assert updater.download(info) == "9.9.9" and updater.downloaded() == "9.9.9"
    assert (tmp_path / "MarinRemote-update" / "MarinRemote-Setup-9.9.9.exe").read_bytes() == data

    fake_github(monkeypatch, tmp_path, b"MZ evil!!!!!", good)                # swapped on the way
    with pytest.raises(RuntimeError, match="подпись"):
        updater.download(info)
    other = signing.new_secret()                                              # someone else's key
    fake_github(monkeypatch, tmp_path, data, signing.sign(other, "MarinRemote-Setup-9.9.9.exe", signing.digest(data)))
    with pytest.raises(RuntimeError, match="подпись"):
        updater.download(info)
    with pytest.raises(RuntimeError, match="не подписан"):
        updater.download(dict(info, signature=""))


def test_a_copy_from_the_sources_never_runs_an_installer(monkeypatch, tmp_path, key):
    data = b"MZ installer"
    fake_github(monkeypatch, tmp_path, data, signing.sign(key, "MarinRemote-Setup-9.9.9.exe", signing.digest(data)))
    updater.download({"latest": "9.9.9", "download": "u", "signature": "s", "size": 0})
    ran = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **kw: ran.append(a))
    assert not updater.can_install() and updater.install() is False and ran == []
    monkeypatch.setattr(updater, "can_install", lambda: True)
    (tmp_path / "MarinRemote-update" / "MarinRemote-Setup-9.9.9.exe").write_bytes(b"swapped")   # waited, got replaced
    assert updater.install() is False and ran == []
