"""A tiny local-only HTTP service used to verify B10A process contracts."""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _Handler(BaseHTTPRequestHandler):
    server_version = "B10A-Mock/1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        payload = {
            "schema_version": "b10a.health.v1",
            "status": "HEALTHY",
            "service": "mock-http",
            "pid": os.getpid(),
        }
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/_b10a/shutdown":
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("X-B10A-Nonce") != getattr(self.server, "b10a_nonce", None):
            self.send_response(403)
            self.end_headers()
            return
        body = b'{"status":"STOPPING"}'
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, format: str, *args: object) -> None:
        # Do not echo request paths or arbitrary values into the managed log.
        print("request status=%s" % (args[1] if len(args) > 1 else "unknown"), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="B10A local mock service")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--identity-file", required=True, type=Path)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--exit-after", type=float, default=None)
    args = parser.parse_args(argv)

    identity = args.identity_file.resolve(strict=False)
    identity.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": os.getpid(), "nonce": args.nonce, "service": "mock-http"}
    identity.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    server.b10a_nonce = args.nonce

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, stop)
    if args.exit_after is not None and args.exit_after > 0:
        threading.Timer(args.exit_after, lambda: threading.Thread(target=server.shutdown, daemon=True).start()).start()
    print("listening host=%s port=%s" % (args.host, args.port), flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        try:
            current = json.loads(identity.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        if current == payload:
            try:
                identity.unlink()
            except FileNotFoundError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
