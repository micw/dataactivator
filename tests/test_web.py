import base64
import io
import json
import zipfile
from pathlib import Path

from dataactivator.core.config import AppConfig, ProviderConfig, StorageConfig, WebConfig
from dataactivator.core.events import JsonlEventSink
from dataactivator.core.web import WebApp

VIN = "WVWZZZED7SE013721"


def make_config(tmp_path: Path, data_password: str = "") -> AppConfig:
    return AppConfig(
        storage=StorageConfig(type="file", folder=tmp_path),
        web=WebConfig(data_password=data_password),
        providers=[ProviderConfig(name="vw-id7", type="volkswagen-data-act-portal")],
    )


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def seed_dataset(tmp_path: Path) -> None:
    vin_dir = tmp_path / "vw-id7" / VIN
    vin_dir.mkdir(parents=True)
    payload = {"vin": VIN, "user_id": "u1", "Data": [
        {"key": "k1", "dataFieldName": "charging_state_report.current_charge_state",
         "value": "CHARGE_STATE_CHARGING_HV_BATTERY"}]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{VIN}_20260617203820.json", json.dumps(payload))
    (vin_dir / f"20260617203820_{VIN}.zip").write_bytes(buf.getvalue())


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
    status, ctype, body, _ = app.respond("/")
    assert status == 200 and "text/html" in ctype
    assert b"vw-id7" in body


def test_html_report_has_no_vin(tmp_path: Path) -> None:
    seed_events(tmp_path)
    app = WebApp(make_config(tmp_path))
    status, ctype, body, _ = app.respond("/vw-id7")
    assert status == 200 and "text/html" in ctype
    assert VIN.encode() not in body            # VIN must never appear publicly
    assert "Verfügbarkeit".encode() in body


def test_public_json_strips_vin_filenames(tmp_path: Path) -> None:
    seed_events(tmp_path)
    app = WebApp(make_config(tmp_path))
    status, ctype, body, _ = app.respond("/vw-id7.json")
    assert status == 200 and "application/json" in ctype
    assert VIN.encode() not in body            # no missing-filename leak
    data = json.loads(body)
    assert "missing" not in data["zips"]
    assert data["zips"]["missing_count"] == 1  # count kept, names dropped


def test_unknown_account_is_404(tmp_path: Path) -> None:
    app = WebApp(make_config(tmp_path))
    status, _, _, _ = app.respond("/nope")
    assert status == 404
    status, _, _, _ = app.respond("/nope.json")
    assert status == 404


def test_no_path_traversal(tmp_path: Path) -> None:
    app = WebApp(make_config(tmp_path))
    status, _, _, _ = app.respond("/../../etc/passwd")
    assert status == 404


def test_report_cache_reuses(tmp_path: Path) -> None:
    seed_events(tmp_path)
    app = WebApp(make_config(tmp_path))
    first = app.report("vw-id7")
    assert app.report("vw-id7") is first  # cached within TTL


def test_data_disabled_without_password(tmp_path: Path) -> None:
    app = WebApp(make_config(tmp_path))  # no data_password
    status, _, _, _ = app.respond("/data")
    assert status == 404  # endpoint inactive, even with correct auth attempts
    status, _, _, _ = app.respond("/data", basic("admin", "x"))
    assert status == 404


def test_data_requires_auth(tmp_path: Path) -> None:
    app = WebApp(make_config(tmp_path, data_password="s3cret"))
    status, _, _, headers = app.respond("/data")
    assert status == 401
    assert headers["WWW-Authenticate"].startswith("Basic")
    status, _, _, _ = app.respond("/data", basic("admin", "wrong"))
    assert status == 401


def test_data_index_with_auth_shows_vin(tmp_path: Path) -> None:
    seed_dataset(tmp_path)
    app = WebApp(make_config(tmp_path, data_password="s3cret"))
    status, ctype, body, _ = app.respond("/data", basic("admin", "s3cret"))
    assert status == 200 and "text/html" in ctype
    assert VIN.encode() in body  # VIN is allowed on the authed internal page


def test_data_vehicle_page_shows_values(tmp_path: Path) -> None:
    seed_dataset(tmp_path)
    app = WebApp(make_config(tmp_path, data_password="s3cret"))
    status, _, body, _ = app.respond(f"/data/vw-id7/{VIN}", basic("admin", "s3cret"))
    assert status == 200
    assert b"current_charge_state" in body
    assert b"CHARGE_STATE_CHARGING_HV_BATTERY" in body


def test_index_links_to_data_only_when_enabled(tmp_path: Path) -> None:
    off = WebApp(make_config(tmp_path))
    _, _, body, _ = off.respond("/")
    assert b"Fahrzeugdaten" not in body
    on = WebApp(make_config(tmp_path, data_password="s3cret"))
    _, _, body, _ = on.respond("/")
    assert b"Fahrzeugdaten" in body
