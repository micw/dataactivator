"""Persistence for provider sessions (cookie jars).

The VW portal authenticates via session cookies, not client-visible
tokens, so "remembering a login" means persisting the cookie jar.
Stored under ``<storage.folder>/.sessions/<provider-name>.json`` with
mode 600 — session cookies grant account access like a password.
"""

from __future__ import annotations

import json
from http.cookiejar import Cookie
from pathlib import Path

import httpx


class SessionStore:
    def __init__(self, storage_folder: Path) -> None:
        self._dir = storage_folder.expanduser() / ".sessions"

    def _path(self, name: str) -> Path:
        return self._dir / f"{name}.json"

    def load(self, name: str) -> httpx.Cookies | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        cookies = httpx.Cookies()
        for e in entries:
            cookies.jar.set_cookie(_make_cookie(e))
        return cookies

    def save(self, name: str, cookies: httpx.Cookies) -> None:
        entries = [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "expires": c.expires,
                "secure": c.secure,
            }
            for c in cookies.jar
        ]
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(name)
        path.touch(mode=0o600, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        path.chmod(0o600)

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)


def _make_cookie(e: dict) -> Cookie:
    domain = e.get("domain") or ""
    return Cookie(
        version=0,
        name=e["name"],
        value=e["value"],
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=e.get("path") or "/",
        path_specified=True,
        secure=bool(e.get("secure")),
        expires=e.get("expires"),
        discard=False,
        comment=None,
        comment_url=None,
        rest={},
    )
