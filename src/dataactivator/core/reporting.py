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
import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from ..providers.vw.const import NO_CONTENT_SUFFIX
from ..providers.vw.datasets import dataset_captures, parse_timestamp
from .events import Event

NOMINAL_INTERVAL = timedelta(minutes=15)
# A gap longer than this implies at least one missing value.
GAP_FACTOR = 1.5
# Data is considered "current" if captured no longer ago than this when published.
FRESH_MAX_AGE = timedelta(minutes=30)

# Slot/gap attribution causes. These codes are the stable machine values
# (JSON); human-readable labels for the rendered views live in CAUSE_LABELS.
COMPLETE = "COMPLETE"
NO_CONTENT = "NO_CONTENT"
DATA_MISSING = "DATA_MISSING"        # portal up, no value → VW (hard)
PORTAL_OUTAGE = "PORTAL_OUTAGE"      # portal down → VW (soft)
DOWNLOAD_FAILED = "DOWNLOAD_FAILED"  # offered but our download failed → our side
NOT_OBSERVED = "NOT_OBSERVED"        # daemon/our side down → not attributable

CAUSE_LABELS = {
    COMPLETE: "Geliefert",
    NO_CONTENT: "Leer geliefert (ohne Daten)",
    DATA_MISSING: "Nicht geliefert (Portal war erreichbar)",
    PORTAL_OUTAGE: "Portal nicht erreichbar",
    DOWNLOAD_FAILED: "Abruf fehlgeschlagen (clientseitig)",
    NOT_OBSERVED: "Nicht erfasst (kein Messbetrieb)",
}


def cause_label(code: str) -> str:
    return CAUSE_LABELS.get(code, code)


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
    expected: int = 0           # covered + attributable missing (excl. NOT_OBSERVED)
    delivered: int = 0          # distinct windows with content
    no_content: int = 0
    not_observed: int = 0       # missing while the daemon wasn't watching
    gaps: list[Gap] = field(default_factory=list)
    delays: list[DeliveryDelay] = field(default_factory=list)
    cause_counts: dict[str, int] = field(default_factory=dict)  # per-slot

    @property
    def completeness_pct(self) -> float:
        # Over windows VW should have delivered *while we were watching*;
        # NOT_OBSERVED windows (our downtime) are excluded — they are not
        # attributable to VW.
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
class Freshness:
    """How stale the delivered data actually is.

    VW re-publishes the same in-vehicle snapshot every 15 min while the
    car is offline, so a file arriving on time can still carry hours-old
    data. ``capture_lag`` = publish time − newest in-vehicle capture;
    ``frozen`` = stretches where the capture time did not advance across
    consecutive publishes (VW shipping unchanged snapshots).
    """

    capture_lags_seconds: list[float] = field(default_factory=list)
    datasets_total: int = 0
    longest_frozen_seconds: float = 0.0
    total_frozen_seconds: float = 0.0
    frozen_spans: int = 0

    @property
    def median_lag_seconds(self) -> float | None:
        return statistics.median(self.capture_lags_seconds) if self.capture_lags_seconds else None

    @property
    def max_lag_seconds(self) -> float | None:
        return max(self.capture_lags_seconds) if self.capture_lags_seconds else None

    @property
    def current_pct(self) -> float:
        """Share of datasets whose data was current (≤ FRESH_MAX_AGE) at publish."""
        if not self.capture_lags_seconds:
            return 0.0
        fresh = sum(1 for lag in self.capture_lags_seconds
                    if lag <= FRESH_MAX_AGE.total_seconds())
        return 100.0 * fresh / len(self.capture_lags_seconds)


@dataclass
class Report:
    account: str
    observation_start: datetime | None
    observation_end: datetime | None
    endpoints: dict[str, EndpointAvailability]
    outages: list[Outage]
    zips: ZipAvailability
    series: SeriesCompleteness
    freshness: Freshness = field(default_factory=Freshness)

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
    freshness = _freshness(data_root)

    return Report(
        account=account,
        observation_start=obs_start,
        observation_end=obs_end,
        endpoints=endpoints,
        outages=outages,
        zips=zips,
        series=series,
        freshness=freshness,
    )


