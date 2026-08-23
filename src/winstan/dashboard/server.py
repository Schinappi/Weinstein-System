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

        if path == "/api/stage1":
            dt = parse_qs(parsed.query).get("date", [""])[0]
            if dt:
                payload = self.service.get_rankings_by_date(dt)
                self._send_json({"stage1": payload.get("stage1", [])})
            else:
                self._send_json(self.service.get_stage1_payload())
            return

        if path == "/api/recommendations":
            self._send_json(self.service.get_recommendations_payload())
            return

        if path == "/api/dashboard":
            self._send_json(self.service.get_dashboard_payload())
            return

        if path == "/api/navigation":
            self._send_json(self.service.get_navigation_payload())
            return

        if path == "/api/rankings/dates":
            self._send_json({"dates": self.service.get_available_dates()})
            return

        if path.startswith("/api/rankings/by-date"):
            dt = parse_qs(parsed.query).get("date", [""])[0]
            if dt:
                self._send_json(self.service.get_rankings_by_date(dt))
            else:
                self._send_json({"error": "Missing 'date' query parameter"}, status=HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/stage2/watchlist":
            self._send_json(self.service.get_stage2_watchlist_payload())
            return

        if path == "/api/stage2/holdings":
            self._send_json(self.service.get_stage2_holdings_payload())
            return

        if path == "/api/shareholder/ranking":
            self._send_json(self.service.get_shareholder_ranking_payload())
            return

        if path == "/api/transition/ranking":
            self._send_json(self.service.get_transition_ranking_payload())
            return

        if path == "/api/continuation/ranking":
            self._send_json(self.service.get_continuation_ranking_payload())
            return

        if path == "/api/demand-support/ranking":
            self._send_json(self.service.get_demand_support_ranking_payload())
            return

        if path == "/api/crash-rebound/ranking":
            self._send_json(self.service.get_crash_rebound_ranking_payload())
            return

        if path == "/api/continuation/refresh-status":
            self._send_json(self.service.get_refresh_status())
            return

        if path == "/api/demand-support/refresh-status":
            self._send_json(self.service.get_demand_refresh_status())
            return

        if path == "/api/search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            self._send_json({"items": self.service.search_stocks(query)})
            return

        if path == "/api/price-monitors":
            self._send_json(self.service.get_price_monitor_payload())
            return

        if path.startswith("/api/stock/") and path.endswith("/analysis"):
            symbol = unquote(path.removeprefix("/api/stock/").removesuffix("/analysis").rstrip("/"))
            try:
                self._send_json(self.service.get_stock_analysis(symbol))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if path.startswith("/api/stock/"):
            symbol = unquote(path.removeprefix("/api/stock/").rstrip("/"))
            chart_type = parse_qs(parsed.query).get("chart", ["weekly"])[0]
            try:
                self._send_json(self.service.get_stock_detail(symbol, chart_type=chart_type))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        self._serve_static(path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        payload = self._read_json_body()

        if path == "/api/dashboard/refresh":
            self._send_json(self.service.refresh_ranking_cache())
            return

        if path == "/api/stage2/refresh":
            self._send_json(self.service.refresh_stage2_tracking())
            return

        if path == "/api/stage2/watchlist":
            self._send_json({"error": "手动加入监控已暂停"}, status=HTTPStatus.FORBIDDEN)
            return

        if path == "/api/preclose/run":
            result = self.service.run_preclose()
            self._send_json(result)
            return

        if path == "/api/continuation/refresh":
            result = self.service.refresh_continuation()
            self._send_json(result)
            return

        if path == "/api/demand-support/refresh":
            result = self.service.refresh_demand_support_ranking()
            self._send_json(result)
            return

        if path == "/api/backtest":
            symbols_str = str(payload.get("symbols", ""))
            target_date = str(payload.get("date", ""))
            reuse_scan = bool(payload.get("reuse_scan", False))
            force_refresh = bool(payload.get("force_refresh", False))
            from winstan.dashboard.backtest_handler import get_scan_status
            # 轮询模式：?job_id=xxx
            job_id = parse_qs(parsed.query).get("job_id", [""])[0]
            if job_id:
                result = get_scan_status(job_id)
                self._send_json(self.service.annotate_backtest_scan_payload(result, target_date))
                return
            self._send_json(self.service.run_backtest_payload(
                symbols_str,
                target_date,
                reuse_scan=reuse_scan,
                force_refresh=force_refresh,
            ))
            return

        if path in {"/api/box-backtest", "/api/demand-backtest"}:
            symbols_str = str(payload.get("symbols", ""))
            target_date = str(payload.get("date", ""))
            reuse_scan = bool(payload.get("reuse_scan", False))
            force_refresh = bool(payload.get("force_refresh", False))
            from winstan.dashboard.box_backtest_handler import get_box_scan_status
            job_id = parse_qs(parsed.query).get("job_id", [""])[0]
            if job_id:
                result = get_box_scan_status(job_id)
                self._send_json(self.service.annotate_box_backtest_scan_payload(result, target_date))
                return
            self._send_json(self.service.run_box_backtest_payload(
                symbols_str,
                target_date,
                reuse_scan=reuse_scan,
                force_refresh=force_refresh,
            ))
            return

        if path == "/api/crash-rebound-backtest":
            symbols_str = str(payload.get("symbols", ""))
            target_date = str(payload.get("date", ""))
            reuse_scan = bool(payload.get("reuse_scan", False))
            force_refresh = bool(payload.get("force_refresh", False))
            from winstan.dashboard.crash_rebound_handler import get_crash_scan_status
            job_id = parse_qs(parsed.query).get("job_id", [""])[0]
            if job_id:
                result = get_crash_scan_status(job_id)
                self._send_json(self.service.annotate_crash_rebound_scan_payload(result, target_date))
                return
            self._send_json(self.service.run_crash_rebound_payload(
                symbols_str,
                target_date,
                reuse_scan=reuse_scan,
                force_refresh=force_refresh,
            ))
            return

        if path == "/api/price-monitors":
            try:
                symbol = str(payload.get("symbol", ""))
                target_price = float(payload.get("target_price") or 0)
                self._send_json(self.service.add_price_monitor(symbol, target_price))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        self._send_json({"error": "Unsupported endpoint"}, status=HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/stage2/watchlist/"):
            watch_id = unquote(path.removeprefix("/api/stage2/watchlist/").rstrip("/"))
            try:
                self._send_json(self.service.delete_watch_item(watch_id))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if path.startswith("/api/stage2/holdings/"):
            holding_id = unquote(path.removeprefix("/api/stage2/holdings/").rstrip("/"))
            try:
                self._send_json(self.service.delete_holding_item(holding_id))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if path.startswith("/api/price-monitors/"):
            item_id = unquote(path.removeprefix("/api/price-monitors/").rstrip("/"))
            try:
                self._send_json(self.service.delete_price_monitor(item_id))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        self._send_json({"error": "Unsupported endpoint"}, status=HTTPStatus.NOT_FOUND)

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

    def _read_json_body(self) -> dict[str, object]:
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}


def run_server(config_path: str | Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    service = DashboardService(config_path)

    # Pre-warm recommendation cache at startup
    try:
        print("[warm] Warming up recommendation cache...")
        payload = service.get_recommendations_payload()
        count = len(payload.get("recommendations", []))
        print(f"[warm] Recommendation cache loaded: {count} items")
    except Exception as e:
        print(f"[warm] Recommendation preload failed (will compute on first request): {e}")

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
