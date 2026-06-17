import io
import json
import zipfile
from pathlib import Path

import httpx

from dataactivator.core.events import JsonlEventSink, read_events
from dataactivator.providers.vw.client import VwPortalClient, VwSettings
from dataactivator.providers.vw.poll import make_observer, poll_cycle

VIN = "WVWZZZED7SE013721"
IDENT = "abc123ident"


def make_zip() -> bytes:
    payload = {"vin": VIN, "user_id": "u1",
               "Data": [{"key": "k1", "dataFieldName": "mileage.value", "value": "123"}]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{VIN}_20260610090000.json", json.dumps(payload))
    return buf.getvalue()


def build_client(observer) -> VwPortalClient:
    """A client whose HTTP layer is a stateful mock; list 500s once."""
    state = {"list_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/vehicles"):
            return httpx.Response(200, json=[{"vin": VIN, "nickName": "Test"}])
        if path.endswith("/metadata/partial"):
            return httpx.Response(200, json={"Identifier": IDENT,
                                             "StartDate": "2026-06-10T08:45:00Z"})
        if path.endswith("/list"):
            state["list_calls"] += 1
            if state["list_calls"] == 1:
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json=[
                {"name": f"20260610090000_{VIN}.zip", "createdOn": "2026-06-10T09:01:00Z", "size": "100"},
                {"name": f"20260610084500_{VIN}_no_content_found.zip",
                 "createdOn": "2026-06-10T08:46:00Z", "size": "20"},
            ])
        if path.endswith("/download"):
            return httpx.Response(200, content=make_zip())
        return httpx.Response(404)

    settings = VwSettings(email="a@b.c", password="x", retry_attempts=3, retry_delay=0)
    client = VwPortalClient(settings, observer=observer)
    client._http = httpx.Client(transport=httpx.MockTransport(handler),
                                follow_redirects=True)
    return client


def test_poll_cycle_downloads_and_records(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    data = tmp_path / "data"
    with JsonlEventSink(log) as sink:
        client = build_client(make_observer(sink, "acc"))
        result = poll_cycle(client, "acc", data, sink)

    assert (result.downloaded, result.no_content, result.failed) == (1, 1, 0)

    # the real zip and the no-content marker both exist locally
    vin_dir = data / VIN
    assert (vin_dir / f"20260610090000_{VIN}.zip").stat().st_size > 0
    marker = vin_dir / f"20260610084500_{VIN}_no_content_found.zip"
    assert marker.exists() and marker.stat().st_size == 0

    events = list(read_events(log))
    types = [e.type for e in events]
    # list endpoint was hit twice (500 then 200) -> two portal_response events
    list_responses = [e for e in events
                      if e.type == "portal_response" and e.data["endpoint"] == "list"]
    assert [e.data["status"] for e in list_responses] == [500, 200]
    assert "datasets_offered" in types
    downloads = [e for e in events if e.type == "dataset_downloaded"]
    assert {e.data["no_content"] for e in downloads} == {True, False}
    real = next(e for e in downloads if not e.data["no_content"])
    assert real.data["bytes"] > 0 and len(real.data["sha256"]) == 64


def test_poll_cycle_dedups_existing(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    data = tmp_path / "data"
    with JsonlEventSink(log) as sink:
        poll_cycle(build_client(make_observer(sink, "acc")), "acc", data, sink)
        r2 = poll_cycle(build_client(make_observer(sink, "acc")), "acc", data, sink)
    assert r2.downloaded == 0 and r2.skipped == 2
