"""Protocol facts for the VW EU Data Act portal.

The portal uses an OIDC authorization-code flow against the VW group
identity provider. The portal backend consumes the code at its login
callback and authenticates the client via session cookies — no access
or refresh token ever reaches the client.

The portal's own ``/services/redirect/authentication`` entry point
returns HTTP 500 for non-browser clients, so the authorize URL must be
constructed directly.
"""

PORTAL_BASE = "https://eu-data-act.drivesomethinggreater.com"
IDENTITY_BASE = "https://identity.vwgroup.io"

AUTHORIZE_URL = f"{IDENTITY_BASE}/oidc/v1/authorize"
CLIENT_ID = "9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com"
SCOPE = "openid cars profile"
REDIRECT_URI = f"{PORTAL_BASE}/login"
DEFAULT_BRAND = "VOLKSWAGEN_PASSENGER_CARS"

# Cheap authenticated endpoint, used to probe whether a stored session
# is still valid before doing a fresh login. The backend answers
# HTTP 400 without the viewPosition parameter.
VEHICLES_PATH = "/proxy_api/consent/me/vehicles"
VEHICLES_PARAMS = {"viewPosition": "FRONT_LEFT"}

# Data request/delivery endpoints. {type} is the request kind:
# "partial" = continuous (15-min datasets), "all" = one-off full export.
# The delivery endpoints additionally require a "type" header with the
# same value; identifier comes from the metadata response.
METADATA_PATH = "/proxy_api/euda-apim/datarequest/vehicles/{vin}/metadata/{type}"
LIST_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/list"
DOWNLOAD_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/download"

# Datasets with this suffix carry no payload.
NO_CONTENT_SUFFIX = "_no_content_found.zip"

# The euda-apim endpoints fail in waves with HTTP 500 (the official web
# UI is affected too); observed outage windows last 5-6 minutes, so the
# one-shot fetch retry span must comfortably exceed that.
RETRY_ATTEMPTS = 8
RETRY_DELAY_SECONDS = 45.0

# Continuous watch polls at a fixed cadence; each poll is one uniform
# availability sample (and its own liveness proof). The data drops every
# ~15 min, but finer polling measures availability and delivery delay.
POLL_INTERVAL_SECONDS = 60.0

# The identity provider rejects obvious non-browser clients.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
