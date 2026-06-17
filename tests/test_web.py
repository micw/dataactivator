import io
import json
import zipfile
from pathlib import Path

from dataactivator.core.config import AppConfig, ProviderConfig, StorageConfig
from dataactivator.core.events import JsonlEventSink
from dataactivator.core.web import WebApp

VIN = "WVWZZZED7SE013721"


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        storage=StorageConfig(type="file", folder=tmp_path),
        providers=[ProviderConfig(name="vw-id7", type="volkswagen-data-act-portal")],
    )


def seed_events(tmp_path: Path) -> None:
    folder = tmp_path / "vw-id7"
    name = f"20260610090000_{VIN}.zip"
    with JsonlEventSink(folder / "events.jsonl") as sink:
        sink.emit("data_request", "vw-id7", vin=VIN, identifier="i",
                  start_date="2026-06-10T08:45:00Z")
        sink.emit("portal_response", "vw-id7", endpoint="list", status=200,
                  latency_ms=10, portal_date=None)
        # An offered-but-not-downloaded dataset: its filename embeds the VIN.
        sink.emit("datasets_offered", "vw-id7", vin=VIN, count=1, datasets=[
            {"name": name, "createdOn": "2026-06-10T09:01:00Z", "size": "100",
             "no_content": False}])


def test_index_lists_providers(tmp_path: Path) -> None:
    app = WebApp(make_config(tmp_path))
    status, ctype, body = app.respond("/")
    assert status == 200 and "text/html" in ctype
    assert b"vw-id7" in body


def test_html_report_has_no_vin(tmp_path: Path) -> None:
    seed_events(tmp_path)
    app = WebApp(make_config(tmp_path))
    status, ctype, body = app.respond("/vw-id7")
    assert status == 200 and "text/html" in ctype
    assert VIN.encode() not in body            # VIN must never appear publicly
    assert "Verfügbarkeit".encode() in body


def test_public_json_strips_vin_filenames(tmp_path: Path) -> None:
    seed_events(tmp_path)
    app = WebApp(make_config(tmp_path))
    status, ctype, body = app.respond("/vw-id7.json")
    assert status == 200 and "application/json" in ctype
    assert VIN.encode() not in body            # no missing-filename leak
    data = json.loads(body)
    assert "missing" not in data["zips"]
    assert data["zips"]["missing_count"] == 1  # count kept, names dropped


def test_unknown_account_is_404(tmp_path: Path) -> None:
    app = WebApp(make_config(tmp_path))
    status, _, _ = app.respond("/nope")
    assert status == 404
    status, _, _ = app.respond("/nope.json")
    assert status == 404


def test_no_path_traversal(tmp_path: Path) -> None:
    app = WebApp(make_config(tmp_path))
    status, _, _ = app.respond("/../../etc/passwd")
    assert status == 404


def test_report_cache_reuses(tmp_path: Path) -> None:
    seed_events(tmp_path)
    app = WebApp(make_config(tmp_path))
    first = app.report("vw-id7")
    assert app.report("vw-id7") is first  # cached within TTL
