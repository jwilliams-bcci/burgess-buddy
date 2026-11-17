import os
import time
import json
import base64
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv

load_dotenv(override=True)


def _decode_jwt_exp(token: str) -> float:
    """Decodes the 'exp' claim from a JWT payload without signature verification."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return time.time() + 3600
        
        payload_b64 = parts[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        decoded_bytes = base64.urlsafe_b64decode(padded)
        payload = json.loads(decoded_bytes.decode("utf-8"))
        
        return float(payload.get("exp", time.time() + 3600))
    except Exception:
        return time.time() + 3600


class InternalPortalClient:
    """Client for internal portal APIs with automatic Bearer token management."""

    def __init__(self):
        raw_url = os.getenv("INTERNAL_PORTAL_API_URL") or os.getenv("BURGESS_API_BASE_URL") or ""
        self.base_url = raw_url.rstrip("/")
        self.client_id = os.getenv("INTERNAL_API_CLIENT_ID")
        self.client_secret = os.getenv("INTERNAL_API_CLIENT_SECRET")
        
        self._token = None
        self._token_expires_at = 0

    def _get_valid_token(self) -> str:
        """Fetches or reuses a valid Bearer token."""
        now = time.time()
        
        if self._token and now < (self._token_expires_at - 60):
            return self._token

        auth_url = f"{self.base_url}/Account/Login"
        payload = {
            "email": self.client_id,
            "password": self.client_secret
        }
        
        response = requests.post(auth_url, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()

        if not data.get("isSuccess"):
            raise PermissionError(f"Authentication failed: {data.get('message', 'Unknown error')}")

        self._token = data.get("token")

        exp_str = data.get("expirationTime")
        if exp_str:
            if exp_str.endswith("Z"):
                exp_str = exp_str[:-1] + "+00:00"
            exp_dt = datetime.fromisoformat(exp_str)
            self._token_expires_at = exp_dt.timestamp()
        else:
            self._token_expires_at = now + 3600

        return self._token

    def get(self, endpoint: str, params: dict = None) -> dict:
        """Executes GET requests using an active Bearer token."""
        try:
            token = self._get_valid_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            url = f"{self.base_url}/{endpoint.lstrip('/')}"
            res = requests.get(url, headers=headers, params=params, timeout=10)
            res.raise_for_status()
            return res.json()
        except requests.exceptions.RequestException as err:
            return {"error": f"Internal Portal API Error: {str(err)}"}

    def post(self, endpoint: str, json_data: dict = None) -> dict:
        """Executes POST requests using an active Bearer token."""
        try:
            token = self._get_valid_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            url = f"{self.base_url}/{endpoint.lstrip('/')}"
            res = requests.post(url, headers=headers, json=json_data, timeout=10)
            res.raise_for_status()
            return res.json()
        except requests.exceptions.RequestException as err:
            return {"error": f"Internal Portal API Error: {str(err)}"}


class ExternalAppsClient:
    """Client for external mobile and customer-facing APIs."""

    def __init__(self):
        self.base_url = os.getenv("EXTERNAL_APPS_API_URL", "").rstrip("/")
        self.client_id = os.getenv("EXTERNAL_API_CLIENT_ID")
        self.client_secret = os.getenv("EXTERNAL_API_CLIENT_SECRET")
        
        self._token = None
        self._token_expires_at = 0

    def _get_valid_token(self) -> str:
        now = time.time()
        
        if self._token and now < (self._token_expires_at - 60):
            return self._token

        auth_url = f"{self.base_url}/login"
        payload = {
            "username": self.client_id,
            "password": self.client_secret
        }
        
        response = requests.post(auth_url, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()

        self._token = data.get("token")
        if not self._token:
            raise PermissionError("External Apps Auth failed: No token returned in response.")

        self._token_expires_at = _decode_jwt_exp(self._token)

        return self._token

    def get(self, endpoint: str, params: dict = None) -> dict:
        try:
            token = self._get_valid_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            url = f"{self.base_url}/{endpoint.lstrip('/')}"
            res = requests.get(url, headers=headers, params=params, timeout=10)
            res.raise_for_status()
            return res.json()
        except requests.exceptions.RequestException as err:
            return {"error": f"External Apps API Error: {str(err)}"}

    def post(self, endpoint: str, json_data: dict = None) -> dict:
        try:
            token = self._get_valid_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            url = f"{self.base_url}/{endpoint.lstrip('/')}"
            res = requests.post(url, headers=headers, json=json_data, timeout=10)
            res.raise_for_status()
            return res.json()
        except requests.exceptions.RequestException as err:
            return {"error": f"External Apps API Error: {str(err)}"}