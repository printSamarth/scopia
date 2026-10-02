"""Local server for the live page.

A `file://` page cannot poll — browsers block `fetch` on local files, and a
`<meta refresh>` destroys scroll and selection — so the page is served over HTTP.
Bound to 127.0.0.1 only: this serves source code.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..graph.model import Graph
from ..render import html as html_render


class LiveServer:
    def __init__(self, port: int = 0) -> None:
        self.version = 0
        self._lock = threading.Lock()
        self._page = b""
        self._graph_json = b"{}"
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(self))
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self.publish(Graph())

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def publish(self, graph: Graph) -> None:
        """Render once per rebuild, not once per request — pages poll every second."""
        with self._lock:
            self.version += 1
            version = self.version
            page = html_render.render(graph, title="scopia · watch", live_version=version)
            payload = html_render.bundle(graph)
            payload["version"] = version
            self._page = page.encode("utf-8")
            self._graph_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    def snapshot(self) -> tuple[int, bytes, bytes]:
        with self._lock:
            return self.version, self._page, self._graph_json


def _make_handler(server: LiveServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            version, page, graph_json = server.snapshot()
            route = self.path.split("?", 1)[0]
            if route == "/":
                self._send(200, "text/html; charset=utf-8", page)
            elif route == "/version":
                self._send(200, "text/plain; charset=utf-8", str(version).encode())
            elif route == "/graph.json":
                self._send(200, "application/json", graph_json)
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found")

        def _send(self, status: int, kind: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            # The whole point is that the content changes; a cached copy defeats it.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:  # silence per-request noise
            return

    return Handler
