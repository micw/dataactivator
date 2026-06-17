from pathlib import Path

import httpx

from dataactivator.core.sessions import SessionStore


def make_cookies() -> httpx.Cookies:
    cookies = httpx.Cookies()
    cookies.set("SESSION", "abc123", domain="eu-data-act.example.com", path="/")
    cookies.set("AWSALB", "lb-cookie", domain="eu-data-act.example.com", path="/")
    return cookies


def test_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.save("vw", make_cookies())
    loaded = store.load("vw")
    assert loaded is not None
    assert {c.name: c.value for c in loaded.jar} == {
        "SESSION": "abc123",
        "AWSALB": "lb-cookie",
    }
    assert all(c.domain == "eu-data-act.example.com" for c in loaded.jar)


def test_file_permissions(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.save("vw", make_cookies())
    path = tmp_path / ".sessions" / "vw.json"
    assert path.stat().st_mode & 0o077 == 0


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert SessionStore(tmp_path).load("nope") is None


def test_load_corrupt_returns_none(tmp_path: Path) -> None:
    path = tmp_path / ".sessions"
    path.mkdir()
    (path / "vw.json").write_text("{not json")
    assert SessionStore(tmp_path).load("vw") is None


def test_delete(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.save("vw", make_cookies())
    store.delete("vw")
    assert store.load("vw") is None
    store.delete("vw")  # idempotent
