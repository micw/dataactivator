from datetime import datetime, timedelta, timezone

from dataactivator.core.events import Event
from dataactivator.core.reporting import (
    DATA_MISSING,
    NOT_OBSERVED,
    PORTAL_OUTAGE,
    build_report,
    render_markdown,
    to_dict,
)

VIN = "WVWZZZED7SE013721"
T0 = datetime(2026, 6, 10, 6, 0, 0, tzinfo=timezone.utc)


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def ds_name(minutes: float, no_content: bool = False) -> str:
    ts = (T0 + timedelta(minutes=minutes)).strftime("%Y%m%d%H%M%S")
    suffix = "_no_content_found.zip" if no_content else ".zip"
    return f"{ts}_{VIN}{suffix}"


def ev(minutes: float, type: str, **data) -> Event:
    return Event(type=type, account="acc", data=data, ts=at(minutes))


def offered(minutes: float, names_nc: list[tuple[float, bool]]) -> Event:
    return ev(minutes, "datasets_offered", vin=VIN, datasets=[
        {"name": ds_name(m, nc), "createdOn": at(m + 1), "size": "100", "no_content": nc}
        for m, nc in names_nc
    ])


def test_availability_and_outage() -> None:
    events = [
        ev(0, "portal_response", endpoint="list", status=200),
        ev(1, "portal_response", endpoint="list", status=500),
        ev(2, "portal_response", endpoint="list", status=500),
        ev(3, "portal_response", endpoint="list", status=200),
        ev(4, "portal_response", endpoint="list", status=None, error="timeout"),
    ]
    r = build_report("acc", events)
    lst = r.endpoints["list"]
    assert lst.attempts == 5
    assert lst.ok == 2
    assert lst.server_error == 2
    assert lst.network_error == 1
    # one contiguous 5xx outage from min 1 to min 2
    assert len(r.outages) == 1
    assert round(r.outages[0].minutes) == 1


def test_zip_availability_counts_no_content_as_available() -> None:
    events = [
        offered(5, [(0, False), (15, True)]),
        ev(5, "dataset_downloaded", name=ds_name(0), sha256="a" * 64, bytes=10, no_content=False),
        ev(5, "dataset_downloaded", name=ds_name(15, True), sha256=None, bytes=0, no_content=True),
    ]
    r = build_report("acc", events)
    assert r.zips.offered == 2
    assert r.zips.retrieved == 1
    assert r.zips.no_content == 1
    assert r.zips.missing == []
    assert r.zips.availability_pct == 100.0


def test_missing_zip_listed() -> None:
    events = [offered(5, [(0, False)])]  # offered but never downloaded
    r = build_report("acc", events)
    assert r.zips.retrieved == 0
    assert r.zips.missing == [ds_name(0)]


def test_zip_present_locally_counts_as_retrieved(tmp_path) -> None:
    # Offered, no download event (e.g. fetched before logging existed),
    # but the file is on disk -> counts as retrieved, not missing.
    events = [offered(5, [(0, False)])]
    vin_dir = tmp_path / VIN
    vin_dir.mkdir()
    (vin_dir / ds_name(0)).write_bytes(b"zipdata")
    r = build_report("acc", events, data_root=tmp_path)
    assert r.zips.retrieved == 1
    assert r.zips.missing == []


def test_gap_attributed_data_missing_when_portal_up() -> None:
    # values at min 0 and 45 (a 45-min gap = ~2 missing); portal answered
    # 200 during the gap -> DATA_MISSING.
    events = [
        ev(0, "data_request", vin=VIN, identifier="i", start_date=at(0), frequency="15mins"),
        offered(0, [(0, False)]),
        ev(20, "portal_response", endpoint="list", status=200),
        offered(45, [(45, False)]),
    ]
    r = build_report("acc", events)
    assert len(r.series.gaps) == 1
    assert r.series.gaps[0].cause == DATA_MISSING
    assert r.series.gaps[0].estimated_missing == 2


def test_gap_attributed_portal_outage() -> None:
    events = [
        offered(0, [(0, False)]),
        ev(20, "portal_response", endpoint="list", status=500),
        ev(30, "portal_response", endpoint="list", status=500),
        offered(45, [(45, False)]),
    ]
    r = build_report("acc", events)
    assert r.series.gaps[0].cause == PORTAL_OUTAGE


