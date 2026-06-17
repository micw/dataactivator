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

import base64
import hmac
import html
import json
import logging
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..providers.vw.datasets import latest_state
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

    def respond(
        self, path: str, auth_header: str | None = None
    ) -> tuple[int, str, bytes, dict[str, str]]:
        """Return (status, content_type, body, extra_headers) for a path."""
        path = path.split("?", 1)[0]
        names = {p.name for p in self.config.providers}

        if path in ("", "/"):
            return self._ok("text/html; charset=utf-8", self._index())

        # Internal vehicle-data pages — only active when a password is set,
        # then gated by HTTP Basic auth.
        if path == "/data" or path.startswith("/data/"):
            return self._data(path, auth_header)

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

        return self._not_found()

    # -- internal vehicle-data pages --------------------------------------

    def _data(self, path: str, auth_header: str | None) -> tuple[int, str, bytes, dict]:
        web = self.config.web
        if not web.data_enabled:
            return self._not_found()
        if not _check_basic(auth_header, web.data_username, web.data_password):
            return (401, "text/plain; charset=utf-8", b"authentication required\n",
                    {"WWW-Authenticate": 'Basic realm="dataactivator"'})

        names = {p.name for p in self.config.providers}
        sub = path[len("/data"):].strip("/")
        if sub == "":
            return self._ok("text/html; charset=utf-8", self._vehicle_index())
        parts = sub.split("/")
        if len(parts) == 2 and parts[0] in names:
            return self._ok("text/html; charset=utf-8",
                            self._vehicle_data(parts[0], parts[1]))
        return self._not_found()

    def _vehicle_index(self) -> str:
        rows = []
        for p in self.config.providers:
            folder = self.config.storage.folder.expanduser() / p.name
            for vin_dir in sorted(d for d in folder.glob("*") if d.is_dir()):
                state = latest_state(vin_dir)
                stand = _fmt(state[1]) + f" ({_age(state[1])})" if state and state[1] \
                    else "—"
                rows.append(
                    f"<tr><td><a href='data/{html.escape(p.name)}/"
                    f"{html.escape(vin_dir.name)}'>{html.escape(vin_dir.name)}</a></td>"
                    f"<td>{html.escape(p.name)}</td><td>{stand}</td></tr>"
                )
        body = "".join(rows) or "<tr><td colspan=3>noch keine Daten</td></tr>"
        return _page("Fahrzeugdaten",
                     "<h1>Fahrzeugdaten</h1>"
                     "<table><tr><th>VIN</th><th>Account</th>"
                     "<th>Stand (Fahrzeug-Erfassung)</th></tr>"
                     + body + "</table><p><a href='../'>&larr; Übersicht</a></p>")

    def _vehicle_data(self, account: str, vin: str) -> str:
        folder = self.config.storage.folder.expanduser() / account / vin
        state = latest_state(folder)
        if state is None:
            return _page("Fahrzeugdaten", f"<h1>{html.escape(vin)}</h1>"
                         "<p>keine Daten</p><p><a href='../../data'>&larr; zurück</a></p>")
        publish_ts, captured, fields = state
        rows = "".join(
            f"<tr><td>{html.escape(name)}</td><td>{html.escape(str(value))}</td></tr>"
            for name, value in sorted(fields.items())
        )
        # The capture time is the real freshness; the file/publish time only
        # says when VW re-packaged the (often stale) snapshot.
        if captured is not None:
            stand = (f"<p class='big'>Stand: {_fmt(captured)} "
                     f"<span class=age>({_age(captured)})</span></p>")
        else:
            stand = "<p class=sub>keine Erfassungszeit im Datensatz</p>"
        return _page(
            f"{vin}",
            f"<h1>{html.escape(vin)}</h1>" + stand +
            f"<p class=sub>Datei publiziert {_fmt(publish_ts)} · {len(fields)} Werte "
            "(Report-Metadaten ausgeblendet)</p>"
            "<table><tr><th>Feld</th><th>Wert</th></tr>" + rows + "</table>"
            "<p><a href='../../data'>&larr; zurück</a></p>")

    def _index(self) -> str:
        rows = []
        for p in self.config.providers:
            r = self.report(p.name)
            rows.append(
                f"<tr><td><a href='{html.escape(p.name)}'>{html.escape(p.name)}</a></td>"
                f"<td>{r.overall_availability_pct:.1f} %</td>"
                f"<td>{r.series.completeness_pct:.1f} %</td></tr>"
            )
        data_link = ("<p><a href='data'>→ Fahrzeugdaten (intern, geschützt)</a></p>"
                     if self.config.web.data_enabled else "")
        return _page(
            "Übersicht",
            "<h1>dataACTivator</h1>"
            "<p>EU-Data-Act-Compliance je Fahrzeug-Account.</p>"
            "<table><tr><th>Account</th><th>Portal-Verfügbarkeit</th>"
            "<th>Vollständigkeit</th></tr>" + "".join(rows) + "</table>" + data_link)

    @staticmethod
    def _ok(content_type: str, body: str) -> tuple[int, str, bytes, dict]:
        return (200, content_type, body.encode("utf-8"), {})

    @staticmethod
    def _not_found() -> tuple[int, str, bytes, dict]:
        return (404, "text/plain; charset=utf-8", b"not found\n", {})


def _page(title: str, inner: str) -> str:
    return (
        "<!doctype html><html lang=de><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        f"<title>dataACTivator — {html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:52rem;"
        "margin:2rem auto;padding:0 1rem}table{border-collapse:collapse;"
        "width:100%}th,td{text-align:left;padding:.35rem .6rem;"
        "border-bottom:1px solid #eee}a{color:#0969da}.sub{color:#666}"
        ".big{font-size:1.2rem;font-weight:600}.age{color:#bf8700;font-weight:400}"
        "</style></head><body>" + inner + "</body></html>"
    )


def _fmt(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _age(dt: datetime) -> str:
    minutes = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    if minutes < 90:
        return f"vor {minutes:.0f} min"
    hours = minutes / 60
    if hours < 36:
        return f"vor {hours:.1f} h"
    return f"vor {hours / 24:.1f} Tagen"


def _check_basic(auth_header: str | None, user: str, password: str) -> bool:
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    got_user, _, got_pass = decoded.partition(":")
    # Constant-time compare on both fields.
    return (hmac.compare_digest(got_user, user)
            and hmac.compare_digest(got_pass, password))


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
            status, content_type, body, extra = app.respond(
                self.path, self.headers.get("Authorization"))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            logger.debug("web %s - " + fmt, self.address_string(), *args)

    server = ThreadingHTTPServer((config.web.host, config.web.port), Handler)
    server.daemon_threads = True
    return server
