"""Append-only observation log.

The ``watch`` daemon records raw observations here; ``report`` derives
all metrics from them afterwards. The log is the evidence — plain
append-only JSONL, one line per observation, human-readable. No hash
chaining: this is a recording, not a tamper-proof ledger.

``EventSink`` is the storage abstraction so a SQLite (or other) backend
can be added later without touching the producers.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    """One observation. ``seq`` and ``ts`` are filled in by the sink."""

    type: str
    account: str
    data: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    ts: str = ""
    monotonic: float = 0.0

    def to_json_line(self) -> str:
        # Flatten: common fields at top level, the rest spread in.
        record = {
            "seq": self.seq,
            "ts": self.ts,
            "monotonic": self.monotonic,
            "account": self.account,
            "type": self.type,
            **self.data,
        }
        return json.dumps(record, ensure_ascii=False, sort_keys=False)

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Event:
        known = {"seq", "ts", "monotonic", "account", "type"}
        return cls(
            type=record["type"],
            account=record.get("account", ""),
            data={k: v for k, v in record.items() if k not in known},
            seq=record.get("seq", 0),
            ts=record.get("ts", ""),
            monotonic=record.get("monotonic", 0.0),
        )


class EventSink(Protocol):
    def emit(self, type: str, account: str, **data: Any) -> Event: ...

    def close(self) -> None: ...


class JsonlEventSink:
    """Appends events as JSON lines to a single file, fsync per write.

    ``seq`` continues from the highest sequence already in the file, so
    restarts keep one monotonic sequence across daemon runs.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = _last_seq(self.path)
        self._fh = self.path.open("a", encoding="utf-8")

    def emit(self, type: str, account: str, **data: Any) -> Event:
        self._seq += 1
        event = Event(
            type=type,
            account=account,
            data=data,
            seq=self._seq,
            ts=_utc_now_iso(),
            monotonic=round(time.monotonic(), 6),
        )
        self._fh.write(event.to_json_line() + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        return event

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> JsonlEventSink:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_events(path: Path) -> Iterator[Event]:
    """Yield every event from a JSONL log, skipping blank/corrupt lines."""
    path = path.expanduser()
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            yield Event.from_record(record)


def _last_seq(path: Path) -> int:
    last = 0
    for event in read_events(path):
        if event.seq > last:
            last = event.seq
    return last
