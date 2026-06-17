from dataactivator.providers.vw.pages import (
    collect_login_fields,
    extract_csrf,
    extract_login_error,
    extract_template_model,
)

SIGNIN_HTML = """
<html><body>
<form method="post" action="/signin-service/v1/xyz/login/identifier">
  <input type="hidden" name="_csrf" value="csrf-from-input"/>
  <input type="hidden" name="relayState" value="rs-1"/>
  <input type="hidden" name="hmac" value="hmac-1"/>
  <input type="email" name="email" value=""/>
</form>
</body></html>
"""

AUTHENTICATE_HTML = """
<html><body><script>
window._IDK = {
  templateModel: {"hmac": "hmac-2", "relayState": "rs-2",
                  "emailPasswordForm": {"email": "user@example.com"},
                  "postAction": "login/authenticate"},
  csrf_token: 'csrf-from-js'
};
</script></body></html>
"""

ERROR_HTML = """
<script>
window._IDK = {
  templateModel: {"error": {"text": "Incorrect email or password"}},
  csrf_token: 'x'
};
</script>
"""


def test_fields_from_html_inputs() -> None:
    fields, action = collect_login_fields(SIGNIN_HTML)
    assert action == "/signin-service/v1/xyz/login/identifier"
    assert fields["_csrf"] == "csrf-from-input"
    assert fields["hmac"] == "hmac-1"
    assert fields["relayState"] == "rs-1"


def test_fields_from_js_template_model() -> None:
    fields, action = collect_login_fields(AUTHENTICATE_HTML)
    assert action is None
    assert fields["_csrf"] == "csrf-from-js"
    assert fields["hmac"] == "hmac-2"
    assert fields["relayState"] == "rs-2"
    assert fields["email"] == "user@example.com"


def test_template_model_balanced_braces() -> None:
    model = extract_template_model(AUTHENTICATE_HTML)
    assert model["emailPasswordForm"] == {"email": "user@example.com"}


def test_csrf_missing() -> None:
    assert extract_csrf("<html></html>") is None


def test_login_error_extracted() -> None:
    assert extract_login_error(ERROR_HTML) == "Incorrect email or password"
    assert extract_login_error(AUTHENTICATE_HTML) is None
