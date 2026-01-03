import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
import requests

from .extensions import UPLOAD_DIR

DA_BASE = "https://www.donationalerts.com"
DA_API_BASE = f"{DA_BASE}/api/v1"
DA_AUTHORIZE_URL = f"{DA_BASE}/oauth/authorize"
DA_TOKEN_URL = f"{DA_BASE}/oauth/token"
DA_USER_OAUTH_URL = f"{DA_API_BASE}/user/oauth"
DA_DONATIONS_URL = f"{DA_API_BASE}/alerts/donations"


def _token_store_path() -> str:
    return os.getenv("DA_TOKEN_STORE", os.path.join(UPLOAD_DIR, "da_oauth.json"))


def load_tokens() -> Dict[str, Any]:
    path = _token_store_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_tokens(data: Dict[str, Any]) -> None:
    path = _token_store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _client_credentials() -> Tuple[str, str, str]:
    cid = str(os.getenv("DA_CLIENT_ID", "")).strip()
    secret = str(os.getenv("DA_CLIENT_SECRET", "")).strip()
    redirect = str(os.getenv("DA_REDIRECT_URI", "")).strip()
    if not cid or not secret or not redirect:
        raise RuntimeError("DonationAlerts OAuth is not configured (DA_CLIENT_ID/DA_CLIENT_SECRET/DA_REDIRECT_URI)")
    return cid, secret, redirect


def build_authorize_url(state: str, scopes: str) -> str:
    cid, _, redirect = _client_credentials()
    # DonationAlerts expects scope as space-delimited list
    params = {
        "client_id": cid,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": scopes,
        "state": state,
    }
    from urllib.parse import urlencode
    return f"{DA_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code: str) -> Dict[str, Any]:
    cid, secret, redirect = _client_credentials()
    # DonationAlerts uses query string parameters, but form-encoded body works fine too.
    payload = {
        "grant_type": "authorization_code",
        "client_id": cid,
        "client_secret": secret,
        "redirect_uri": redirect,
        "code": code,
    }
    resp = requests.post(DA_TOKEN_URL, data=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    # Normalize timestamps
    now = int(time.time())
    expires_in = int(data.get("expires_in") or 0)
    if expires_in:
        data["expires_at"] = now + expires_in - 30  # 30s safety margin
    return data


def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    cid, secret, redirect = _client_credentials()
    payload = {
        "grant_type": "refresh_token",
        "client_id": cid,
        "client_secret": secret,
        "redirect_uri": redirect,
        "refresh_token": refresh_token,
    }
    resp = requests.post(DA_TOKEN_URL, data=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    now = int(time.time())
    expires_in = int(data.get("expires_in") or 0)
    if expires_in:
        data["expires_at"] = now + expires_in - 30
    return data


def get_valid_access_token() -> str:
    tokens = load_tokens()
    access = str(tokens.get("access_token") or "").strip()
    refresh = str(tokens.get("refresh_token") or "").strip()
    expires_at = int(tokens.get("expires_at") or 0)

    now = int(time.time())
    if access and (not expires_at or now < expires_at):
        return access

    if not refresh:
        raise RuntimeError("DonationAlerts is not connected yet (missing refresh_token). Open /da/connect as admin.")

    new_tokens = refresh_access_token(refresh)
    # Keep refresh_token if API didn't return one
    if not new_tokens.get("refresh_token"):
        new_tokens["refresh_token"] = refresh
    tokens.update(new_tokens)
    save_tokens(tokens)
    return str(tokens.get("access_token"))


def fetch_user_oauth(access_token: str) -> Dict[str, Any]:
    resp = requests.get(
        DA_USER_OAUTH_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def list_donations(access_token: str, page: int = 1) -> Dict[str, Any]:
    resp = requests.get(
        DA_DONATIONS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"page": page},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()
