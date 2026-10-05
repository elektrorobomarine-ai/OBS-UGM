"""
GRC-UGM-PERTAMINA OBS
Local IPC command bus

Purpose
-------
Provide local inter-process command transport between consumer GUIs and
obs_setting.py without opening any additional TCP connection to the OBS.

Architecture:
    Camera / MiniSEED -> QLocalSocket -> OBS Setting -> TCP 54300 -> OBS

No consumer module connects directly to OBS TCP 54300/54301.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

IPC_SERVER_NAME = "GRC_UGM_PERTAMINA_OBS_COMMAND_BUS_V1"
IPC_MAX_LINE_BYTES = 8192
IPC_RECONNECT_MS = 1000


class OBSIPCClient(QObject):
    """Reusable local command-bus client.

    This object deliberately mimics a small subset of QThread's lifecycle API
    (start/stop/wait/isRunning) so existing GUI modules can migrate from their
    old direct TCP worker with minimal changes.
    """

    connection_changed = Signal(bool, str)
    response_received = Signal(object)
    error_occurred = Signal(str)

    def __init__(self, source: str, parent=None):
        super().__init__(parent)
        self.source = str(source).strip() or "consumer"
        self._running = False
        self._buffer = bytearray()
        self._obs_command_connected = False

        self.socket = QLocalSocket(self)
        self.socket.connected.connect(self._on_connected)
        self.socket.disconnected.connect(self._on_disconnected)
        self.socket.readyRead.connect(self._on_ready_read)
        self.socket.errorOccurred.connect(self._on_error)

        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setInterval(IPC_RECONNECT_MS)
        self.reconnect_timer.timeout.connect(self._ensure_connected)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.reconnect_timer.start()
        self._ensure_connected()

    def stop(self) -> None:
        self._running = False
        self.reconnect_timer.stop()
        self._obs_command_connected = False
        self._buffer.clear()
        try:
            self.socket.abort()
        except Exception:
            pass
        self.connection_changed.emit(False, "OBS Setting IPC stopped")

    def wait(self, _timeout_ms: int = 0) -> bool:
        # Compatibility shim; QLocalSocket is event-driven in the GUI thread.
        return True

    def isRunning(self) -> bool:
        return bool(self._running)

    def is_ipc_connected(self) -> bool:
        return self.socket.state() == QLocalSocket.ConnectedState

    def is_obs_command_connected(self) -> bool:
        return bool(self._obs_command_connected)

    def send_rmcmd(self, command_code: int) -> str:
        return self.send_request({
            "type": "rmcmd",
            "code": int(command_code),
        })

    def send_time_sync(self) -> str:
        return self.send_request({"type": "time_sync"})

    def send_request(self, payload: dict) -> str:
        if not self._running:
            raise RuntimeError("OBS Setting IPC client is not running.")
        if not self.is_ipc_connected():
            raise RuntimeError("OBS Setting IPC is not connected.")

        request_id = str(payload.get("request_id") or uuid.uuid4().hex)
        message = dict(payload)
        message["request_id"] = request_id
        message["source"] = self.source

        raw = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        if len(raw) > IPC_MAX_LINE_BYTES:
            raise ValueError("IPC command exceeds local message limit.")

        written = self.socket.write(raw)
        if written < 0:
            raise RuntimeError(self.socket.errorString() or "IPC write failed.")
        self.socket.flush()
        return request_id

    def _ensure_connected(self) -> None:
        if not self._running:
            return
        state = self.socket.state()
        if state in (
            QLocalSocket.ConnectedState,
            QLocalSocket.ConnectingState,
        ):
            return
        self.socket.abort()
        self.socket.connectToServer(IPC_SERVER_NAME)

    def _on_connected(self) -> None:
        # OBS availability is supplied by the broker's status frame immediately
        # after connection. Do not report OBS command availability yet.
        self.connection_changed.emit(
            False,
            "Connected to OBS Setting IPC; waiting for OBS command-link status",
        )

    def _on_disconnected(self) -> None:
        self._obs_command_connected = False
        if self._running:
            self.connection_changed.emit(
                False,
                "OBS Setting IPC disconnected",
            )

    def _on_error(self, _error) -> None:
        if not self._running:
            return
        text = self.socket.errorString().strip() or "Local IPC error"
        self._obs_command_connected = False
        self.error_occurred.emit(text)
        self.connection_changed.emit(False, text)

    def _on_ready_read(self) -> None:
        self._buffer.extend(bytes(self.socket.readAll()))
        if len(self._buffer) > IPC_MAX_LINE_BYTES * 4:
            self._buffer.clear()
            self.error_occurred.emit("OBS Setting IPC receive buffer overflow.")
            return

        while b"\n" in self._buffer:
            raw_line, _, remaining = self._buffer.partition(b"\n")
            self._buffer = bytearray(remaining)
            if not raw_line.strip():
                continue
            try:
                message = json.loads(raw_line.decode("utf-8"))
            except Exception as exc:
                self.error_occurred.emit(f"Invalid OBS Setting IPC response: {exc}")
                continue

            if not isinstance(message, dict):
                continue

            if message.get("type") == "status":
                self._obs_command_connected = bool(
                    message.get("obs_command_connected", False)
                )
                detail = str(
                    message.get("detail")
                    or (
                        "OBS command TCP 54300 connected via OBS Setting"
                        if self._obs_command_connected
                        else "OBS command TCP 54300 is not connected in OBS Setting"
                    )
                )
                self.connection_changed.emit(
                    self._obs_command_connected,
                    detail,
                )

            self.response_received.emit(message)


class OBSIPCServer(QLocalServer):
    """Local server owned exclusively by obs_setting.py."""

    request_received = Signal(object, object)
    client_count_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._clients: dict[int, QLocalSocket] = {}
        self._buffers: dict[int, bytearray] = {}
        self._obs_command_connected = False
        self.newConnection.connect(self._accept_pending)

    def start(self) -> None:
        # Detect a live broker before removing a potentially stale endpoint.
        probe = QLocalSocket()
        probe.connectToServer(IPC_SERVER_NAME)
        if probe.waitForConnected(80):
            probe.abort()
            raise RuntimeError(
                "Another OBS Setting command broker is already running."
            )
        probe.abort()

        QLocalServer.removeServer(IPC_SERVER_NAME)
        if not self.listen(IPC_SERVER_NAME):
            raise RuntimeError(
                self.errorString()
                or "Unable to start OBS Setting local command broker."
            )

    def stop(self) -> None:
        for sock in list(self._clients.values()):
            try:
                sock.abort()
            except Exception:
                pass
        self._clients.clear()
        self._buffers.clear()
        self.close()
        QLocalServer.removeServer(IPC_SERVER_NAME)
        self.client_count_changed.emit(0)

    def set_obs_command_connected(self, connected: bool, detail: str = "") -> None:
        self._obs_command_connected = bool(connected)
        payload = {
            "type": "status",
            "obs_command_connected": self._obs_command_connected,
            "detail": str(detail),
        }
        self.broadcast(payload)

    def send_response(self, sock: Optional[QLocalSocket], payload: dict) -> None:
        if sock is None or sock.state() != QLocalSocket.ConnectedState:
            return
        raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        if len(raw) > IPC_MAX_LINE_BYTES:
            return
        sock.write(raw)
        sock.flush()

    def send_to_client_id(self, client_id: int, payload: dict) -> None:
        self.send_response(self._clients.get(int(client_id)), payload)

    def broadcast(self, payload: dict) -> None:
        for sock in list(self._clients.values()):
            self.send_response(sock, payload)

    def _accept_pending(self) -> None:
        while self.hasPendingConnections():
            sock = self.nextPendingConnection()
            if sock is None:
                continue
            client_id = id(sock)
            self._clients[client_id] = sock
            self._buffers[client_id] = bytearray()
            sock.readyRead.connect(lambda s=sock: self._read_client(s))
            sock.disconnected.connect(lambda s=sock: self._drop_client(s))
            self.send_response(sock, {
                "type": "status",
                "obs_command_connected": self._obs_command_connected,
                "detail": (
                    "OBS command TCP 54300 connected via OBS Setting"
                    if self._obs_command_connected
                    else "OBS Setting is running; TCP 54300 is not connected"
                ),
            })
        self.client_count_changed.emit(len(self._clients))

    def _drop_client(self, sock: QLocalSocket) -> None:
        client_id = id(sock)
        self._clients.pop(client_id, None)
        self._buffers.pop(client_id, None)
        try:
            sock.deleteLater()
        except Exception:
            pass
        self.client_count_changed.emit(len(self._clients))

    def _read_client(self, sock: QLocalSocket) -> None:
        client_id = id(sock)
        buf = self._buffers.setdefault(client_id, bytearray())
        buf.extend(bytes(sock.readAll()))
        if len(buf) > IPC_MAX_LINE_BYTES * 4:
            buf.clear()
            self.send_response(sock, {
                "type": "error",
                "message": "IPC request buffer overflow",
            })
            return

        while b"\n" in buf:
            raw_line, _, remaining = buf.partition(b"\n")
            buf[:] = remaining
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line.decode("utf-8"))
            except Exception as exc:
                self.send_response(sock, {
                    "type": "error",
                    "message": f"Invalid IPC JSON: {exc}",
                })
                continue
            if not isinstance(payload, dict):
                self.send_response(sock, {
                    "type": "error",
                    "message": "IPC request must be a JSON object",
                })
                continue
            payload["_client_id"] = client_id
            self.request_received.emit(payload, sock)
