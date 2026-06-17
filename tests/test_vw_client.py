import base64
import json
import time

import httpx

from dataactivator.providers.vw.client import VwPortalClient, VwSettings


def make_jwt(exp: float) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()
    ).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.sig"


def make_client(token: str | None) -> VwPortalClient:
    cookies = httpx.Cookies()
    if token is not None:
        cookies.set("access_token", token, domain="eu-data-act.drivesomethinggreater.com")
    return VwPortalClient(
        VwSettings(email="a@b.c", password="x"), cookies=cookies
    )


def test_token_valid() -> None:
    with make_client(make_jwt(time.time() + 3600)) as client:
        assert client._access_token_expired() is False


def test_token_expired() -> None:
    with make_client(make_jwt(time.time() - 10)) as client:
        assert client._access_token_expired() is True


def test_token_about_to_expire_counts_as_expired() -> None:
    with make_client(make_jwt(time.time() + 5)) as client:
        assert client._access_token_expired() is True


def test_token_missing() -> None:
    with make_client(None) as client:
        assert client._access_token_expired() is True


def test_token_garbage() -> None:
    with make_client("not-a-jwt") as client:
        assert client._access_token_expired() is True