def _freshness(data_root: Path | None) -> Freshness:
    """Capture-lag and frozen-data analysis from the stored ZIPs.

    Reads each content dataset's newest in-vehicle capture time (the real
    freshness), independent of the punctual publish cadence.
    """
    fr = Freshness()
    if data_root is None or not data_root.exists():
        return fr

    captures: list[tuple[datetime, datetime | None]] = []
    for vin_dir in sorted(d for d in data_root.iterdir() if d.is_dir()):
        captures.extend(dataset_captures(vin_dir))
    captures.sort(key=lambda c: c[0])
    fr.datasets_total = len(captures)

    for publish, captured in captures:
        if captured is not None:
            fr.capture_lags_seconds.append(max((publish - captured).total_seconds(), 0.0))

    # Frozen stretches: consecutive publishes during which the newest
    # capture never advanced (VW re-shipping an unchanged snapshot).
    threshold = NOMINAL_INTERVAL * 2
    seen_max: datetime | None = None
    frozen_start: datetime | None = None
    prev_publish: datetime | None = None
    spans: list[tuple[datetime, datetime]] = []
    for publish, captured in captures:
        advanced = captured is not None and (seen_max is None or captured > seen_max)
        if advanced:
            seen_max = captured
            if frozen_start is not None:
                spans.append((frozen_start, prev_publish))
                frozen_start = None
        elif frozen_start is None and prev_publish is not None:
            frozen_start = prev_publish
        prev_publish = publish
    if frozen_start is not None and prev_publish is not None:
        spans.append((frozen_start, prev_publish))

    durations = [(b - a).total_seconds() for a, b in spans if b - a > threshold]
    fr.frozen_spans = len(durations)
    fr.total_frozen_seconds = sum(durations)
    fr.longest_frozen_seconds = max(durations) if durations else 0.0
    return fr


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
    series.gaps, series.cause_counts = _analyse_gaps(windows, events, observation_end)

    # Expected = covered plus the *attributable* missing values. Attribution
    # is per 15-min slot (not per gap), so a week of daemon downtime is
    # counted as NOT_OBSERVED — excluded from the denominator and reported
    # separately, because we cannot blame VW for windows we weren't watching.
    covered = series.delivered + series.no_content
    series.not_observed = series.cause_counts.get(NOT_OBSERVED, 0)
    attributable_missing = sum(
        v for k, v in series.cause_counts.items() if k != NOT_OBSERVED)
    series.expected = covered + attributable_missing
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

    # Only trust a delay if we were actively polling right after the data
    # moment; otherwise our own downtime (between the data window and when
    # we next ran) would masquerade as VW being late.
    poll_times = [t for t, _ in _poll_samples(events)]

    delays: list[DeliveryDelay] = []
    for name, got_at in downloaded_at.items():
        try:
            measured = parse_timestamp(name)
        except ValueError:
            continue
        if obs_start is not None and measured < obs_start:
            continue
        if not _polled_within(poll_times, measured, measured + 2 * NOMINAL_INTERVAL):
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


def _polled_within(poll_times: list[datetime], t0: datetime, t1: datetime) -> bool:
    i = bisect.bisect_left(poll_times, t0)
    return i < len(poll_times) and poll_times[i] <= t1


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


def _poll_samples(events: list[Event]) -> list[tuple[datetime, bool]]:
    """Sorted (timestamp, portal_reachable) for every poll attempt."""
    samples = []
    for e in events:
        if e.type != "portal_response":
            continue
        ts = _parse_ts(e.ts)
        if ts is None:
            continue
        status = e.data.get("status")
        samples.append((ts, isinstance(status, int) and status < 500))
    samples.sort()
    return samples


