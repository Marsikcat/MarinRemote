"""
The local clipboard for the clipboard sync (the host's ClipboardService and the session window):
text only, changes only, nothing echoed back, and nothing a password manager asked clipboard
watchers to leave alone.
"""

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication

# how programs (password managers among them) ask clipboard watchers to leave a copy alone:
# the mere presence of a format, or a DWORD 0 in one
PRIVATE = ("ExcludeClipboardContentFromMonitorProcessing", "Clipboard Viewer Ignore")
PRIVATE_IF_ZERO = ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard")


def private(md):
    for f in md.formats():
        if any(name in f for name in PRIVATE):
            return True
        if any(name in f for name in PRIVATE_IF_ZERO):
            data = bytes(md.data(f))
            if len(data) >= 4 and int.from_bytes(data[:4], "little") == 0:
                return True
    return False


class QtClipboard(QObject):
    """set_text(text) from any thread; on_change(text) is called (on the Qt thread) when the user
    copies text here."""

    _set = Signal(str)

    def __init__(self):
        super().__init__()
        self.on_change = None
        self._from_remote = None      # what we put there ourselves: not news
        self._last = None
        self._set.connect(self._apply)
        QGuiApplication.clipboard().dataChanged.connect(self._changed)

    def set_text(self, text):
        self._set.emit(text)

    def _apply(self, text):
        cb = QGuiApplication.clipboard()
        if cb.text() == text:
            return
        self._from_remote = text
        self._last = text
        cb.setText(text)

    def _changed(self):
        md = QGuiApplication.clipboard().mimeData()
        if md is None or not md.hasText() or private(md):
            return
        text = md.text()
        if text == self._from_remote:
            self._from_remote = None
            return
        if text == self._last:
            return
        self._last = text
        if self.on_change is not None and text:
            self.on_change(text)
