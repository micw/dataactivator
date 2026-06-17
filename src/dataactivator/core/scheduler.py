"""Fixed-interval scheduler driving one or more poll targets.

Provider-agnostic on purpose: it knows nothing about VW or events, only
that each target should run roughly every ``interval`` seconds. This is
the seam for multi-account today (targets from config) and multi-user
later (targets from a database) without changing the loop.

Each target's ``run`` is expected to handle and record its own errors;
the loop only guards against unexpected exceptions so one bad cycle can
never kill the daemon.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class PollTarget:
    name: str
    interval: float
    run: Callable[[], None]


class Scheduler:
    def __init__(
        self,
        targets: list[PollTarget],
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._targets = targets
        self._monotonic = monotonic
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run_forever(
        self,
        *,
        install_signals: bool = True,
        max_cycles: int | None = None,
    ) -> None:
        """Run targets on their intervals until stopped.

        ``max_cycles`` (tests) returns after that many target runs.
        Targets run immediately on start, then every ``interval`` seconds.
        """
        if install_signals:
            self._install_signals()

        next_due = {t.name: self._monotonic() for t in self._targets}
        cycles = 0
        while not self._stop.is_set():
            now = self._monotonic()
            for target in self._targets:
                if self._stop.is_set():
                    break
                if now >= next_due[target.name]:
                    self._run_one(target)
                    next_due[target.name] = self._monotonic() + target.interval
                    cycles += 1
                    if max_cycles is not None and cycles >= max_cycles:
                        return
            # Sleep until the soonest target is due, capped so signals and
            # stop requests are honoured promptly. Interruptible.
            soonest = min(next_due.values()) if next_due else self._monotonic() + 1.0
            wait = max(0.0, min(soonest - self._monotonic(), 1.0))
            self._stop.wait(wait)

    def _run_one(self, target: PollTarget) -> None:
        try:
            target.run()
        except Exception:  # noqa: BLE001 — never let one cycle kill the loop
            logger.exception("unexpected error in target %s", target.name)

    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self.request_stop())
            except (ValueError, OSError):
                pass
