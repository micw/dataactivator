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
