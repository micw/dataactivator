"""Public statistics web server (stdlib only).

Serves the compliance metrics as a public, unauthenticated site:
``/`` lists the providers, ``/<account>`` is the HTML report and
``/<account>.json`` the machine-readable form. Everything here is
**aggregate only** — no VIN, no dataset filenames (those embed the VIN).

This runs on the public port. Health/metrics belong on a separate
management port, and the authenticated evcc data endpoint is a later
addition; neither lives here.
"""

from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import AppConfig
from .events import read_events
from .reporting import Report, build_report, render_html, to_dict

logger = logging.getLogger(__name__)

# Reports are recomputed from the (growing) event log at most this often.
CACHE_TTL_SECONDS = 30.0


class WebApp:
    """Routing and rendering, independent of the socket layer (testable)."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._cache: dict[str, tuple[float, Report]] = {}

    def report(self, name: str) -> Report:
        cached = self._cache.get(name)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
        folder = self.config.storage.folder.expanduser() / name
        report = build_report(name, read_events(folder / "events.jsonl"), folder)
        self._cache[name] = (time.monotonic(), report)
        return report

    def respond(self, path: str) -> tuple[int, str, bytes]:
        """Return (status, content_type, body) for a request path."""
        path = path.split("?", 1)[0]
        names = {p.name for p in self.config.providers}

        if path in ("", "/"):
            return self._ok("text/html; charset=utf-8", self._index())

        target = path.lstrip("/")
        as_json = target.endswith(".json")
        if as_json:
            target = target[: -len(".json")]
        if target in names:
            report = self.report(target)
            if as_json:
                body = json.dumps(_public_dict(report), indent=2, ensure_ascii=False)
                return self._ok("application/json; charset=utf-8", body)
            return self._ok("text/html; charset=utf-8", render_html(report))

        return (404, "text/plain; charset=utf-8", b"not found\n")

    def _index(self) -> str:
        import html

        rows = []
        for p in self.config.providers:
            r = self.report(p.name)
            rows.append(
                f"<tr><td><a href='{html.escape(p.name)}'>{html.escape(p.name)}</a></td>"
                f"<td>{r.overall_availability_pct:.1f} %</td>"
                f"<td>{r.series.completeness_pct:.1f} %</td></tr>"
            )
        return (
            "<!doctype html><html lang=de><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width, initial-scale=1'>"
            "<title>dataACTivator — Übersicht</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:48rem;"
            "margin:2rem auto;padding:0 1rem}table{border-collapse:collapse;"
            "width:100%}th,td{text-align:left;padding:.4rem .6rem;"
            "border-bottom:1px solid #eee}a{color:#0969da}</style></head><body>"
            "<h1>dataACTivator</h1><p>EU-Data-Act-Compliance je Fahrzeug-Account.</p>"
            "<table><tr><th>Account</th><th>Portal-Verfügbarkeit</th>"
            "<th>Vollständigkeit</th></tr>" + "".join(rows) + "</table></body></html>"
        )

    @staticmethod
    def _ok(content_type: str, body: str) -> tuple[int, str, bytes]:
        return (200, content_type, body.encode("utf-8"))


def _public_dict(report: Report) -> dict:
    """Machine form with VIN-bearing dataset filenames stripped."""
    data = to_dict(report)
    zips = data.get("zips", {})
    zips["missing_count"] = len(zips.pop("missing", []))
    return data


def make_server(config: AppConfig) -> ThreadingHTTPServer:
    app = WebApp(config)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            status, content_type, body = app.respond(self.path)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            logger.debug("web %s - " + fmt, self.address_string(), *args)

    server = ThreadingHTTPServer((config.web.host, config.web.port), Handler)
    server.daemon_threads = True
    return server
