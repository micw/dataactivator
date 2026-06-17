"""Per-account locking so only one worker processes an account at a time.

Without this, two processes (e.g. an accidental second ``serve``, or a
``fetch`` run while ``serve`` is watching) would both append to the same
event log and download in parallel — interleaving sequence numbers and
corrupting the evidence.

The lock is keyed per account on purpose: single-instance safety today,
and — with a shared backend later — horizontal scaling where each
instance grabs a disjoint set of accounts. ``Lock`` is the abstraction;
``FileLock`` is the local (single-node) implementation. A DB backend
would implement the same protocol with an advisory/row lock.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Protocol


class Lock(Protocol):
    def acquire(self) -> bool: ...

    def release(self) -> None: ...


class FileLock:
    """Advisory, non-blocking inter-process lock via flock(2).

    The kernel releases the lock automatically if the process dies, so
    there are no stale locks to clean up after a crash.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Try to take the lock without blocking; True if acquired."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        # Record the holder's pid for humans inspecting the lock file.
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> FileLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def account_lock(storage_folder: Path, account: str) -> FileLock:
    """The lock guarding one account's state under the storage folder."""
    return FileLock(storage_folder.expanduser() / ".locks" / f"{account}.lock")
