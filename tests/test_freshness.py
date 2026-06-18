import io
import json
import zipfile
from pathlib import Path

from dataactivator.core.reporting import build_report
from dataactivator.providers.vw.datasets import latest_state, newest_capture

VIN = "WVWZZZED7SE013721"


def write_dataset(vin_dir: Path, publish: str, points: list[dict]) -> None:
    vin_dir.mkdir(parents=True, exist_ok=True)
    payload = {"vin": VIN, "user_id": "u1", "Data": points}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{VIN}_{publish}.json", json.dumps(payload))
    (vin_dir / f"{publish}_{VIN}.zip").write_bytes(buf.getvalue())


def pt(name: str, value) -> dict:
    return {"key": name + str(value), "dataFieldName": name, "value": value}


def test_latest_state_filters_envelope_and_keeps_telemetry(tmp_path: Path) -> None:
    vin_dir = tmp_path / VIN
    write_dataset(vin_dir, "20260617100000", [
        pt("report_type", "REPORT_TYPE_CONSUMPTION_VALUES"),   # envelope -> dropped
        pt("timestamp", "2026-06-17T08:00:00Z"),               # envelope -> dropped
        pt("car_captured_time", "2026-06-17T09:00:00Z"),       # used for capture, dropped
        pt("battery_state_report.soc", 67),                    # telemetry -> kept
        pt("locked", True),                                    # bare but telemetry -> kept
    ])
    publish, captured, fields = latest_state(vin_dir)
    assert "report_type" not in fields and "timestamp" not in fields
    assert "car_captured_time" not in fields
    assert fields["battery_state_report.soc"] == 67
    assert fields["locked"] is True
    assert captured.isoformat() == "2026-06-17T09:00:00+00:00"


def test_newest_capture_picks_max() -> None:
    points = [
        pt("car_captured_time", "2026-06-17T07:00:00Z"),
        pt("car_captured_utc_timestamp", "2026-06-17T09:30:00Z"),
        pt("car_captured_time", "2026-06-17T08:00:00Z"),
    ]
    assert newest_capture(points).isoformat() == "2026-06-17T09:30:00+00:00"


def test_freshness_lag_and_frozen_span(tmp_path: Path) -> None:
    vin_dir = tmp_path / VIN
    # Capture stuck at 09:00 across 09:30..10:30 (frozen), then advances 11:00.
    for publish, captured in [
        ("20260617093000", "2026-06-17T09:00:00Z"),
        ("20260617100000", "2026-06-17T09:00:00Z"),
        ("20260617103000", "2026-06-17T09:00:00Z"),
        ("20260617110000", "2026-06-17T10:00:00Z"),
    ]:
        write_dataset(vin_dir, publish, [
            pt("car_captured_time", captured), pt("battery_state_report.soc", 67)])

    r = build_report("acc", [], data_root=tmp_path)
    fr = r.freshness
    assert fr.datasets_total == 4
    # lags: 30, 60, 90, 60 min -> median 60 min
    assert round(fr.median_lag_seconds / 60) == 60
    assert round(fr.max_lag_seconds / 60) == 90
    # one frozen stretch 09:30 -> 10:30 = 60 min
    assert fr.frozen_spans == 1
    assert round(fr.longest_frozen_seconds / 60) == 60


def test_current_pct(tmp_path: Path) -> None:
    vin_dir = tmp_path / VIN
    # Two datasets ≤30 min old, two older -> 50% current.
    for publish, captured in [
        ("20260617100000", "2026-06-17T09:50:00Z"),   # 10 min -> current
        ("20260617101500", "2026-06-17T10:10:00Z"),   # 5 min  -> current
        ("20260617103000", "2026-06-17T08:00:00Z"),   # 150 min -> stale
        ("20260617104500", "2026-06-17T07:00:00Z"),   # 225 min -> stale
    ]:
        write_dataset(vin_dir, publish, [pt("car_captured_time", captured)])
    fr = build_report("acc", [], data_root=tmp_path).freshness
    assert fr.current_pct == 50.0
