from pathlib import Path

from dataactivator.core.locks import FileLock, account_lock


def test_second_acquire_fails_while_held(tmp_path: Path) -> None:
    a = FileLock(tmp_path / "x.lock")
    b = FileLock(tmp_path / "x.lock")
    assert a.acquire() is True
    assert b.acquire() is False  # contention
    a.release()
    assert b.acquire() is True   # free again
    b.release()


def test_release_is_idempotent(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "x.lock")
    lock.acquire()
    lock.release()
    lock.release()  # no error


def test_context_manager(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with FileLock(path) as held:
        assert held.acquire() is True
        assert FileLock(path).acquire() is False
    assert FileLock(path).acquire() is True


def test_account_lock_path(tmp_path: Path) -> None:
    lock = account_lock(tmp_path, "vw-id7")
    assert lock.path == tmp_path / ".locks" / "vw-id7.lock"
    assert lock.acquire() is True
    lock.release()
