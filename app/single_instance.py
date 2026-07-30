"""Single-instance guard for the desktop client."""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstanceGuard(QObject):
    """Keep one client process and notify it when another launch is attempted."""

    activate_requested = pyqtSignal()

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = str(name)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)
        self._primary = False

    def acquire(self) -> bool:
        """Return whether this process became the primary instance."""
        probe = QLocalSocket(self)
        probe.connectToServer(self.name)
        if probe.waitForConnected(150):
            probe.write(b"activate")
            probe.waitForBytesWritten(150)
            probe.disconnectFromServer()
            return False

        if not self._server.listen(self.name):
            # A stale server can remain after a forced process termination.
            QLocalServer.removeServer(self.name)
            if not self._server.listen(self.name):
                return False
        self._primary = True
        return True

    def close(self) -> None:
        """Release the local server endpoint during normal application exit."""
        if not self._primary:
            return
        self._server.close()
        QLocalServer.removeServer(self.name)
        self._primary = False

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            socket.readyRead.connect(socket.deleteLater)
            socket.disconnected.connect(socket.deleteLater)
            self.activate_requested.emit()
