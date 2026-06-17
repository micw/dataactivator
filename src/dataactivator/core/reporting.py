"""Derive Data-Act compliance metrics from the observation log.

Everything here is computed *after the fact* from the append-only event
log — the daemon only records, the report interprets. Three metric
families, plus the crucial attribution of every missing value to a
cause (VW vs. our side vs. not observed).

Parsing of dataset filename timestamps is reused from the VW provider
(`providers.vw.datasets.parse_timestamp`).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from ..providers.vw.const import NO_CONTENT_SUFFIX
from ..providers.vw.datasets import parse_timestamp
from .events import Event

NOMINAL_INTERVAL = timedelta(minutes=15)
# A gap longer than this implies at least one missing value.
GAP_FACTOR = 1.5

# Slot/gap attribution causes.
COMPLETE = "COMPLETE"
NO_CONTENT = "NO_CONTENT"
DATA_MISSING = "DATA_MISSING"        # portal up, no value → VW (hard)
PORTAL_OUTAGE = "PORTAL_OUTAGE"      # portal down → VW (soft)
NOT_OBSERVED = "NOT_OBSERVED"        # daemon/our side down → not attributable


def _parse_ts(iso: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class EndpointAvailability:
    endpoint: str
    attempts: int = 0
    ok: int = 0
    server_error: int = 0
    network_error: int = 0

    @property
    def availability_pct(self) -> float:
        return 100.0 * self.ok / self.attempts if self.attempts else 0.0


@dataclass
class Outage:
    endpoint: str
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass
class ZipAvailability:
    offered: int = 0
    retrieved: int = 0          # downloaded with content + sha256
    no_content: int = 0         # window acknowledged, empty
    missing: list[str] = field(default_factory=list)  # offered, never stored

    @property
    def availability_pct(self) -> float:
        if not self.offered:
            return 0.0
        return 100.0 * (self.retrieved + self.no_content) / self.offered


@dataclass
class Gap:
    after: datetime
    before: datetime
    estimated_missing: int
    cause: str

    @property
    def minutes(self) -> float:
        return (self.before - self.after).total_seconds() / 60


@dataclass
class DeliveryDelay:
    """How late one 15-min value reached us, decomposed by cause.

    All spans are seconds from the dataset's filename timestamp (the data
    moment). ``end_to_end`` is what actually matters: data moment → file
    in hand. ``publish_lag`` is VW's "file not there yet" part;
    ``outage_affected`` flags values whose retrieval was delayed by a
    portal HTTP 500 (the "portal down" part).
    """

    name: str
    publish_lag: float        # createdOn - filename_ts (VW publishing)
    end_to_end: float         # downloaded_at - filename_ts (data moment -> at me)
    outage_affected: bool


@dataclass
class SeriesCompleteness:
    start: datetime | None = None
    end: datetime | None = None
    expected: int = 0
    delivered: int = 0          # distinct windows with content
    no_content: int = 0
    gaps: list[Gap] = field(default_factory=list)
    delays: list[DeliveryDelay] = field(default_factory=list)

    @property
    def completeness_pct(self) -> float:
        if not self.expected:
            return 0.0
        return 100.0 * (self.delivered + self.no_content) / self.expected

    def _e2e(self) -> list[float]:
        return [d.end_to_end for d in self.delays]

    @property
    def median_delay_seconds(self) -> float | None:
        vals = self._e2e()
        return statistics.median(vals) if vals else None

    @property
    def max_delay_seconds(self) -> float | None:
        vals = self._e2e()
        return max(vals) if vals else None

    @property
    def median_publish_lag_seconds(self) -> float | None:
        vals = [d.publish_lag for d in self.delays]
        return statistics.median(vals) if vals else None

    @property
    def outage_delayed_count(self) -> int:
        return sum(1 for d in self.delays if d.outage_affected)


@dataclass
class Report:
    account: str
    observation_start: datetime | None
    observation_end: datetime | None
    endpoints: dict[str, EndpointAvailability]
    outages: list[Outage]
    zips: ZipAvailability
    series: SeriesCompleteness

    @property
    def overall_availability_pct(self) -> float:
        att = sum(e.attempts for e in self.endpoints.values())
        ok = sum(e.ok for e in self.endpoints.values())
        return 100.0 * ok / att if att else 0.0


def build_report(
    account: str, events: Iterable[Event], data_root: Path | None = None
) -> Report:
    events = list(events)
    timestamps = [t for t in (_parse_ts(e.ts) for e in events) if t]
    obs_start = min(timestamps) if timestamps else None
    obs_end = max(timestamps) if timestamps else None

    endpoints, outages = _availability(events)
    zips = _zip_availability(events, data_root)
    # Delivery delay is only meaningful for windows that appeared *during*
    # observation; backlog already offered at the first poll would yield a
    # bogus delay measured from log start.
    series = _series_completeness(events, obs_start)

    return Report(
        account=account,
        observation_start=obs_start,
        observation_end=obs_end,
        endpoints=endpoints,
        outages=outages,
        zips=zips,
        series=series,
    )


def _availability(events: list[Event]) -> tuple[dict[str, EndpointAvailability], list[Outage]]:
    endpoints: dict[str, EndpointAvailability] = {}
    # Track contiguous failure runs per endpoint to derive outage windows.
    open_outage: dict[str, datetime] = {}
    last_fail: dict[str, datetime] = {}
    outages: list[Outage] = []

    for e in events:
        if e.type != "portal_response":
            continue
        endpoint = e.data.get("endpoint", "?")
        ts = _parse_ts(e.ts)
        ea = endpoints.setdefault(endpoint, EndpointAvailability(endpoint))
        ea.attempts += 1

        status = e.data.get("status")
        ok = isinstance(status, int) and 200 <= status < 400
        if ok:
            ea.ok += 1
        elif status is None:
            ea.network_error += 1
        elif status >= 500:
            ea.server_error += 1
        else:
            ea.ok += 1  # 4xx counts as "portal reachable" for availability

        if ts is None:
            continue
        # Outage windows are built only from unambiguous 5xx: a reachable
        # response (2xx/3xx/4xx) ends one, a 5xx opens/extends one. A
        # network error is ambiguous (could be our side) and stays neutral
        # — it is counted separately but never forms a portal-outage window.
        reachable = isinstance(status, int) and status < 500
        server_down = isinstance(status, int) and status >= 500
        if reachable:
            if endpoint in open_outage:
                outages.append(Outage(endpoint, open_outage.pop(endpoint),
                                      last_fail.get(endpoint, ts)))
        elif server_down:
            open_outage.setdefault(endpoint, ts)
            last_fail[endpoint] = ts

    for endpoint, start in open_outage.items():
        outages.append(Outage(endpoint, start, last_fail.get(endpoint, start)))
    outages.sort(key=lambda o: o.start)
    return endpoints, outages


def _zip_availability(events: list[Event], data_root: Path | None) -> ZipAvailability:
    offered: dict[str, tuple[bool, str | None]] = {}   # name -> (is_no_content, vin)
    retrieved: set[str] = set()
    no_content_got: set[str] = set()

    for e in events:
        if e.type == "datasets_offered":
            vin = e.data.get("vin")
            for ds in e.data.get("datasets", []):
                name = ds.get("name")
                if name:
                    is_nc = bool(ds.get("no_content")) or name.endswith(NO_CONTENT_SUFFIX)
                    offered[name] = (is_nc, vin)
        elif e.type == "dataset_downloaded":
            name = e.data.get("name")
            if not name:
                continue
            if e.data.get("no_content"):
                no_content_got.add(name)
            elif e.data.get("sha256"):
                retrieved.add(name)

    za = ZipAvailability(offered=len(offered))
    for name, (is_nc, vin) in offered.items():
        if name in retrieved or _present_locally(data_root, vin, name):
            za.retrieved += 1
        elif name in no_content_got or is_nc:
            za.no_content += 1
        else:
            za.missing.append(name)
    za.missing.sort()
    return za


def _present_locally(data_root: Path | None, vin: str | None, name: str) -> bool:
    """A dataset downloaded before the event log existed is still on disk."""
    if data_root is None or vin is None or name.endswith(NO_CONTENT_SUFFIX):
        return False
    path = data_root / vin / name
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _series_completeness(
    events: list[Event], obs_start: datetime | None = None
) -> SeriesCompleteness:
    series = SeriesCompleteness()

    start_date = None
    for e in events:
        if e.type == "data_request" and e.data.get("start_date"):
            start_date = _parse_ts(e.data["start_date"])
            if start_date:
                break

    # First time each dataset name was seen offered, and whether it has content.
    first_seen: dict[str, datetime] = {}
    is_no_content: dict[str, bool] = {}
    for e in events:
        if e.type != "datasets_offered":
            continue
        seen_at = _parse_ts(e.ts)
        for ds in e.data.get("datasets", []):
            name = ds.get("name")
            if not name:
                continue
            is_no_content[name] = bool(ds.get("no_content")) or name.endswith(NO_CONTENT_SUFFIX)
            if name not in first_seen and seen_at:
                first_seen[name] = seen_at

    # Measurement time per dataset from its filename.
    windows: list[tuple[datetime, str, datetime | None]] = []  # (measured, name, first_seen)
    for name in first_seen:
        try:
            measured = parse_timestamp(name)
        except ValueError:
            continue
        windows.append((measured, name, first_seen.get(name)))
    windows.sort()

    observation_end = max(
        (t for t in (_parse_ts(e.ts) for e in events) if t),
        default=None,
    )
    series.start = start_date or (windows[0][0] if windows else None)
    series.end = observation_end

    content_windows = [w for w in windows if not is_no_content[w[1]]]
    series.delivered = len(content_windows)
    series.no_content = sum(1 for w in windows if is_no_content[w[1]])

    series.delays = _delivery_delays(events, obs_start)
    series.gaps = _attribute_gaps(windows, events, observation_end)

    # Expected = what was actually covered plus the values the gap analysis
    # found missing. This is robust to VW's cadence jitter (a normal ~15-min
    # interval is never counted as a gap), unlike a rigid grid from StartDate.
    covered = series.delivered + series.no_content
    missing = sum(g.estimated_missing for g in series.gaps)
    series.expected = covered + missing
    return series


def _delivery_delays(
    events: list[Event], obs_start: datetime | None
) -> list[DeliveryDelay]:
    """End-to-end lateness per delivered value: data moment -> file in hand.

    Uses three clocks: the filename timestamp (the data moment), VW's
    ``createdOn`` (published), and our ``dataset_downloaded`` event ts
    (arrived at us). Only values whose data moment falls within the
    observation period are measured — backlog already published before we
    started watching would yield a meaningless delay.
    """
    created_on: dict[str, datetime] = {}
    for e in events:
        if e.type != "datasets_offered":
            continue
        for ds in e.data.get("datasets", []):
            name = ds.get("name")
            co = _parse_ts(ds.get("createdOn")) if name else None
            if name and co and name not in created_on:
                created_on[name] = co

    downloaded_at: dict[str, datetime] = {}
    for e in events:
        if e.type == "dataset_downloaded" and not e.data.get("no_content"):
            name = e.data.get("name")
            ts = _parse_ts(e.ts)
            if name and ts and name not in downloaded_at:
                downloaded_at[name] = ts

    delays: list[DeliveryDelay] = []
    for name, got_at in downloaded_at.items():
        try:
            measured = parse_timestamp(name)
        except ValueError:
            continue
        if obs_start is not None and measured < obs_start:
            continue
        published = created_on.get(name)
        publish_lag = (published - measured).total_seconds() if published else 0.0
        end_to_end = (got_at - measured).total_seconds()
        # "Portal down" component: a 5xx between publish and our retrieval
        # means the file existed but we could not fetch it on time.
        outage_affected = _outage_between(published or measured, got_at, events)
        delays.append(DeliveryDelay(
            name=name,
            publish_lag=max(publish_lag, 0.0),
            end_to_end=max(end_to_end, 0.0),
            outage_affected=outage_affected,
        ))
    delays.sort(key=lambda d: d.name)
    return delays


def _outage_between(t0: datetime, t1: datetime, events: list[Event]) -> bool:
    for e in events:
        if e.type != "portal_response":
            continue
        status = e.data.get("status")
        if not (isinstance(status, int) and status >= 500):
            continue
        ts = _parse_ts(e.ts)
        if ts and t0 <= ts <= t1:
            return True
    return False


def _attribute_gaps(
    windows: list, events: list[Event], observation_end: datetime | None = None
) -> list[Gap]:
    gaps: list[Gap] = []
    threshold = NOMINAL_INTERVAL * GAP_FACTOR
    for (t0, _, _), (t1, _, _) in zip(windows, windows[1:]):
        if t1 - t0 <= threshold:
            continue
        estimated = max(round((t1 - t0) / NOMINAL_INTERVAL) - 1, 1)
        gaps.append(Gap(t0, t1, estimated, _cause_during(t0, t1, events)))

    # Trailing gap: from the last delivered value to "now" (observation end).
    # A long quiet tail means values are missing right up to the present.
    if windows and observation_end is not None:
        last = windows[-1][0]
        if observation_end - last > threshold:
            estimated = max(round((observation_end - last) / NOMINAL_INTERVAL) - 1, 1)
            gaps.append(Gap(last, observation_end, estimated,
                            _cause_during(last, observation_end, events)))
    return gaps


def _cause_during(t0: datetime, t1: datetime, events: list[Event]) -> str:
    """Why was there no value between two delivered windows?"""
    observed = False
    portal_reached = False
    for e in events:
        if e.type != "portal_response":
            continue
        ts = _parse_ts(e.ts)
        if ts is None or not (t0 < ts < t1):
            continue
        observed = True
        status = e.data.get("status")
        if isinstance(status, int) and status < 500:
            portal_reached = True
    if not observed:
        return NOT_OBSERVED
    return DATA_MISSING if portal_reached else PORTAL_OUTAGE


# -- rendering --------------------------------------------------------------


def to_dict(r: Report) -> dict:
    return {
        "account": r.account,
        "observation_start": _iso(r.observation_start),
        "observation_end": _iso(r.observation_end),
        "overall_availability_pct": round(r.overall_availability_pct, 2),
        "endpoints": {
            name: {
                "attempts": e.attempts, "ok": e.ok,
                "server_error": e.server_error, "network_error": e.network_error,
                "availability_pct": round(e.availability_pct, 2),
            }
            for name, e in r.endpoints.items()
        },
        "outages": [
            {"endpoint": o.endpoint, "start": _iso(o.start), "end": _iso(o.end),
             "minutes": round(o.minutes, 1)}
            for o in r.outages
        ],
        "zips": {
            "offered": r.zips.offered, "retrieved": r.zips.retrieved,
            "no_content": r.zips.no_content, "missing": r.zips.missing,
            "availability_pct": round(r.zips.availability_pct, 2),
        },
        "series": {
            "start": _iso(r.series.start), "end": _iso(r.series.end),
            "expected": r.series.expected, "delivered": r.series.delivered,
            "no_content": r.series.no_content,
            "completeness_pct": round(r.series.completeness_pct, 2),
            "delay_measured_count": len(r.series.delays),
            "median_end_to_end_delay_seconds": _round_opt(r.series.median_delay_seconds),
            "max_end_to_end_delay_seconds": _round_opt(r.series.max_delay_seconds),
            "median_publish_lag_seconds": _round_opt(r.series.median_publish_lag_seconds),
            "outage_delayed_count": r.series.outage_delayed_count,
            "gaps": [
                {"after": _iso(g.after), "before": _iso(g.before),
                 "minutes": round(g.minutes, 1),
                 "estimated_missing": g.estimated_missing, "cause": g.cause}
                for g in r.series.gaps
            ],
        },
    }


def render_markdown(r: Report) -> str:
    s = r.series
    lines = [
        f"# Data-Act-Bericht — {r.account}",
        "",
        f"Beobachtungszeitraum: {_fmt(r.observation_start)} – {_fmt(r.observation_end)} "
        f"({_local_tzname()})",
        "",
        "## Portal-Verfügbarkeit",
        f"- Gesamt: **{r.overall_availability_pct:.1f} %** erfolgreiche Anfragen",
    ]
    for name, e in sorted(r.endpoints.items()):
        lines.append(
            f"- `{name}`: {e.availability_pct:.1f} % "
            f"({e.ok}/{e.attempts} ok, {e.server_error}× 5xx, {e.network_error}× Netzfehler)"
        )
    lines.append(f"- Ausfallfenster: {len(r.outages)}")
    for o in r.outages:
        lines.append(f"  - `{o.endpoint}` {_fmt(o.start)} – {_fmt(o.end)} ({o.minutes:.0f} min)")

    lines += [
        "",
        "## ZIP-Verfügbarkeit",
        f"- Angeboten: {r.zips.offered}",
        f"- Geholt (mit Daten): {r.zips.retrieved}",
        f"- Leer-Fenster (no_content): {r.zips.no_content}",
        f"- Angeboten, aber nicht lokal: {len(r.zips.missing)}",
        f"- Verfügbarkeit: **{r.zips.availability_pct:.1f} %**",
        "",
        "## Reihen-Vollständigkeit",
        f"- Reihe: {_fmt(s.start)} – {_fmt(s.end)}",
        f"- Erwartet (geliefert + erkannte Lücken): {s.expected}",
        f"- Geliefert (mit Daten): {s.delivered}",
        f"- Leer-Fenster: {s.no_content}",
        f"- Vollständigkeit: **{s.completeness_pct:.1f} %**",
    ]
    if s.delays:
        lines.append(
            f"- Verzögerung bei mir (Datenmoment → abrufbar), {len(s.delays)} Werte: "
            f"Median {s.median_delay_seconds/60:.1f} min, Max {s.max_delay_seconds/60:.1f} min"
        )
        if s.median_publish_lag_seconds is not None:
            lines.append(
                f"  - davon VW-Publish (Datei noch nicht da): "
                f"Median {s.median_publish_lag_seconds:.0f} s"
            )
        lines.append(
            f"  - durch Portal-Ausfall verzögerte Werte: {s.outage_delayed_count}"
        )
    else:
        lines.append("- Verzögerung: noch keine Werte im Beobachtungsfenster geliefert")
    lines.append(f"- Lücken: {len(s.gaps)}")
    for g in s.gaps:
        lines.append(
            f"  - {_fmt(g.after)} → {_fmt(g.before)} ({g.minutes:.0f} min, "
            f"~{g.estimated_missing} fehlend) — **{g.cause}**"
        )

    causes = _gap_cause_summary(s.gaps)
    if causes:
        lines += ["", "### Zurechenbarkeit der Lücken"]
        for cause, count in causes.items():
            lines.append(f"- {cause}: {count} fehlende Werte")
    return "\n".join(lines) + "\n"


def _gap_cause_summary(gaps: list[Gap]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for g in gaps:
        summary[g.cause] = summary.get(g.cause, 0) + g.estimated_missing
    return summary


def _iso(dt: datetime | None) -> str | None:
    # Local-timezone ISO (with offset) so machine output is unambiguous
    # but in the reader's wall-clock time.
    return dt.astimezone().isoformat() if dt else None


def _fmt(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else "—"


def _local_tzname() -> str:
    return datetime.now().astimezone().strftime("%Z") or "Lokalzeit"


def _round_opt(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None
