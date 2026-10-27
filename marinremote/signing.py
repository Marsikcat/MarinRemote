"""
Release signatures.

Every installer of a release goes up with a `<name>.sig` next to it: the file's name and SHA-256,
signed with the MarinRemote release key (Ed25519). The app installs an update only if that
signature checks out against the public key built into it (config.RELEASE_KEY) — someone who
gets hold of the GitHub repository or swaps a download still cannot push an installer to
everyone. The secret half never leaves the PC it was made on (tools/sign.py).

The name is part of what is signed, so an old (validly signed) installer cannot be passed off
as a newer version either.
"""

import hashlib
import json

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

MAGIC = b"MarinRemote release v1\n"


def digest(data):
    """SHA-256 of bytes or of a file path, hex."""
    h = hashlib.sha256()
    if isinstance(data, (bytes, bytearray)):
        h.update(data)
    else:
        with open(data, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def _message(name, sha256):
    return MAGIC + name.encode() + b"\n" + sha256.encode()


def new_secret():
    return SigningKey.generate().encode().hex()


def public_key(secret_hex):
    return SigningKey(bytes.fromhex(secret_hex)).verify_key.encode().hex()


def sign(secret_hex, name, sha256):
    """The text of `<name>.sig`."""
    sig = SigningKey(bytes.fromhex(secret_hex)).sign(_message(name, sha256)).signature.hex()
    return json.dumps({"file": name, "sha256": sha256, "sig": sig}, indent=1) + "\n"


def verify(public_hex, name, data, sig_text):
    """Raises ValueError unless `sig_text` is the release key's signature of this very file."""
    try:
        sig = json.loads(sig_text)
    except ValueError:
        raise ValueError("файл подписи повреждён") from None
    if not isinstance(sig, dict) or sig.get("file") != name:
        raise ValueError("подпись от другого файла")
    sha256 = digest(data)
    if sig.get("sha256") != sha256:
        raise ValueError("файл отличается от подписанного")
    try:
        VerifyKey(bytes.fromhex(public_hex)).verify(_message(name, sha256), bytes.fromhex(str(sig.get("sig", ""))))
    except (BadSignatureError, ValueError, TypeError):
        raise ValueError("подпись не от ключа MarinRemote") from None
