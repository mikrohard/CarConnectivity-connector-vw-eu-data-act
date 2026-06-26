"""Synchronous client for the VW EU Data Act portal (OIDC login + data delivery).

Ported from the homeassistant-vw-eu-data-act integration's ``api.py``, swapping
``aiohttp`` for a :class:`requests.Session`. The login flow and the workarounds
it relies on (building the OIDC authorize URL directly to bypass the broken AEM
servlet, the ``type: partial`` and ``traceid`` headers, stripping the duplicate
``relayState`` query) are kept verbatim in behaviour.
"""
from __future__ import annotations

import io
import json
import logging
import re
import uuid
import zipfile
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from carconnectivity_connectors.vw_eu_data_act.brands import resolve_brand

LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.vw_eu_data_act-api-debug")

# --- Portal / OIDC endpoints ----------------------------------------------
BASE_URL = "https://eu-data-act.drivesomethinggreater.com"
IDENTITY_BASE = "https://identity.vwgroup.io"

OIDC_AUTHORIZE_URL = IDENTITY_BASE + "/oidc/v1/authorize"
OIDC_CLIENT_ID = "9b58543e-1c15-4193-91d5-8a14145bebb0@apps_vw-dilab_com"
OIDC_SCOPE = "openid cars profile"
OIDC_REDIRECT_URI = BASE_URL + "/login"

# Defaults for the OIDC ``state`` (country__language__brand), echoed back to
# the portal callback. Overridable via connector config.
DEFAULT_COUNTRY = "si"
DEFAULT_LANGUAGE = "sl"
DEFAULT_BRAND = "VOLKSWAGEN_PASSENGER_CARS"

# proxy_api paths (relative to BASE_URL)
VEHICLES_PATH = "/proxy_api/consent/me/vehicles"
RELATION_PATH = "/proxy_api/vum/v2/users/me/relations/{vin}"
METADATA_PATH = "/proxy_api/euda-apim/datarequest/vehicles/{vin}/metadata/partial"
LIST_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/list"
DOWNLOAD_PATH = "/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/download"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# Files with this suffix carry no payload and are skipped.
NO_CONTENT_SUFFIX = "_no_content_found.zip"


class ApiError(Exception):
    """Generic API failure."""


class AuthError(ApiError):
    """Authentication failed or session expired."""


class _FormParser(HTMLParser):
    """Extract the first <form> action and all hidden/input fields."""

    def __init__(self) -> None:
        super().__init__()
        self.action: Optional[str] = None
        self.fields: Dict[str, str] = {}
        self._in_form = False
        self._done = False  # only capture the first form

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._done:
            return
        a = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = a.get("action")
            self._in_form = True
        elif tag == "input" and self._in_form:
            name = a.get("name")
            if name:
                self.fields[name] = a.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._in_form:
            self._in_form = False
            self._done = True


def _parse_form(html: str) -> _FormParser:
    p = _FormParser()
    p.feed(html)
    return p


def _extract_template_model(html: str) -> dict:
    """Extract the VW identity ``templateModel`` JSON embedded in the page.

    The signin/authenticate pages carry their form state (hmac, relayState,
    prefilled email, postAction, error) in a JS object rather than HTML inputs:

        window._IDK = { templateModel: { ... }, csrf_token: '...' }
    """
    idx = html.find("templateModel")
    if idx == -1:
        return {}
    brace = html.find("{", idx)
    if brace == -1:
        return {}
    depth = 0
    for i in range(brace, len(html)):
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[brace : i + 1])
                except ValueError:
                    return {}
    return {}


def _extract_csrf(html: str) -> Optional[str]:
    """Pull the csrf_token out of the identity page's JS."""
    m = re.search(r"csrf_token\s*[:=]\s*['\"]([^'\"]+)['\"]", html)
    return m.group(1) if m else None


