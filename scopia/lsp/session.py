"""One language-server process per language, started lazily and killed on exit."""

from __future__ import annotations

import atexit
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from .client import LspClient, LspError

#: language name (as in `LanguageAdapter.name`) -> candidate commands, first found wins.
#: `jedi-language-server` is the default for Python on purpose: it is pure Python, so
#: `pip install` is the whole cost. pyright's pip package wraps a Node binary, and dragging
#: Node in to analyse Python is the cost that sank the SCIP prototype.
SERVERS: dict[str, list[list[str]]] = {
    "python": [["jedi-language-server"]],
}

_LANGUAGE_IDS = {"python": "python"}

#: (relative path or None when outside the repo, zero-based line)
Location = tuple[str | None, int]


def server_command(language: str) -> list[str] | None:
    for command in SERVERS.get(language, ()):
        found = shutil.which(command[0])
        if found is None:
            # Installed beside the interpreter running scopia (a venv that was never
            # activated) is still installed.
            beside = Path(sys.executable).parent / command[0]
            found = str(beside) if beside.exists() else None
        if found:
            return [found, *command[1:]]
    return None


class Session:
    """A started, initialised server for one language."""

    def __init__(self, client: LspClient, root: Path, language: str, warmup: float) -> None:
        self.client = client
        self.root = root.resolve()
        self.language = language
        self.warmup = warmup
        self._versions: dict[str, tuple[int, int]] = {}  # rel path -> (mtime_ns, version)
        self._lock = threading.Lock()

    @property
    def supports_definition(self) -> bool:
        return bool(self.client.capabilities.get("definitionProvider"))

    def alive(self) -> bool:
        return self.client.alive()

    def close(self) -> None:
        self.client.close()

    def definition(self, path: str, line: int, col: int) -> list[Location] | None:
        """Where the symbol at (line, col) is defined.

        `None` means the server could not answer (error, timeout, dead) — unknown. `[]`
        means it answered and found nothing. Callers must not conflate the two: only the
        first says nothing about the call.
        """
        try:
            with self._lock:
                self._sync(path)
                result = self.client.request(
                    "textDocument/definition",
                    {
                        "textDocument": {"uri": (self.root / path).as_uri()},
                        "position": {"line": line, "character": col},
                    },
                )
        except (LspError, OSError):
            return None
        return [self._locate(item) for item in _as_list(result)]

    def _sync(self, path: str) -> None:
        """Tell the server what is on disk. `didOpen` once, `didChange` when it moves —
        the agent is rewriting files under us, and a stale document answers stale."""
        full = self.root / path
        mtime = full.stat().st_mtime_ns
        known = self._versions.get(path)
        if known is not None and known[0] == mtime:
            return
        text = full.read_text("utf-8", "replace")
        uri = full.as_uri()
        if known is None:
            self.client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": _LANGUAGE_IDS.get(self.language, self.language),
                        "version": 1,
                        "text": text,
                    }
                },
            )
            self._versions[path] = (mtime, 1)
        else:
            version = known[1] + 1
            self.client.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                },
            )
            self._versions[path] = (mtime, version)

    def _locate(self, item: dict) -> Location:
        uri = item.get("uri") or item.get("targetUri") or ""
        rng = item.get("range") or item.get("targetSelectionRange") or {}
        line = rng.get("start", {}).get("line", 0)
        file = Path(unquote(urlparse(uri).path)) if uri.startswith("file:") else None
        try:
            rel = str(file.resolve().relative_to(self.root)) if file else None
        except (ValueError, OSError):
            rel = None
        return rel, line


def _as_list(result) -> list[dict]:
    if not result:
        return []
    return result if isinstance(result, list) else [result]


class Sessions:
    """Lazy, per-language, and remembers failure so a missing server is tried once."""

    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self.root = root
        self._enabled = enabled
        self._sessions: dict[str, Session | None] = {}
        self._lock = threading.Lock()
        self.notes: list[str] = []
        atexit.register(self.close)

    def get(self, language: str) -> Session | None:
        if not self._enabled:
            return None
        with self._lock:
            if language in self._sessions:
                session = self._sessions[language]
                if session is not None and not session.alive():
                    self.notes.append(f"{language} language server stopped; using static analysis")
                    self._sessions[language] = None
                    return None
                return session
            session = self._start(language)
            self._sessions[language] = session
            return session

    def _start(self, language: str) -> Session | None:
        command = server_command(language)
        if command is None:
            if language in SERVERS:
                name = SERVERS[language][0][0]
                self.notes.append(f"{language}: {name} not found — using static analysis")
            return None
        began = time.monotonic()
        try:
            client = LspClient(command, self.root)
            client.initialize()
        except LspError as exc:
            self.notes.append(f"{language}: language server failed ({exc}) — using static analysis")
            return None
        session = Session(client, self.root, language, time.monotonic() - began)
        if not session.supports_definition:
            self.notes.append(f"{language}: server lacks go-to-definition — using static analysis")
            session.close()
            return None
        return session

    def warmups(self) -> dict[str, float]:
        return {k: v.warmup for k, v in self._sessions.items() if v is not None}

    def close(self) -> None:
        with self._lock:
            self._enabled = False  # nothing may restart after teardown
            for session in self._sessions.values():
                if session is not None:
                    session.close()
            self._sessions = {}

