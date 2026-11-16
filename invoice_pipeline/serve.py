"""A tiny localhost web server for the live review workbench.

Standard-library `http.server` only, bound to 127.0.0.1. `GET /` renders the workbench from the
ledger's current state; `POST /api/decision` approves, rejects, or routes an escalated invoice
through the same `apply_decision` path the CLI uses, so both write the ledger identically.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import cli, db, workbench
from .config import Settings

VALID_DECISIONS = ("approve", "reject", "route")


def handle_decision(conn, payload: dict) -> tuple[int, dict]:
    """The decision endpoint's logic, split out so it can be tested without a socket."""
    payload = payload or {}
    number = payload.get("invoice_number")
    decision = payload.get("decision")
    reason = payload.get("reason") or ""
    if not number or decision not in VALID_DECISIONS:
        return 400, {"ok": False, "error": "invoice_number and a valid decision are required."}
    result = cli.apply_decision(conn, number, decision, reason)
    return (200 if result["ok"] else 400), result


class _Handler(BaseHTTPRequestHandler):
    settings: Settings | None = None

    def _open(self):
        conn = db.connect(self.settings.db_path)
        db.init_db(conn)
        return conn

    def _send(self, code: int, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802  (http.server naming)
        if self.path.split("?")[0] != "/":
            self._send(404, "Not found", "text/plain; charset=utf-8")
            return
        conn = self._open()
        try:
            html = workbench.render(workbench.build_view_model_from_ledger(conn, self.settings), live=True)
        finally:
            conn.close()
        self._send(200, html, "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/api/decision":
            self._send(404, json.dumps({"ok": False, "error": "Not found."}), "application/json")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, json.dumps({"ok": False, "error": "Invalid JSON."}), "application/json")
            return
        conn = self._open()
        try:
            code, result = handle_decision(conn, payload)
        finally:
            conn.close()
        self._send(code, json.dumps(result), "application/json")

    def log_message(self, *args) -> None:  # keep stdout clean; the pipeline logs to stderr
        return


def run(settings: Settings, port: int = 8765) -> None:
    _Handler.settings = settings
    server = HTTPServer(("127.0.0.1", port), _Handler)
    print(f"Review workbench live at http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
