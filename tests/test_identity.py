"""The device key: never on the disk in the clear, kept across restarts, a pre-DPAPI key taken over,
and a copied settings file (another Windows user or PC) is not enough to pass for this device."""
import base64
import json

from marinremote import dpapi
from marinremote.config import Settings


def on_disk(folder):
    return (folder / "settings.json").read_text(encoding="utf-8") + (folder / "settings.backup.json").read_text(
        encoding="utf-8")


def test_the_key_is_kept_encrypted_and_survives_a_restart(tmp_path):
    s = Settings(tmp_path)
    assert s.identity_note == "new" and len(s.secret_key) == 64
    text = on_disk(tmp_path)
    assert s.secret_key not in text and '"secret_key"' not in text
    blob = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["secret_key_dpapi"]
    assert dpapi.unprotect(base64.b64decode(blob)).hex() == s.secret_key
    again = Settings(tmp_path)
    assert again.identity_note is None
    assert (again.secret_key, again.uid) == (s.secret_key, s.uid)


def test_a_plain_key_from_before_is_taken_over_and_removed(tmp_path):
    plain = "ab" * 32
    for name in ("settings.json", "settings.backup.json"):
        (tmp_path / name).write_text(json.dumps({"secret_key": plain, "name": "старый"}), encoding="utf-8")
    s = Settings(tmp_path)
    assert s.identity_note == "migrated" and s.secret_key == plain and s["name"] == "старый"
    assert plain not in on_disk(tmp_path)                         # gone from both files
    assert Settings(tmp_path).secret_key == plain


def test_a_copied_file_does_not_carry_the_identity_over(tmp_path):
    foreign = base64.b64encode(dpapi.protect(bytes(32), b"someone else's machine")).decode()
    (tmp_path / "settings.json").write_text(json.dumps({"secret_key_dpapi": foreign, "name": "копия"}),
                                            encoding="utf-8")
    s = Settings(tmp_path)
    assert s.identity_note == "unreadable"
    assert s.secret_key != bytes(32).hex()                        # a new identity, not the copied one
    assert s["secret_key_unreadable"] == foreign                  # kept aside, not destroyed
    assert Settings(tmp_path).secret_key == s.secret_key          # and the new one is kept from now on


def test_a_damaged_blob_is_not_a_key_either(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"secret_key_dpapi": "bm90IGEga2V5"}), encoding="utf-8")
    s = Settings(tmp_path)
    assert s.identity_note == "unreadable" and len(s.secret_key) == 64
