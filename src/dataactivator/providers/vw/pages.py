"""Parsing of the VW identity provider's login pages.

The sign-in flow renders its state in two places, depending on the
step: classic hidden ``<input>`` fields inside the first ``<form>``,
and/or a JavaScript object on the page::

    window._IDK = { templateModel: { hmac: ..., relayState: ... },
                    csrf_token: '...' }

``collect_login_fields`` merges both sources so the caller gets one
dict of fields to POST, regardless of which step rendered the page.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser


class FirstFormParser(HTMLParser):
    """Action and input fields of the first <form> on the page."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._in_form = False
        self._finished = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._finished:
            return
        attr = dict(attrs)
        if tag == "form" and not self._in_form:
            self.action = attr.get("action")
            self._in_form = True
        elif tag == "input" and self._in_form and attr.get("name"):
            self.fields[attr["name"]] = attr.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._in_form:
            self._in_form = False
            self._finished = True


def parse_first_form(html: str) -> FirstFormParser:
    parser = FirstFormParser()
    parser.feed(html)
    return parser


def extract_template_model(html: str) -> dict:
    """Return the ``templateModel`` JS object as a dict, or {}.

    Finds the first balanced ``{...}`` after the ``templateModel`` key;
    the object is plain JSON in practice.
    """
    key = html.find("templateModel")
    if key == -1:
        return {}
    start = html.find("{", key)
    if start == -1:
        return {}
    depth = 0
    for pos in range(start, len(html)):
        char = html[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(html[start : pos + 1])
                except ValueError:
                    return {}
                return parsed if isinstance(parsed, dict) else {}
    return {}


def extract_csrf(html: str) -> str | None:
    match = re.search(r"csrf_token\s*[:=]\s*['\"]([^'\"]+)['\"]", html)
    return match.group(1) if match else None


def collect_login_fields(html: str) -> tuple[dict[str, str], str | None]:
    """Return (fields_to_post, form_action) for a login page."""
    form = parse_first_form(html)
    fields = dict(form.fields)

    model = extract_template_model(html)
    for key in ("hmac", "relayState"):
        value = model.get(key)
        if value:
            fields[key] = value
    prefilled = (model.get("emailPasswordForm") or {}).get("email")
    if prefilled:
        fields.setdefault("email", prefilled)

    csrf = extract_csrf(html)
    if csrf:
        fields.setdefault("_csrf", csrf)
    return fields, form.action


def extract_login_error(html: str) -> str | None:
    """Human-readable error message rendered into the page, if any."""
    model = extract_template_model(html)
    error = model.get("error") or model.get("errorCode")
    if isinstance(error, dict):
        return error.get("text") or error.get("errorCode") or str(error)
    return str(error) if error else None