def test_completeness_is_gap_based_not_grid() -> None:
    # Two delivered values 30 min apart (one missing between) and portal up.
    # Expected = covered(2) + missing(1) = 3 -> 66.7%, regardless of the
    # absolute span / rigid grid.
    events = [
        ev(0, "data_request", vin=VIN, identifier="i", start_date=at(0)),
        offered(0, [(0, False)]),
        ev(15, "portal_response", endpoint="list", status=200),
        offered(30, [(30, False)]),
    ]
    r = build_report("acc", events)
    assert r.series.delivered == 2
    assert r.series.expected == 3
    assert round(r.series.completeness_pct, 1) == 66.7


def test_trailing_gap_detected() -> None:
    # Last value at min 0, but observation continues (portal up) to min 30
    # with nothing new -> a trailing gap of ~1 missing value.
    events = [
        offered(0, [(0, False)]),
        ev(15, "portal_response", endpoint="list", status=200),
        ev(30, "portal_response", endpoint="list", status=200),
    ]
    r = build_report("acc", events)
    assert len(r.series.gaps) == 1
    assert r.series.gaps[0].cause == DATA_MISSING
    assert r.series.gaps[0].estimated_missing == 1


def test_gap_attributed_not_observed_when_no_events() -> None:
    events = [
        offered(0, [(0, False)]),
        offered(45, [(45, False)]),  # nothing recorded between -> NOT_OBSERVED
    ]
    r = build_report("acc", events)
    assert r.series.gaps[0].cause == NOT_OBSERVED


def test_delivery_delay_end_to_end_and_publish_lag() -> None:
    # Data moment at min 0; VW publishes (createdOn) at min 1; we download
    # it at min 6. End-to-end = 6 min, publish lag = 1 min (60s).
    name = ds_name(0)
    events = [
        ev(0, "data_request", vin=VIN, identifier="i", start_date=at(0)),
        ev(5, "datasets_offered", vin=VIN, datasets=[
            {"name": name, "createdOn": at(1), "size": "100", "no_content": False}]),
        ev(6, "dataset_downloaded", name=name, sha256="a" * 64, bytes=10, no_content=False),
    ]
    r = build_report("acc", events)
    assert len(r.series.delays) == 1
    d = r.series.delays[0]
    assert d.end_to_end == 6 * 60
    assert d.publish_lag == 60
    assert d.outage_affected is False
    assert r.series.median_delay_seconds == 6 * 60


def test_delivery_delay_flags_portal_outage() -> None:
    # File published at min 1 but a 5xx occurs before we finally download at min 9.
    name = ds_name(0)
    events = [
        ev(0, "data_request", vin=VIN, identifier="i", start_date=at(0)),
        ev(2, "datasets_offered", vin=VIN, datasets=[
            {"name": name, "createdOn": at(1), "size": "100", "no_content": False}]),
        ev(4, "portal_response", endpoint="download", status=500),
        ev(9, "dataset_downloaded", name=name, sha256="a" * 64, bytes=10, no_content=False),
    ]
    r = build_report("acc", events)
    assert r.series.delays[0].outage_affected is True
    assert r.series.outage_delayed_count == 1


def test_delivery_delay_skips_backlog_before_observation() -> None:
    # Data moment well before observation start -> not measured (bogus delay).
    name = ds_name(-120)  # 2h before T0
    events = [
        ev(0, "datasets_offered", vin=VIN, datasets=[
            {"name": name, "createdOn": at(-119), "size": "100", "no_content": False}]),
        ev(0, "dataset_downloaded", name=name, sha256="a" * 64, bytes=10, no_content=False),
    ]
    r = build_report("acc", events)
    assert r.series.delays == []


def test_render_markdown_and_json_smoke() -> None:
    events = [
        ev(0, "data_request", vin=VIN, identifier="i", start_date=at(0), frequency="15mins"),
        offered(0, [(0, False)]),
        ev(0, "portal_response", endpoint="list", status=200),
    ]
    r = build_report("acc", events)
    md = render_markdown(r)
    assert "Data-Act-Bericht" in md and "Portal-Verfügbarkeit" in md
    d = to_dict(r)
    assert d["account"] == "acc"
    assert "completeness_pct" in d["series"]
