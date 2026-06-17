import json
from pathlib import Path

from dataactivator.core.events import (
    Event,
    JsonlEventSink,
    read_events,
)


def test_emit_appends_and_assigns_seq(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    with JsonlEventSink(log) as sink:
        a = sink.emit("poll", "vw-id7", endpoint="list", status=500)
        b = sink.emit("poll", "vw-id7", endpoint="list", status=200)
    assert (a.seq, b.seq) == (1, 2)
    assert a.ts and a.monotonic >= 0

    events = list(read_events(log))
    assert [e.type for e in events] == ["poll", "poll"]
    assert events[0].data == {"endpoint": "list", "status": 500}
    assert events[1].data["status"] == 200


def test_seq_continues_across_reopen(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    with JsonlEventSink(log) as sink:
        sink.emit("daemon_start", "acc")
        sink.emit("poll", "acc")
    with JsonlEventSink(log) as sink:
        e = sink.emit("poll", "acc")
    assert e.seq == 3
    assert [e.seq for e in read_events(log)] == [1, 2, 3]


def test_lines_are_valid_json_with_flat_fields(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    with JsonlEventSink(log) as sink:
        sink.emit("portal_response", "acc", endpoint="list", status=200,
                  latency_ms=42, portal_date="Wed, 10 Jun 2026 08:41:02 GMT")
    record = json.loads(log.read_text().strip())
    assert record["seq"] == 1
    assert record["account"] == "acc"
    assert record["type"] == "portal_response"
    assert record["endpoint"] == "list"
    assert record["portal_date"].endswith("GMT")


def test_read_skips_corrupt_lines(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    with JsonlEventSink(log) as sink:
        sink.emit("poll", "acc")
    with log.open("a") as fh:
        fh.write("{ not json\n\n")
    with JsonlEventSink(log) as sink:
        sink.emit("poll", "acc")
    seqs = [e.seq for e in read_events(log)]
    assert seqs == [1, 2]


def test_round_trip_event_record() -> None:
    e = Event(type="poll", account="acc", data={"x": 1}, seq=5,
              ts="2026-06-10T08:00:00+00:00", monotonic=1.5)
    record = json.loads(e.to_json_line())
    back = Event.from_record(record)
    assert back == e
