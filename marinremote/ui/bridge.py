"""The network thread ↔ the Qt thread: signals carry events over, answers go back by
NetLoop.call. Qt queues a signal emitted on another thread into the receiver's thread."""

import asyncio

from PySide6.QtCore import QObject, Signal

from ..host.core import Approver, HostEvents
from ..viewer.session import ViewerEvents


class QtApprover(QObject, Approver):
    """Shows the confirmation window and hands its answer back to the host core."""

    asked = Signal(dict)        # a request to show
    withdrawn = Signal(int)     # request id: close its window unanswered

    def __init__(self, net):
        super().__init__()
        self.net = net
        self._futures = {}

    async def ask(self, request):
        fut = asyncio.get_running_loop().create_future()
        self._futures[request["id"]] = fut
        self.asked.emit(request)
        try:
            return await fut
        finally:
            self._futures.pop(request["id"], None)

    def cancel(self, request_id):
        self.withdrawn.emit(request_id)

    def answer(self, request_id, decision):
        """From the Qt thread."""
        self.net.call(self._resolve, request_id, decision)

    def _resolve(self, request_id, decision):
        fut = self._futures.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(decision)


class HostBridge(QObject, HostEvents):
    sessions = Signal(list)
    settings = Signal()
    journal = Signal(dict)

    def sessions_changed(self, sessions):
        self.sessions.emit(sessions)

    def settings_changed(self):
        self.settings.emit()

    def journal_entry(self, entry):
        self.journal.emit(entry)


class ViewerBridge(QObject, ViewerEvents):
    state = Signal(object)              # the session
    received = Signal(object, dict)     # the session, a message from the host

    def state_changed(self, session):
        self.state.emit(session)

    def message(self, session, msg):
        self.received.emit(session, msg)