def _analyse_gaps(
    windows: list, events: list[Event], observation_end: datetime | None = None
) -> tuple[list[Gap], dict[str, int]]:
    """Attribute every missing 15-min slot to a cause.

    Per slot (not per gap): a multi-day gap that contains only a handful
    of polls is mostly NOT_OBSERVED, not VW's fault. For each missing slot
    we look for polls within ±½ interval.
    """
    samples = _poll_samples(events)
    times = [s[0] for s in samples]
    half = NOMINAL_INTERVAL / 2

    def cause_at(slot: datetime) -> str:
        lo = bisect.bisect_left(times, slot - half)
        hi = bisect.bisect_right(times, slot + half)
        if lo == hi:
            return NOT_OBSERVED
        return DATA_MISSING if any(samples[i][1] for i in range(lo, hi)) else PORTAL_OUTAGE

    gaps: list[Gap] = []
    counts: dict[str, int] = {}
    threshold = NOMINAL_INTERVAL * GAP_FACTOR

    def add_gap(t0: datetime, t1: datetime) -> None:
        missing = max(round((t1 - t0) / NOMINAL_INTERVAL) - 1, 1)
        slot_causes = [cause_at(t0 + NOMINAL_INTERVAL * k) for k in range(1, missing + 1)]
        for cause in slot_causes:
            counts[cause] = counts.get(cause, 0) + 1
        dominant = max(set(slot_causes), key=slot_causes.count)
        gaps.append(Gap(t0, t1, missing, dominant))

    for (t0, _, _), (t1, _, _) in zip(windows, windows[1:]):
        if t1 - t0 > threshold:
            add_gap(t0, t1)
    # Trailing gap: last delivered value to "now" (observation end).
    if windows and observation_end is not None:
        last = windows[-1][0]
        if observation_end - last > threshold:
            add_gap(last, observation_end)
    return gaps, counts


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
            "not_observed": r.series.not_observed,
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
        "freshness": {
            "datasets_total": r.freshness.datasets_total,
            "current_pct": round(r.freshness.current_pct, 2),
            "current_threshold_minutes": int(FRESH_MAX_AGE.total_seconds() / 60),
            "median_capture_lag_seconds": _round_opt(r.freshness.median_lag_seconds),
            "max_capture_lag_seconds": _round_opt(r.freshness.max_lag_seconds),
            "frozen_spans": r.freshness.frozen_spans,
            "longest_frozen_seconds": round(r.freshness.longest_frozen_seconds, 1),
            "total_frozen_seconds": round(r.freshness.total_frozen_seconds, 1),
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
        "## Datenpaket-Verfügbarkeit",
        f"- Bereitgestellt: {r.zips.offered}",
        f"- Abgerufen (mit Daten): {r.zips.retrieved}",
        f"- Leer (ohne Daten): {r.zips.no_content}",
        f"- Bereitgestellt, aber nicht lokal: {len(r.zips.missing)}",
        f"- Verfügbarkeit: **{r.zips.availability_pct:.1f} %**",
        "",
        "## Datenvollständigkeit",
        f"- Reihe: {_fmt(s.start)} – {_fmt(s.end)}",
        f"- Erwartet (geliefert + erkannte Lücken): {s.expected}",
        f"- Geliefert (mit Daten): {s.delivered}",
        f"- Leer geliefert: {s.no_content}",
        f"- Vollständigkeit: **{s.completeness_pct:.1f} %** "
        f"(während Beobachtung; {s.not_observed} nicht erfasste Fenster, "
        f"separat ausgewiesen)",
        f"- Aktualität (Daten ≤ 30 min alt): **{r.freshness.current_pct:.1f} %**",
    ]
    if s.delays:
        lines.append(
            f"- Bereitstellungsverzögerung, {len(s.delays)} Werte: "
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
        lines.append("- Bereitstellungsverzögerung: noch keine Werte im Beobachtungsfenster")
    lines.append(f"- Lücken: {len(s.gaps)}")
    for g in s.gaps:
        lines.append(
            f"  - {_fmt(g.after)} → {_fmt(g.before)} ({g.minutes:.0f} min, "
            f"~{g.estimated_missing} fehlend) — **{cause_label(g.cause)}**"
        )

    if s.cause_counts:
        lines += ["", "### Zurechenbarkeit der fehlenden Werte"]
        for cause, count in sorted(s.cause_counts.items()):
            lines.append(f"- {cause_label(cause)}: {count}")

    fr = r.freshness
    lines += ["", "## Datenfrische"]
    if fr.median_lag_seconds is not None:
        lines.append(
            f"- Erfassungs-Lag (Publish − Fahrzeug-Erfassung): "
            f"Median {fr.median_lag_seconds/3600:.1f} h, Max {fr.max_lag_seconds/3600:.1f} h"
        )
    else:
        lines.append("- Erfassungs-Lag: keine Erfassungszeiten gefunden")
    lines.append(
        f"- Eingefrorene Daten: {fr.frozen_spans} Phasen, "
        f"längste {fr.longest_frozen_seconds/3600:.1f} h, "
        f"gesamt {fr.total_frozen_seconds/3600:.1f} h "
        f"(VW re-publiziert unveränderte Snapshots)"
    )
    return "\n".join(lines) + "\n"


_HTML_STYLE = """
:root { font-family: system-ui, sans-serif; line-height: 1.5; }
body { max-width: 56rem; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
h1 { margin-bottom: 0; }
.sub { color: #666; margin-top: .25rem; }
section { margin: 1.5rem 0; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #eee; }
.big { font-size: 1.6rem; font-weight: 700; }
.good { color: #1a7f37; } .warn { color: #bf8700; } .bad { color: #cf222e; }
.muted { color: #888; } a { color: #0969da; }
""".strip()


def _pct_class(pct: float) -> str:
    return "good" if pct >= 99 else "warn" if pct >= 90 else "bad"


def render_html(r: Report) -> str:
    """Public statistics page for one account — aggregate only, no VIN."""
    import html

    s = r.series
    av_cls = _pct_class(r.overall_availability_pct)
    comp_cls = _pct_class(s.completeness_pct)
    out: list[str] = [
        "<!doctype html><html lang=de><head><meta charset=utf-8>",
        "<meta name=viewport content='width=device-width, initial-scale=1'>",
        f"<title>Data-Act-Bericht — {html.escape(r.account)}</title>",
        f"<style>{_HTML_STYLE}</style></head><body>",
        f"<h1>Data-Act-Bericht — {html.escape(r.account)}</h1>",
        f"<p class=sub>Beobachtungszeitraum {_fmt(r.observation_start)} – "
        f"{_fmt(r.observation_end)} ({_local_tzname()})</p>",

        "<section><h2>Portal-Verfügbarkeit</h2>",
        f"<p class='big {av_cls}'>{r.overall_availability_pct:.1f} %</p>",
        "<table><tr><th>Endpunkt</th><th>Verfügbar</th><th>OK</th>"
        "<th>5xx</th><th>Netzfehler</th></tr>",
    ]
    for name, e in sorted(r.endpoints.items()):
        out.append(
            f"<tr><td>{html.escape(name)}</td>"
            f"<td class='{_pct_class(e.availability_pct)}'>{e.availability_pct:.1f} %</td>"
            f"<td>{e.ok}/{e.attempts}</td><td>{e.server_error}</td>"
            f"<td>{e.network_error}</td></tr>"
        )
    out.append("</table>")
    if r.outages:
        out.append(f"<p>Ausfallfenster: {len(r.outages)}</p><ul>")
        for o in r.outages:
            out.append(f"<li>{html.escape(o.endpoint)}: {_fmt(o.start)} – "
                       f"{_fmt(o.end)} ({o.minutes:.0f} min)</li>")
        out.append("</ul>")
    out.append("</section>")

    out += [
        "<section><h2>Datenpaket-Verfügbarkeit</h2>",
        f"<p class='big {_pct_class(r.zips.availability_pct)}'>"
        f"{r.zips.availability_pct:.1f} %</p>",
        f"<p>Bereitgestellt {r.zips.offered} · abgerufen {r.zips.retrieved} · "
        f"leer {r.zips.no_content} · fehlt {len(r.zips.missing)}</p></section>",

        "<section><h2>Datenvollständigkeit</h2>",
        f"<p class='big {comp_cls}'>{s.completeness_pct:.1f} %</p>",
        f"<p>Erwartet {s.expected} · geliefert {s.delivered} · "
        f"leer {s.no_content}</p>",
        f"<p class=muted>{s.not_observed} nicht erfasste Fenster "
        f"(kein Messbetrieb, nicht VW zugerechnet)</p>" if s.not_observed else "",
        f"<p>Aktualität (Daten ≤ 30 min alt): "
        f"<b class='{_pct_class(r.freshness.current_pct)}'>"
        f"{r.freshness.current_pct:.1f} %</b></p>",
    ]
    if s.delays:
        delay = f"Median {s.median_delay_seconds/60:.1f} min, Max {s.max_delay_seconds/60:.1f} min"
        out.append(f"<p>Bereitstellungsverzögerung ({len(s.delays)} Werte): {delay}; "
                   f"durch Portal-Ausfall verzögert: {s.outage_delayed_count}</p>")
    if s.cause_counts:
        out.append("<p>Fehlende Werte nach Ursache:</p><ul>")
        for cause, count in sorted(s.cause_counts.items()):
            out.append(f"<li>{html.escape(cause_label(cause))}: {count}</li>")
        out.append("</ul>")
    else:
        out.append("<p class=muted>keine Lücken</p>")
    out.append("</section>")

    fr = r.freshness
    out.append("<section><h2>Datenfrische</h2>")
    if fr.median_lag_seconds is not None:
        out.append(
            f"<p class='big {_pct_class(0)}'>Median {fr.median_lag_seconds/3600:.1f} h "
            f"alt</p><p>Erfassungs-Lag zwischen Messung im Fahrzeug und "
            f"Bereitstellung · Max {fr.max_lag_seconds/3600:.1f} h</p>")
    else:
        out.append("<p class=muted>keine Erfassungszeiten gefunden</p>")
    if fr.frozen_spans:
        out.append(
            f"<p>Eingefrorene Daten: {fr.frozen_spans} Phasen, längste "
            f"{fr.longest_frozen_seconds/3600:.1f} h, gesamt "
            f"{fr.total_frozen_seconds/3600:.1f} h — unveränderte Snapshots "
            f"trotz laufender Publish-Kadenz</p>")
    out.append("</section>")

    out.append("<p class=sub><a href='./'>&larr; Übersicht</a></p>")
    out.append("</body></html>")
    return "".join(out)


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