def _login_fields(html: str) -> Tuple[Dict[str, str], Optional[str]]:
    """Collect the fields needed to POST a VW identity login step.

    Merges HTML hidden inputs with the JS templateModel/csrf so it works
    whether the page renders inputs server-side (email step) or via JS
    (password step). Returns (fields, form_action).
    """
    form = _parse_form(html)
    fields: Dict[str, str] = dict(form.fields)
    model = _extract_template_model(html)
    if model:
        for key in ("hmac", "relayState"):
            if model.get(key):
                fields[key] = model[key]
        email = (model.get("emailPasswordForm") or {}).get("email")
        if email:
            fields.setdefault("email", email)
    csrf = _extract_csrf(html)
    if csrf:
        fields.setdefault("_csrf", csrf)
    return fields, form.action


def _login_error(html: str) -> Optional[str]:
    """Return a human-readable login error from the page, if present."""
    model = _extract_template_model(html)
    err = model.get("error") or model.get("errorCode")
    if isinstance(err, dict):
        return err.get("text") or err.get("errorCode") or str(err)
    return str(err) if err else None


def _extract_vins(payload) -> List[dict]:
    """Best-effort extraction of vehicles from the (undocumented) vehicles body.

    Returns a list of {vin, nickname?} dicts. Walks the JSON for any 17-char
    VIN-like identifier so it is robust to wrapper shape ({vehicles:[]}, list…).
    """
    vins: Dict[str, dict] = {}

    def walk(node):
        if isinstance(node, dict):
            vin = node.get("vin") or node.get("vehicleIdentificationNumber")
            if isinstance(vin, str) and len(vin) == 17:
                vins.setdefault(vin, {"vin": vin})
                nick = node.get("vehicleNickname") or node.get("nickname") or node.get("modelName")
                if nick:
                    vins[vin]["nickname"] = nick
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return list(vins.values())


