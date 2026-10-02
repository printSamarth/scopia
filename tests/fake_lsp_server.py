"""A stand-in language server, speaking JSON-RPC over stdio.

Mirrors how the SCIP tests synthesised protobuf instead of requiring the real indexer:
the client is tested against canned replies, so the suite installs nothing.

argv[1] is a JSON config:
  definitions: {"<path>:<line0>:<col>": [[<relpath>, <line0>], ...] | "external"}
  mode: "normal" | "silent" (never answers definition) | "crash" (exits on definition)
  log: path to append every received message to
"""

import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text())
log = config.get("log")


def read():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            length = int(value)
    return json.loads(sys.stdin.buffer.read(length))


def send(message):
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()


root = None
while True:
    msg = read()
    if msg is None:
        break
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps(msg) + "\n")
    method = msg.get("method")
    if method == "initialize":
        root = msg["params"]["rootUri"]
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {
            "definitionProvider": config.get("definition", True)}}})
    elif method == "textDocument/definition":
        mode = config.get("mode", "normal")
        if mode == "crash":
            sys.exit(1)
        if mode == "silent":
            continue
        # Ask the client something first, as real servers do, and carry on regardless.
        send({"jsonrpc": "2.0", "id": 9001, "method": "window/workDoneProgress/create",
              "params": {"token": "t"}})
        uri = msg["params"]["textDocument"]["uri"]
        path = uri[len(root) + 1:] if uri.startswith(root) else uri
        pos = msg["params"]["position"]
        answer = config["definitions"].get(f"{path}:{pos['line']}:{pos['character']}", [])
        if answer == "external":
            result = [{"uri": "file:///usr/lib/python3/builtins.pyi",
                       "range": {"start": {"line": 3, "character": 0},
                                 "end": {"line": 3, "character": 1}}}]
        else:
            result = [{"uri": f"{root}/{rel}",
                       "range": {"start": {"line": line, "character": 4},
                                 "end": {"line": line, "character": 7}}}
                      for rel, line in answer]
        send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
    elif method == "exit":
        break
