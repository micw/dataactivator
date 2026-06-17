"""One polling cycle against the VW portal, emitting observation events.

This is the shared work unit for both the one-shot ``fetch`` and the
continuous ``watch`` loop: discover vehicles, read each vehicle's data
request, list the offered datasets, and download the ones not yet stored.
Every portal HTTP attempt is recorded through the client's observer; the
dataset outcomes are recorded here.

``no_content`` windows are persisted as zero-byte markers so the local
store has a file for every window the portal acknowledged — distinct
from a window for which the portal offered nothing at all.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from ...core.events import EventSink
from . import const
from .client import Observation, VwApiError, VwAuthError, VwPortalClient

logger = logging.getLogger(__name__)


@dataclass
class PollResult:
    downloaded: int = 0
    no_content: int = 0
    skipped: int = 0
    failed: int = 0
    vehicles: int = 0


def make_observer(sink: EventSink, account: str):
    """An observer that records every portal HTTP attempt as an event."""

    def observe(obs: Observation) -> None:
        sink.emit(
            "portal_response", account,
            endpoint=obs.endpoint, status=obs.status,
            latency_ms=obs.latency_ms, portal_date=obs.portal_date,
            error=obs.error,
        )

    return observe


def poll_cycle(
    client: VwPortalClient,
    account: str,
    target_root: Path,
    sink: EventSink,
    *,
    request_type: str = "partial",
) -> PollResult:
    """Run one full cycle for an account. Assumes the client is logged in.

    Raises ``VwAuthError`` if the session expired (the caller re-logs in
    and the next cycle recovers); other per-dataset errors are recorded
    and do not abort the cycle.
    """
    result = PollResult()
    vehicles = client.list_vehicles()
    result.vehicles = len(vehicles)

    for veh in vehicles:
        vin = veh["vin"]
        # Session expiry must propagate so the caller re-logs in; a portal
        # outage for one vehicle is recorded but must not abort the rest.
        try:
            request = client.get_data_request(vin, request_type)
            identifier = request.get("Identifier")
            if not identifier:
                sink.emit("no_data_request", account, vin=vin)
                continue
            sink.emit(
                "data_request", account, vin=vin, identifier=identifier,
                start_date=request.get("StartDate"),
                frequency=request.get("Frequency"),
            )
            datasets = client.list_datasets(vin, identifier, request_type)
        except VwAuthError:
            raise
        except VwApiError as exc:
            sink.emit("vehicle_failed", account, vin=vin, reason=str(exc))
            result.failed += 1
            continue

        sink.emit(
            "datasets_offered", account, vin=vin,
            count=len(datasets),
            datasets=[
                {
                    "name": d.get("name"),
                    "createdOn": d.get("createdOn"),
                    "size": d.get("size"),
                    "no_content": str(d.get("name", "")).endswith(const.NO_CONTENT_SUFFIX),
                }
                for d in datasets
            ],
        )

        target = target_root / vin
        target.mkdir(parents=True, exist_ok=True)
        for ds in datasets:
            _handle_dataset(client, account, vin, identifier, ds, target,
                            sink, request_type, result)

    return result


def _handle_dataset(client, account, vin, identifier, ds, target, sink,
                    request_type, result: PollResult) -> None:
    name = ds.get("name", "")
    if not name:
        return
    path = target / name
    if path.exists():
        result.skipped += 1
        return

    if name.endswith(const.NO_CONTENT_SUFFIX):
        # The portal acknowledged the window with no data. Persist a marker
        # (the download endpoint refuses no-content names); the file's
        # existence is the record.
        path.write_bytes(b"")
        sink.emit("dataset_downloaded", account, vin=vin, name=name,
                  bytes=0, sha256=None, no_content=True)
        result.no_content += 1
        return

    try:
        raw = client.download_dataset(vin, identifier, name, request_type)
    except VwApiError as exc:
        sink.emit("download_failed", account, vin=vin, name=name, reason=str(exc))
        result.failed += 1
        return

    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(raw)
    tmp.replace(path)
    sink.emit("dataset_downloaded", account, vin=vin, name=name,
              bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
              no_content=False)
    result.downloaded += 1
