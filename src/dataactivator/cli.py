"""dataACTivator command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .core.config import AppConfig, ConfigError, ProviderConfig, YamlConfigBackend
from .core.events import JsonlEventSink, read_events
from .core.locks import FileLock, account_lock
from .core.reporting import build_report, render_markdown, to_dict
from .core.scheduler import PollTarget, Scheduler
from .core.sessions import SessionStore
from .core.web import make_server as make_web_server
from .providers.vw import PROVIDER_TYPE as VW_TYPE
from .providers.vw.client import VwApiError, VwAuthError, VwPortalClient, VwSettings
from .providers.vw.datasets import check_directory
from .providers.vw.poll import make_observer, poll_cycle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dataactivator",
        description="Retrieve data accessible under the EU Data Act.",
    )
    parser.add_argument(
        "-c", "--config", type=Path, default=None,
        help="path to config file (default: ./config.yaml)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="enable debug logging (login steps, redirects)",
    )
    # No subcommand defaults to "serve" (the container entrypoint).
    sub = parser.add_subparsers(dest="command", required=False)

    sub.add_parser(
        "serve",
        help="long-running daemon: watch all providers (container entrypoint)",
        description="Watches every provider in the config at its poll "
        "interval and records observations. This is the default when no "
        "subcommand is given. A web server will be added to this mode.",
    )

    p_login = sub.add_parser(
        "login",
        help="log in to a provider and store the session",
        description="Reuses a stored session if it is still valid; "
        "otherwise performs a fresh login and stores the new session.",
    )
    p_login.add_argument("provider", help="provider name from the config file")
    p_login.add_argument(
        "--force", action="store_true",
        help="discard any stored session and log in freshly",
    )

    p_fetch = sub.add_parser(
        "fetch",
        help="download new datasets for all vehicles of a provider",
        description="Logs in (or reuses the stored session), lists the "
        "available datasets per vehicle and downloads the ones not yet "
        "present locally. The portal answers erratically with HTTP 500; "
        "expect retries of up to a minute per request.",
    )
    p_fetch.add_argument("provider", help="provider name from the config file")

    p_check = sub.add_parser(
        "check",
        help="verify completeness of locally stored datasets",
        description="Checks ZIP integrity and looks for time gaps in the "
        "stored datasets. With --portal it also compares the local set "
        "against what the portal currently offers.",
    )
    p_check.add_argument("provider", help="provider name from the config file")
    p_check.add_argument(
        "--portal", action="store_true",
        help="also compare against the portal's dataset list (needs login)",
    )

    p_watch = sub.add_parser(
        "watch",
        help="run continuously, polling at a fixed interval",
        description="Long-running process: polls each provider at its "
        "configured interval (default 60s), downloads new datasets and "
        "records every observation to the event log. Stop with Ctrl-C "
        "(SIGINT) or SIGTERM. Designed to run in the foreground (e.g. in "
        "Docker).",
    )
    p_watch.add_argument(
        "provider", nargs="*",
        help="provider name(s); default: all providers in the config",
    )

    p_report = sub.add_parser(
        "report",
        help="compute Data-Act compliance metrics from the event log",
        description="Derives portal availability, ZIP availability and "
        "data-series completeness (with cause attribution) from the "
        "recorded observation log.",
    )
    p_report.add_argument("provider", help="provider name from the config file")
    p_report.add_argument(
        "--format", choices=["md", "json"], default="md",
        help="output format (default: md)",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        config = YamlConfigBackend(args.config).load()
        if args.command in (None, "serve"):
            return _cmd_serve(config)
        if args.command == "login":
            return _cmd_login(config, args.provider, force=args.force)
        if args.command == "fetch":
            return _cmd_fetch(config, args.provider)
        if args.command == "check":
            return _cmd_check(config, args.provider, portal=args.portal)
        if args.command == "watch":
            return _cmd_watch(config, args.provider)
        if args.command == "report":
            return _cmd_report(config, args.provider, fmt=args.format)
        raise AssertionError(f"unhandled command {args.command}")
    except (ConfigError, VwApiError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _ensure_session(
    client: VwPortalClient, store: SessionStore, name: str, *, force: bool = False
) -> None:
    """Reuse the stored session if valid, otherwise log in freshly."""
    if not force:
        cookies = store.load(name)
        if cookies is not None:
            client.cookies.update(cookies)
            if client.session_valid():
                print(f"stored session for {name!r} is still valid")
                return
            print(f"stored session for {name!r} has expired, logging in fresh")
    client.login()
    store.save(name, client.cookies)
    print(f"login successful, session stored for {name!r}")


def _cmd_login(config: AppConfig, name: str, *, force: bool) -> int:
    provider = config.provider(name)
    store = SessionStore(config.storage.folder)
    with _build_client(provider) as client:
        _ensure_session(client, store, name, force=force)
        _print_vehicles(client)
    return 0


def _cmd_fetch(config: AppConfig, name: str) -> int:
    provider = config.provider(name)
    store = SessionStore(config.storage.folder)
    target_root = config.storage.folder.expanduser() / name

    # Refuse to write the same account's event log concurrently with a
    # running serve/watch (or another fetch).
    lock = account_lock(config.storage.folder, name)
    if not lock.acquire():
        print(f"error: {name!r} is locked by another instance "
              f"(serve/watch/fetch already running)", file=sys.stderr)
        return 1

    try:
        with JsonlEventSink(_event_log_path(config, name)) as sink:
            client = _build_client(provider, observer=make_observer(sink, name))
            with client:
                _ensure_session(client, store, name)
                try:
                    result = poll_cycle(client, name, target_root, sink)
                except VwAuthError:
                    # Session died mid-cycle: re-login once and retry.
                    client.login()
                    store.save(name, client.cookies)
                    result = poll_cycle(client, name, target_root, sink)
                except VwApiError as exc:
                    print(f"portal unavailable: {exc}")
                    print("the portal answers HTTP 500 in waves — run fetch "
                          "again later, it resumes where it left off")
                    return 1
    finally:
        lock.release()

    print(f"done: {result.downloaded} downloaded, {result.no_content} empty-window "
          f"markers, {result.skipped} already present, {result.failed} failed")
    print(f"data in {target_root}")
    return 1 if result.failed else 0


@dataclass
class _WatchTarget:
    name: str
    client: VwPortalClient
    sink: JsonlEventSink
    store: SessionStore
    target_root: Path
    lock: FileLock

    def run_cycle(self) -> None:
        # Errors during a cycle are recorded (portal_response events come
        # from the observer); a dead session triggers one re-login. The
        # next interval recovers from anything not handled here.
        try:
            poll_cycle(self.client, self.name, self.target_root, self.sink)
        except VwAuthError:
            try:
                self.client.login()
                self.store.save(self.name, self.client.cookies)
                poll_cycle(self.client, self.name, self.target_root, self.sink)
            except VwApiError as exc:
                self.sink.emit("cycle_error", self.name, reason=str(exc))
        except VwApiError as exc:
            self.sink.emit("cycle_error", self.name, reason=str(exc))


def _build_watch_targets(config: AppConfig, names: list[str]) -> list[_WatchTarget]:
    targets: list[_WatchTarget] = []
    for name in names:
        # Take the account lock first: another instance (a second serve, or
        # a fetch) holding it must not be joined — that would corrupt the
        # shared event log. A locked account is skipped, so multiple
        # instances can each grab a disjoint set.
        lock = account_lock(config.storage.folder, name)
        if not lock.acquire():
            print(f"skipping {name!r}: already locked by another instance")
            continue
        provider = config.provider(name)
        sink = JsonlEventSink(_event_log_path(config, name))
        client = _build_client(provider, observer=make_observer(sink, name))
        # In watch mode the poll interval *is* the retry: one attempt per
        # cycle keeps the availability sampling uniform.
        client.settings.retry_attempts = 1
        store = SessionStore(config.storage.folder)
        cookies = store.load(name)
        if cookies is not None:
            client.cookies.update(cookies)
        targets.append(_WatchTarget(
            name, client, sink, store,
            config.storage.folder.expanduser() / name, lock,
        ))
    return targets


def _run_watch_loop(targets: list[_WatchTarget]) -> None:
    """Drive the poll targets until a stop signal; the blocking core of
    both ``watch`` and ``serve``. The scheduler owns the main thread and
    handles SIGINT/SIGTERM, so a web server (serve mode) runs alongside
    it in a background thread."""
    poll_targets = [
        PollTarget(t.name, t.client.settings.poll_interval, t.run_cycle)
        for t in targets
    ]
    for t in targets:
        t.sink.emit("daemon_start", t.name)
    scheduler = Scheduler(poll_targets)
    try:
        scheduler.run_forever()
    finally:
        for t in targets:
            t.sink.emit("daemon_stop", t.name, reason="signal")
            t.sink.close()
            t.client.close()
            t.lock.release()


def _cmd_watch(config: AppConfig, names: list[str]) -> int:
    if not names:
        names = [p.name for p in config.providers]
    if not names:
        print("no providers configured")
        return 1
    targets = _build_watch_targets(config, names)
    if not targets:
        print("nothing to watch (all requested accounts are locked elsewhere)")
        return 1
    interval = targets[0].client.settings.poll_interval
    print(f"watching {', '.join(t.name for t in targets)} "
          f"every {interval:.0f}s — Ctrl-C to stop")
    _run_watch_loop(targets)
    print("\nstopped")
    return 0


def _cmd_serve(config: AppConfig) -> int:
    """Long-running daemon mode: watch every configured provider. This is
    the container entrypoint. A web server will be added here later,
    started in a background thread before the (blocking) watch loop."""
    names = [p.name for p in config.providers]
    if not names:
        print("no providers configured")
        return 1
    targets = _build_watch_targets(config, names)
    if not targets:
        print("nothing to serve (all accounts are locked by other instances)")
        return 1
    interval = targets[0].client.settings.poll_interval
    print(f"serving: watching {len(targets)} provider(s) "
          f"({', '.join(t.name for t in targets)}) every {interval:.0f}s")

    # Public statistics server runs alongside the (blocking) watch loop.
    # (Management port for health/metrics and the authenticated evcc data
    # endpoint are separate, later additions.)
    web_server = None
    if config.web.enabled:
        web_server = make_web_server(config)
        threading.Thread(target=web_server.serve_forever, daemon=True).start()
        print(f"public stats on http://{config.web.host}:{config.web.port}/")
    try:
        _run_watch_loop(targets)
    finally:
        if web_server is not None:
            web_server.shutdown()
    print("\nstopped")
    return 0


def _cmd_report(config: AppConfig, name: str, *, fmt: str) -> int:
    config.provider(name)  # validate the name exists
    log_path = _event_log_path(config, name)
    if not log_path.exists():
        print(f"no event log for {name!r} at {log_path} — run watch or fetch first")
        return 1
    data_root = config.storage.folder.expanduser() / name
    report = build_report(name, read_events(log_path), data_root=data_root)
    if fmt == "json":
        print(json.dumps(to_dict(report), indent=2, ensure_ascii=False))
    else:
        print(render_markdown(report), end="")
    return 0


def _cmd_check(config: AppConfig, name: str, *, portal: bool) -> int:
    provider = config.provider(name)
    root = config.storage.folder.expanduser() / name
    if not root.exists():
        print(f"no local data for {name!r} at {root} — run fetch first")
        return 1

    vin_dirs = sorted(p for p in root.iterdir() if p.is_dir())
    if not vin_dirs:
        print(f"no vehicle data directories under {root}")
        return 1

    portal_lists: dict[str, list[str] | None] = {}
    if portal:
        portal_lists = _portal_dataset_names(provider, config, name,
                                             [d.name for d in vin_dirs])

    all_complete = True
    for vin_dir in vin_dirs:
        vin = vin_dir.name
        report = check_directory(vin_dir)
        print(f"\nvehicle {vin}: {len(report.files)} datasets")

        for f in report.corrupt:
            print(f"  CORRUPT  {f.name}: {f.error}")

        if report.valid:
            ordered = sorted(report.valid, key=lambda f: f.timestamp)
            counts = [f.point_count for f in ordered]
            print(f"  integrity: {len(report.valid)} ok, {len(report.corrupt)} corrupt")
            print(f"  timespan:  {ordered[0].timestamp:%Y-%m-%d %H:%M} .. "
                  f"{ordered[-1].timestamp:%H:%M} UTC")
            print(f"  points/dataset: {min(counts)}–{max(counts)} "
                  f"(varies by design — fields are reported on change)")

        if report.gaps:
            all_complete = False
            print(f"  TIME GAPS ({len(report.gaps)}):")
            for g in report.gaps:
                print(f"    {g.after:%H:%M} -> {g.before:%H:%M}  "
                      f"{g.minutes:.0f} min, ~{g.estimated_missing} dataset(s) missing")
        elif report.valid:
            print("  no time gaps")

        if portal:
            remote = portal_lists.get(vin)
            if remote is None:
                print("  portal check: skipped (portal unreachable)")
            else:
                local_names = {f.name for f in report.files}
                missing = [n for n in remote if n not in local_names]
                if missing:
                    all_complete = False
                    print(f"  PORTAL has {len(missing)} dataset(s) not stored locally:")
                    for n in missing[:10]:
                        print(f"    {n}")
                else:
                    print(f"  portal check: local set covers all "
                          f"{len(remote)} offered datasets")

        if report.corrupt:
            all_complete = False

    print()
    print("result: complete" if all_complete
          else "result: INCOMPLETE — see findings above")
    return 0 if all_complete else 1


def _portal_dataset_names(
    provider: ProviderConfig, config: AppConfig, name: str, vins: list[str]
) -> dict[str, list[str] | None]:
    """Dataset names the portal currently offers per VIN ({vin: names|None})."""
    result: dict[str, list[str] | None] = {}
    store = SessionStore(config.storage.folder)
    with _build_client(provider) as client:
        try:
            _ensure_session(client, store, name)
        except VwApiError as exc:
            print(f"portal login failed, skipping portal check: {exc}")
            return {vin: None for vin in vins}
        for vin in vins:
            try:
                request = client.get_data_request(vin)
                identifier = request.get("Identifier")
                datasets = client.list_datasets(vin, identifier) if identifier else []
                result[vin] = [d["name"] for d in datasets if d.get("name")]
            except VwApiError as exc:
                print(f"portal list for {vin} failed: {exc}")
                result[vin] = None
    return result


def _build_client(provider: ProviderConfig, observer=None) -> VwPortalClient:
    if provider.type != VW_TYPE:
        raise ConfigError(
            f"provider {provider.name!r} has unsupported type {provider.type!r} "
            f"(supported: {VW_TYPE})"
        )
    try:
        settings = VwSettings.model_validate(provider.settings)
    except ValidationError as exc:
        raise ConfigError(f"provider {provider.name!r}: {exc}") from exc
    return VwPortalClient(settings, observer=observer)


def _event_log_path(config: AppConfig, name: str) -> Path:
    return config.storage.folder.expanduser() / name / "events.jsonl"


def _print_vehicles(client: VwPortalClient) -> None:
    vehicles = client.list_vehicles()
    if not vehicles:
        print("no vehicles visible on this account")
        return
    print(f"{len(vehicles)} vehicle(s):")
    for veh in vehicles:
        nickname = f"  ({veh['nickname']})" if "nickname" in veh else ""
        print(f"  {veh['vin']}{nickname}")


if __name__ == "__main__":
    raise SystemExit(main())
