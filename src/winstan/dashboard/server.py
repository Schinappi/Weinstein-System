from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from winstan.dashboard.service import DashboardService

STATIC_DIR = Path(__file__).resolve().parent / "static"


class DashboardHandler(BaseHTTPRequestHandler):
    service: DashboardService

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/dashboard":
            self._send_json(self.service.get_dashboard_payload())
            return

        if path == "/api/search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            self._send_json({"items": self.service.search_stocks(query)})
            return

        if path.startswith("/api/stock/") and path.endswith("/analysis"):
            symbol = unquote(path.removeprefix("/api/stock/").removesuffix("/analysis").rstrip("/"))
            try:
                self._send_json(self.service.get_stock_analysis(symbol))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if path.startswith("/api/stock/"):
            symbol = unquote(path.removeprefix("/api/stock/"))
            try:
                self._send_json(self.service.get_stock_detail(symbol))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        self._serve_static(path)

    def log_message(self, format: str, *args) -> None:
        return

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"/", ""} else path.lstrip("/")
        file_path = (STATIC_DIR / relative).resolve()
        if STATIC_DIR not in file_path.parents and file_path != STATIC_DIR:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type = _content_type_for(file_path)
        body = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(config_path: str | Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    service = DashboardService(config_path)

    class BoundHandler(DashboardHandler):
        pass

    BoundHandler.service = service
    server = ThreadingHTTPServer((host, port), BoundHandler)
    print(f"Dashboard server running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _content_type_for(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix == ".html":
        return "text/html; charset=utf-8"
    if suffix == ".css":
        return "text/css; charset=utf-8"
    if suffix == ".js":
        return "application/javascript; charset=utf-8"
    if suffix == ".json":
        return "application/json; charset=utf-8"
    return "application/octet-stream"
