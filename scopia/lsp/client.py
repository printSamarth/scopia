"""Minimal LSP client: JSON-RPC over stdio with `Content-Length` framing.

Hand-rolled for the same reason the SCIP prototype decoded protobuf by hand: a client
library is a dependency and a version treadmill, and scopia needs four methods.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 20.0


class LspError(RuntimeError):
    pass


class LspClient:
    default_timeout = DEFAULT_TIMEOUT

    def __init__(self, command: list[str], root: Path) -> None:
        self.root = root
        self.capabilities: dict[str, Any] = {}
        self._next_id = 0
        self._pending: dict[int, _Waiter] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False
        try:
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=str(root),
            )
        except OSError as exc:
            raise LspError(f"cannot start {command[0]}: {exc}") from exc
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # -- lifecycle --------------------------------------------------------------------

    def initialize(self, timeout: float = 60.0) -> None:
        result = self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": self.root.resolve().as_uri(),
                "capabilities": {
                    "textDocument": {
                        "definition": {"linkSupport": False},
                        "references": {},
                        "callHierarchy": {},
                    },
                    "workspace": {"didChangeWatchedFiles": {"dynamicRegistration": False}},
                },
                "workspaceFolders": [
                    {"uri": self.root.resolve().as_uri(), "name": self.root.name}
                ],
            },
            timeout=timeout,
        )
        # Never assume a capability: servers vary, so callers must ask before they query.
        self.capabilities = (result or {}).get("capabilities", {})
        self.notify("initialized", {})

    def close(self) -> None:
        """`shutdown` + `exit`, then make sure the process is gone. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        try:
            self._send({"jsonrpc": "2.0", "id": self._take_id(), "method": "shutdown"})
            self._send({"jsonrpc": "2.0", "method": "exit"})
        except (OSError, LspError):
            pass
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        for pipe in (self._proc.stdin, self._proc.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
        for waiter in list(self._pending.values()):
            waiter.fail("server closed")

    def alive(self) -> bool:
        return not self._closed and self._proc.poll() is None

    def __enter__(self) -> LspClient:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- requests ---------------------------------------------------------------------

    def request(self, method: str, params: Any, timeout: float | None = None) -> Any:
        timeout = self.default_timeout if timeout is None else timeout
        waiter = _Waiter()
        with self._lock:
            msg_id = self._take_id()
            self._pending[msg_id] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        except (OSError, ValueError) as exc:
            self._pending.pop(msg_id, None)
            raise LspError(f"{method}: server not writable") from exc
        try:
            return waiter.wait(timeout, method)
        finally:
            self._pending.pop(msg_id, None)

    def notify(self, method: str, params: Any) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})
        except (OSError, ValueError) as exc:
            raise LspError(f"{method}: server not writable") from exc

    # -- plumbing ---------------------------------------------------------------------

    def _take_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _send(self, message: dict) -> None:
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        frame = b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        with self._write_lock:
            stdin = self._proc.stdin
            if stdin is None or stdin.closed:
                raise OSError("stdin closed")
            stdin.write(frame)
            stdin.flush()

    def _read_message(self) -> dict | None:
        stdout = self._proc.stdout
        assert stdout is not None
        length = None
        while True:
            line = stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            name, _, value = line.partition(b":")
            if name.lower() == b"content-length":
                length = int(value)
        if length is None:
            return None
        body = stdout.read(length)
        if len(body) < length:
            return None
        return json.loads(body)

    def _read_loop(self) -> None:
        try:
            while True:
                message = self._read_message()
                if message is None:
                    break
                self._dispatch(message)
        except (OSError, ValueError):
            pass
        finally:
            for waiter in list(self._pending.values()):
                waiter.fail("server exited")

    def _dispatch(self, message: dict) -> None:
        if "method" in message:
            # Requests *from* the server (progress, dynamic registration, configuration)
            # must be answered or some servers stall waiting. We have no opinions, so say so.
            if "id" in message:
                result: Any = [] if message["method"] == "workspace/configuration" else None
                try:
                    self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})
                except OSError:
                    pass
            return
        waiter = self._pending.get(message.get("id"))
        if waiter is None:
            return
        if "error" in message:
            waiter.fail(str(message["error"].get("message", "error")))
        else:
            waiter.ok(message.get("result"))


class _Waiter:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._result: Any = None
        self._error: str | None = None

    def ok(self, result: Any) -> None:
        self._result = result
        self._event.set()

    def fail(self, reason: str) -> None:
        self._error = reason
        self._event.set()

    def wait(self, timeout: float, method: str) -> Any:
        if not self._event.wait(timeout):
            raise LspError(f"{method}: timed out after {timeout:.0f}s")
        if self._error is not None:
            raise LspError(f"{method}: {self._error}")
        return self._result
