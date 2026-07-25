"""Client for an Enphase Envoy's local production API.

Newer Envoy firmware requires a long-lived access token (generated via the
Enlighten portal / entrez.enphaseenergy.com, tied to the Envoy's serial
number) exchanged for a local session cookie - a bare Authorization header
per request is not enough on these firmwares. See README for how to
generate one.
"""

from __future__ import annotations

import logging

import requests
import urllib3

logger = logging.getLogger(__name__)

# The Envoy's local HTTPS uses a self-signed certificate.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class EnvoyAuthError(RuntimeError):
    pass


class EnvoyClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._session = requests.Session()
        self._session.verify = False
        self._authenticated = False

    def _authenticate(self) -> None:
        resp = self._session.post(
            f"{self.base_url}/auth/check_jwt",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout,
        )
        if resp.status_code == 401:
            raise EnvoyAuthError("Envoy rejected the token - it may be expired or wrong")
        resp.raise_for_status()
        self._authenticated = True

    def production(self) -> dict:
        """Return the parsed /production.json payload, (re)authenticating as needed."""
        if not self._authenticated:
            self._authenticate()

        resp = self._session.get(f"{self.base_url}/production.json", timeout=self.timeout)
        if resp.status_code == 401:
            self._authenticated = False
            self._authenticate()
            resp = self._session.get(f"{self.base_url}/production.json", timeout=self.timeout)

        resp.raise_for_status()
        return resp.json()
