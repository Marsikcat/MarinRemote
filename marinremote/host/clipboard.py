"""
Text on the clipboard, both ways, for sessions with the "clipboard" right (the session has
already checked it for what comes from the viewer). Only what is copied during the session
travels — whatever was on the clipboard before (a password, say) stays put.

The clipboard itself belongs to the user's session (Qt here, a helper process in stage 2), so it
is reached through `board`: set_text(text) from any thread, on_change(text) called on a change.
"""

from .core import Service

MAX_TEXT = 1_000_000


def clean(text):
    return text if isinstance(text, str) and 0 < len(text) <= MAX_TEXT else None


class ClipboardService(Service):
    def __init__(self, core, board):
        self.core, self.net = core, core.net
        self.board = board
        self.sessions = {}
        board.on_change = lambda text: self.net.call(self._local_changed, text)

    def session_started(self, s):
        if s.has("clipboard"):
            self.sessions[s.sid] = s

    def session_ended(self, s):
        self.sessions.pop(s.sid, None)

    def on_message(self, s, msg):
        if msg["t"] != "clip":
            return False
        text = clean(msg.get("text"))
        if text is not None and s.sid in self.sessions:
            self.board.set_text(text)
        return True

    def _local_changed(self, text):
        text = clean(text)
        if text is None:
            return
        for s in list(self.sessions.values()):
            s.post({"t": "clip", "text": text})