class EudaApiClient:
    """Authenticated synchronous client for the EU Data Act portal."""

    def __init__(self, email: str, password: str, *, country: str = DEFAULT_COUNTRY,
                 language: str = DEFAULT_LANGUAGE, brand: str = DEFAULT_BRAND,
                 timeout: int = 60) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        # Retry transient connection/5xx errors at the transport layer so a single
        # dropped connection (the portal/Azure blob occasionally closes idle
        # sockets) does not bubble up as a hard failure.
        retry = Retry(total=3, connect=3, read=3, backoff_factor=1.0,
                      status_forcelist=(500, 502, 503, 504),
                      allowed_methods=frozenset(["GET", "POST"]),
                      raise_on_status=False)
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        self._email = email
        self._password = password
        self._state = f"{country}__{language}__{brand}"
        # Each brand authenticates with its own OIDC client_id (the state alone is
        # not enough); resolve it from the brand key, defaulting to VW.
        self._client_id = resolve_brand(brand).client_id
        self._timeout = timeout
        self._logged_in = False

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    # -- authentication ----------------------------------------------------

    def login(self) -> None:
        """Run the full OIDC login, populating the session cookie jar."""
        try:
            self._do_login()
        except requests.RequestException as err:
            raise ApiError(f"Network error during login: {err}") from err
        self._logged_in = True

    def _do_login(self) -> None:
        # 0. Prime the portal session (the browser loads the site first; this
        #    sets the AEM load-balancer/session cookies the callback needs).
        try:
            self._session.get(f"{BASE_URL}/", timeout=self._timeout)
        except requests.RequestException as err:
            LOG.debug("login step0: priming GET failed (ignored): %s", err)

        # 1. Start the OIDC flow directly at the identity provider. We build the
        #    authorize URL ourselves because the portal's
        #    /services/redirect/authentication servlet returns HTTP 500 for
        #    non-browser clients (it depends on AEM browser session state).
        authorize_url = self._build_authorize_url()
        LOG.debug("login step1: authorize url = %s", authorize_url)
        resp = self._session.get(authorize_url, timeout=self._timeout)
        signin_url = resp.url
        signin_html = resp.text
        LOG.debug("login step2: signin page = %s (%d bytes)", signin_url, len(signin_html))

        # 2. POST the email (identifier step). Fields come from HTML inputs
        #    and/or the JS templateModel (hmac, _csrf, relayState).
        fields, action = _login_fields(signin_html)
        LOG.debug("login step2: action=%s fields=%s", action, sorted(fields))
        if "hmac" not in fields or "_csrf" not in fields:
            raise AuthError(f"Could not parse the sign-in form (fields found: {sorted(fields)})")
        fields["email"] = self._email
        identifier_action = urljoin(signin_url, action or "")
        resp = self._session.post(
            identifier_action,
            data=fields,
            headers={"Referer": signin_url},
            timeout=self._timeout,
        )
        authenticate_url = resp.url
        authenticate_html = resp.text
        LOG.debug("login step3: after identifier POST status=%s url=%s", resp.status_code, authenticate_url)

        # 3. The identifier step lands on the password (authenticate) page,
        #    whose hidden fields live in the JS templateModel, not HTML inputs.
        fields2, action2 = _login_fields(authenticate_html)
        LOG.debug("login step3: action=%s fields=%s", action2, sorted(fields2))
        if "hmac" not in fields2 or "_csrf" not in fields2:
            err = _login_error(authenticate_html)
            raise AuthError(
                err
                or "Identity portal did not return the password form - check the "
                "email address (or the login flow changed)"
            )
        fields2["email"] = self._email
        fields2["password"] = self._password
        # The browser posts to the clean /login/authenticate URL with relayState
        # in the body; posting to authenticate_url (which carries ?relayState=)
        # duplicates it and is rejected with HTTP 400. Strip the query.
        if action2:
            authenticate_action = urljoin(authenticate_url, action2)
        else:
            authenticate_action = authenticate_url.split("?", 1)[0]
        LOG.debug("login step4: POST credentials to %s", authenticate_action)

        # 4. POST credentials; follow the redirect chain back to the portal,
        #    which sets the session cookies via /services/callbacklogin.
        resp = self._session.post(
            authenticate_action,
            data=fields2,
            headers={"Referer": authenticate_url},
            timeout=self._timeout,
        )
        landing = resp.url
        landing_html = resp.text
        if resp.status_code >= 400:
            LOG.debug("login step4: HTTP %s body[:500]=%s", resp.status_code, landing_html[:500])
            err = _login_error(landing_html)
            raise AuthError(err or f"Login rejected (HTTP {resp.status_code})")
        LOG.debug("login step4: landed on %s", landing)

        # Positively confirm success: a completed flow lands back on the portal
        # host (via /services/callbacklogin). Bad credentials re-render the
        # identity sign-in page (URL still on identity.vwgroup.io/signin-service).
        portal_host = urlparse(BASE_URL).netloc
        if "signin-service" in landing or "/error" in landing:
            raise AuthError("Login failed - check email and password")
        if urlparse(landing).netloc != portal_host:
            raise AuthError(f"Login did not complete (ended at {landing})")

    def _build_authorize_url(self) -> str:
        """Construct the OIDC authorize URL (bypasses the broken AEM servlet)."""
        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "scope": OIDC_SCOPE,
            "state": self._state,
            "redirect_uri": OIDC_REDIRECT_URI,
            "prompt": "login",
        }
        return f"{OIDC_AUTHORIZE_URL}?{urlencode(params)}"

    def ensure_login(self) -> None:
        """Log in if not already authenticated."""
        if not self._logged_in:
            self.login()

    # -- authenticated requests -------------------------------------------

    def _session_get(self, url: str, *, headers: Optional[dict] = None):
        """GET wrapper that translates transport errors into ApiError.

        Transient network failures (dropped connections, timeouts, DNS) must be
        surfaced as ApiError so the connector's background loop retries on its
        interval instead of crashing the worker thread.
        """
        try:
            return self._session.get(url, headers=headers, timeout=self._timeout)
        except requests.RequestException as err:
            raise ApiError(f"Network error for GET {url}: {err}") from err

    def _get_json(self, url: str, *, headers: Optional[dict] = None, _retry: bool = True):
        resp = self._session_get(url, headers=headers)
        if resp.status_code in (401, 403) and _retry:
            LOG.debug("Session expired (%s) for %s; re-authenticating", resp.status_code, url)
            self._logged_in = False
            self.login()
            return self._get_json(url, headers=headers, _retry=False)
        if resp.status_code >= 400:
            raise ApiError(f"GET {url} -> HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as err:
            raise ApiError(f"Invalid JSON from {url}: {err}") from err

    def list_vehicles(self) -> List[dict]:
        """Return [{vin, nickname?}] for vehicles consented on the portal."""
        self.ensure_login()
        payload = self._get_json(f"{BASE_URL}{VEHICLES_PATH}?viewPosition=FRONT_LEFT")
        vehicles = _extract_vins(payload)
        # Enrich with the friendly vehicleNickname from the relation endpoint
        # (the authoritative source, e.g. "ID.3").
        for veh in vehicles:
            try:
                rel = self.get_relation(veh["vin"])
                nickname = (rel.get("relation") or {}).get("vehicleNickname")
                LOG.debug("relation for %s: nickname=%r", veh["vin"], nickname)
                if nickname:
                    veh["nickname"] = nickname
            except ApiError as err:
                LOG.debug("Could not fetch nickname for %s: %s", veh["vin"], err)
        return vehicles

    def get_relation(self, vin: str) -> dict:
        """Return the user<->vehicle relation (carries vehicleNickname)."""
        self.ensure_login()
        # The relation endpoint requires a traceid header; HTTP 400 without one.
        headers = {"traceid": f"vehicle-relation-fetch-{uuid.uuid4()}"}
        return self._get_json(f"{BASE_URL}{RELATION_PATH.format(vin=vin)}", headers=headers)

    def get_metadata(self, vin: str) -> dict:
        """Return the data-request metadata; ``Identifier`` is needed downstream."""
        self.ensure_login()
        return self._get_json(f"{BASE_URL}{METADATA_PATH.format(vin=vin)}")

    def list_datasets(self, vin: str, identifier: str) -> List[dict]:
        """Return the rolling list of available zips: [{name, createdOn, size}]."""
        self.ensure_login()
        url = f"{BASE_URL}{LIST_PATH.format(vin=vin, identifier=identifier)}"
        # The list endpoint requires the data-request type header (matching
        # metadata/partial); without it the backend returns HTTP 500.
        data = self._get_json(url, headers={"type": "partial"})
        return data if isinstance(data, list) else data.get("files", [])

    def download_dataset(self, vin: str, identifier: str, name: str) -> dict:
        """Download a specific zip by name and return the parsed JSON inside it."""
        self.ensure_login()
        if name.endswith(NO_CONTENT_SUFFIX):
            raise ApiError(f"{name} contains no content")
        url = f"{BASE_URL}{DOWNLOAD_PATH.format(vin=vin, identifier=identifier)}"
        headers = {"filename": name, "type": "partial"}
        resp = self._session_get(url, headers=headers)
        if resp.status_code in (401, 403):
            self._logged_in = False
            self.login()
            resp = self._session_get(url, headers=headers)
        if resp.status_code >= 400:
            raise ApiError(f"Download {name} -> HTTP {resp.status_code}")
        return self._unzip_json(resp.content, name)

    @staticmethod
    def _unzip_json(raw: bytes, name: str) -> dict:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                members = [n for n in zf.namelist() if n.lower().endswith(".json")]
                if not members:
                    raise ApiError(f"No JSON inside {name}")
                with zf.open(members[0]) as fh:
                    return json.loads(fh.read().decode("utf-8"))
        except (zipfile.BadZipFile, ValueError) as err:
            raise ApiError(f"Could not read {name}: {err}") from err
