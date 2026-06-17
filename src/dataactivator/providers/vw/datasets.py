"""Reading and integrity-checking of locally stored VW datasets.

A dataset is a ZIP named ``YYYYMMDDHHMMSS_<vin>.zip`` (UTC timestamp)
holding a single JSON with ``vin``, ``user_id`` and a ``Data`` list of
``{key, dataFieldName, value}`` points. VW reports a field only when it
has a fresh value for the 15-minute window, so the point count and the
set of present fields vary per dataset by design — a missing *dataset*
(a time gap) is the real signal of incompleteness, not a missing field.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import const

# Nominal cadence of the continuous data request.
NOMINAL_INTERVAL = timedelta(minutes=15)
# An interval longer than this implies at least one dataset is missing.
GAP_THRESHOLD = timedelta(minutes=25)


@dataclass
class DatasetFile:
    path: Path
    name: str
    timestamp: datetime
    ok: bool
    error: str | None = None
    vin: str | None = None
    point_count: int = 0
    fields: frozenset[str] = frozenset()
    # True for a ``_no_content_found.zip``: the portal acknowledged the
    # window but delivered no data — a file exists, but it is empty.
    no_content: bool = False


@dataclass
class Gap:
    after: datetime
    before: datetime
    minutes: float
    estimated_missing: int


@dataclass
class CheckReport:
    files: list[DatasetFile] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)

    @property
    def valid(self) -> list[DatasetFile]:
        return [f for f in self.files if f.ok]

    @property
    def corrupt(self) -> list[DatasetFile]:
        return [f for f in self.files if not f.ok]

    @property
    def complete(self) -> bool:
        return not self.corrupt and not self.gaps


def parse_timestamp(name: str) -> datetime:
    """UTC timestamp encoded in the dataset filename."""
    stamp = name.split("_", 1)[0]
    return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def load_latest(vin_dir: Path) -> tuple[datetime, list[dict]] | None:
    """Newest dataset's (timestamp, data points) for a vehicle, or None.

    Data points are ``{key, dataFieldName, value}`` dicts. Empty-window
    markers are skipped.
    """
    zips = sorted(
        p for p in vin_dir.glob("*.zip")
        if not p.name.endswith(const.NO_CONTENT_SUFFIX)
    )
    if not zips:
        return None
    path = zips[-1]
    try:
        timestamp = parse_timestamp(path.name)
        with zipfile.ZipFile(path) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".json")]
            if not members:
                return None
            payload = json.loads(zf.read(members[0]))
    except (ValueError, OSError, zipfile.BadZipFile):
        return None
    points = [p for p in payload.get("Data", []) if isinstance(p, dict)]
    return timestamp, points


# Per-report housekeeping fields that repeat once per bundled report; they
# are not vehicle telemetry and are filtered from the data view. Only bare
# (un-dotted) names — qualified fields like ``mileage.state`` are kept.
ENVELOPE_FIELDS = frozenset({
    "report_type", "message_id", "state", "result_app", "result_master",
    "update_reason", "car_captured_time", "car_captured_utc_timestamp",
    "timestamp", "instrument_cluster_time",
    "error_code", "error_description", "error_number", "value",
})


def _parse_iso(value: object) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def newest_capture(points: list[dict]) -> datetime | None:
    """The freshest in-vehicle capture time across a dataset's reports.

    This — not the filename/publish time — is when the data was actually
    measured in the car. VW re-publishes the same snapshot every 15 min
    while the car is offline, so this is the real freshness signal.
    """
    best: datetime | None = None
    for p in points:
        if p.get("dataFieldName") in ("car_captured_time", "car_captured_utc_timestamp"):
            t = _parse_iso(p.get("value"))
            if t and (best is None or t > best):
                best = t
    return best


def latest_state(vin_dir: Path) -> tuple[datetime, datetime | None, dict[str, object]] | None:
    """(publish_time, captured_time, fields) for the newest dataset.

    ``fields`` is the telemetry with report-envelope fields removed and
    duplicate field names collapsed to their last (newest) occurrence.
    """
    latest = load_latest(vin_dir)
    if latest is None:
        return None
    publish_ts, points = latest
    captured = newest_capture(points)
    fields: dict[str, object] = {}
    for p in points:
        name = p.get("dataFieldName")
        if name and name not in ENVELOPE_FIELDS:
            fields[name] = p.get("value")  # later occurrence wins
    return publish_ts, captured, fields


def dataset_captures(vin_dir: Path) -> list[tuple[datetime, datetime | None]]:
    """(publish_time, newest_capture) for every content dataset, oldest first."""
    out: list[tuple[datetime, datetime | None]] = []
    for path in sorted(vin_dir.glob("*.zip")):
        if path.name.endswith(const.NO_CONTENT_SUFFIX):
            continue
        try:
            publish = parse_timestamp(path.name)
            with zipfile.ZipFile(path) as zf:
                members = [n for n in zf.namelist() if n.lower().endswith(".json")]
                if not members:
                    continue
                points = json.loads(zf.read(members[0])).get("Data", [])
        except (ValueError, OSError, zipfile.BadZipFile):
            continue
        out.append((publish, newest_capture(points)))
    return out


def read_dataset(path: Path) -> DatasetFile:
    """Open, validate and summarise one dataset ZIP."""
    name = path.name
    try:
        timestamp = parse_timestamp(name)
    except ValueError as exc:
        return DatasetFile(path, name, datetime.min.replace(tzinfo=timezone.utc),
                           ok=False, error=f"unparseable filename: {exc}")

    # A no-content window is a valid observation with zero data points.
    # The local file may be the portal's empty ZIP or just a marker, so
    # don't try to parse it — its existence is the information.
    if name.endswith(const.NO_CONTENT_SUFFIX):
        return DatasetFile(path, name, timestamp, ok=True, no_content=True)

    try:
        with zipfile.ZipFile(path) as zf:
            broken = zf.testzip()
            if broken is not None:
                return DatasetFile(path, name, timestamp, ok=False,
                                   error=f"CRC error in {broken}")
            members = [n for n in zf.namelist() if n.lower().endswith(".json")]
            if not members:
                return DatasetFile(path, name, timestamp, ok=False,
                                   error="no JSON member in archive")
            payload = json.loads(zf.read(members[0]))
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        return DatasetFile(path, name, timestamp, ok=False, error=str(exc))

    points = payload.get("Data", [])
    fields = frozenset(
        p["dataFieldName"] for p in points if isinstance(p, dict) and "dataFieldName" in p
    )
    return DatasetFile(
        path, name, timestamp, ok=True, vin=payload.get("vin"),
        point_count=len(points), fields=fields,
    )


def check_directory(folder: Path) -> CheckReport:
    """Integrity- and gap-check every dataset ZIP in a directory."""
    report = CheckReport()
    # Include no-content markers: they cover a window (so are not a time
    # gap) and the report distinguishes them as their own NO_CONTENT state.
    report.files = [read_dataset(p) for p in sorted(folder.glob("*.zip"))]

    ordered = sorted(report.valid, key=lambda f: f.timestamp)
    for earlier, later in zip(ordered, ordered[1:]):
        delta = later.timestamp - earlier.timestamp
        if delta > GAP_THRESHOLD:
            missing = max(round(delta / NOMINAL_INTERVAL) - 1, 1)
            report.gaps.append(
                Gap(earlier.timestamp, later.timestamp,
                    delta.total_seconds() / 60, missing)
            )
    return report
