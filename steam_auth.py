#!/usr/bin/env python3
"""Steam authentication helper for the library extractor.

Runs once (or when Steam revokes the token) and stores a long-lived refresh
token in ``auth.json``.  After that, ``library_extractor.py`` can mint a fresh
Steam web session without any human input.

Preferred flow is QR login: the script prints a QR code (and writes a PNG),
the user scans it with the Steam mobile app and approves.  No password is
typed and no Steam Guard code is needed.

Fallback flow is account name + password + Steam Guard code, for accounts
that do not have the mobile app.

The refresh token is a bearer credential: keep ``auth.json`` private.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

import requests

API_BASE = "https://api.steampowered.com"
LOGIN_BASE = "https://login.steampowered.com"
STORE_BASE = "https://store.steampowered.com"
COMMUNITY_BASE = "https://steamcommunity.com"

DEFAULT_AUTH_FILE = "auth.json"
DEFAULT_QR_PNG = "steam_login_qr.png"
USER_AGENT = "steam-library-extractor/2.0"

# EAuthTokenPlatformType
PLATFORM_WEB = 2

# EAuthSessionGuardType
GUARD_NONE = 1
GUARD_EMAIL_CODE = 2
GUARD_DEVICE_CODE = 3
GUARD_DEVICE_CONFIRMATION = 4
GUARD_EMAIL_CONFIRMATION = 5
GUARD_MACHINE_TOKEN = 6


class AuthError(Exception):
    """Authentication failed or could not be completed."""


class AuthExpired(AuthError):
    """The stored refresh token is no longer valid; a new login is required."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean_text(text: str, limit: int = 200) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
    )
    return session


def _api_url(service: str, method: str, version: str = "v1") -> str:
    return f"{API_BASE}/{service}/{method}/{version}/"


def _read_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise AuthError(
            "Steam returned a non-JSON response: "
            f"{_clean_text(response.text)}"
        ) from exc


def _request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    data: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: tuple = (10, 30),
) -> Any:
    try:
        response = session.request(
            method, url, params=params, data=data, headers=headers, timeout=timeout
        )
    except requests.RequestException as exc:
        raise AuthError(f"Network error contacting Steam: {exc}") from exc

    if response.status_code == 401:
        raise AuthError(
            "Steam rejected the credentials (HTTP 401). "
            "Check the account name/password, or use QR login."
        )
    if response.status_code == 403:
        raise AuthError(
            "Steam denied access (HTTP 403). Try again later or use QR login."
        )
    if response.status_code == 429:
        raise AuthError(
            "Steam rate-limited the login attempt (HTTP 429). "
            "Wait a few minutes and try again."
        )
    if response.status_code >= 400:
        raise AuthError(
            f"Steam returned HTTP {response.status_code}: "
            f"{_clean_text(response.text)}"
        )

    return _read_json(response)


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_jwt(token: str) -> dict:
    """Return a JWT payload without verifying the signature."""
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def steamid_from_token(token: str) -> Optional[str]:
    """Extract the SteamID64 from a Steam JWT (``sub`` claim)."""
    subject = decode_jwt(token).get("sub")
    if subject is not None and str(subject).isdigit():
        return str(subject)
    return None


def token_expiry(token: str) -> Optional[datetime]:
    expires = decode_jwt(token).get("exp")
    if isinstance(expires, (int, float)):
        return datetime.fromtimestamp(expires, tz=timezone.utc)
    return None


def _expiry_iso(token: str) -> Optional[str]:
    expiry = token_expiry(token)
    return expiry.replace(microsecond=0).isoformat() if expiry else None


def is_refresh_token(token: str) -> bool:
    """Best-effort check that a token is a long-lived Steam refresh token."""
    payload = decode_jwt(token)
    audience = payload.get("aud")
    if isinstance(audience, str):
        audience = [audience]
    if isinstance(audience, list):
        # Steam refresh tokens carry the "derive" audience; access tokens do not.
        return "derive" in audience
    expires = payload.get("exp")
    return isinstance(expires, (int, float)) and expires > time.time() + 86400


def _is_durable_token(token: str, steamid: str) -> bool:
    """Check for a long-lived token belonging to the same account."""
    payload = decode_jwt(token)
    if str(payload.get("sub")) != str(steamid):
        return False
    expires = payload.get("exp")
    return isinstance(expires, (int, float)) and expires > time.time() + 30 * 86400


# --------------------------------------------------------------------------
# Login primitives
# --------------------------------------------------------------------------


def get_rsa_key(session: requests.Session, account_name: str) -> dict:
    """Fetch the RSA key Steam uses to encrypt the password."""
    last_error: Optional[Exception] = None
    for attempt in range(6):
        try:
            payload = _request(
                session,
                "GET",
                _api_url("IAuthenticationService", "GetPasswordRSAPublicKey"),
                params={"account_name": account_name},
            )
        except AuthError as exc:
            last_error = exc
            time.sleep(1 + attempt)
            continue

        response = payload.get("response") or {}
        if all(
            key in response
            for key in ("publickey_mod", "publickey_exp", "timestamp")
        ):
            return response
        last_error = AuthError(
            "Steam did not return an RSA key. Check the account name."
        )
        time.sleep(1 + attempt)

    raise AuthError(str(last_error) if last_error else "Could not obtain RSA key.")


def encrypt_password(password: str, modulus_hex: str, exponent_hex: str) -> str:
    """RSA-encrypt a password the way the Steam login page does."""
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    modulus = int(modulus_hex, 16)
    exponent = int(exponent_hex, 16)
    public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
    encrypted = public_key.encrypt(password.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("ascii")


def _parse_begin_response(payload: Any, label: str) -> dict:
    if not isinstance(payload, dict):
        raise AuthError(f"Steam returned an unexpected {label} response.")
    if payload.get("error"):
        raise AuthError(f"Steam rejected the {label}: {payload['error']}")

    response = payload.get("response") or {}
    if not response.get("client_id"):
        if payload.get("captcha_needed") or response.get("captcha_needed"):
            raise AuthError(
                "Steam requires a captcha for password login. "
                "Use QR login (`python steam_auth.py`) instead."
            )
        message = (
            response.get("extended_error_message")
            or payload.get("message")
            or "unknown error"
        )
        raise AuthError(f"Steam rejected the {label}: {message}")

    response.setdefault("interval", 5)
    return response


def begin_credentials(
    session: requests.Session, account_name: str, password: str
) -> dict:
    key = get_rsa_key(session, account_name)
    data = {
        "account_name": account_name,
        "encrypted_password": encrypt_password(
            password, key["publickey_mod"], key["publickey_exp"]
        ),
        "encryption_timestamp": key["timestamp"],
        "persistence": 1,  # ESessionPersistence.Persistent
        "platform_type": PLATFORM_WEB,
        "website_id": "Store",
        "device_friendly_name": "steam-library-extractor",
    }
    payload = _request(
        session,
        "POST",
        _api_url("IAuthenticationService", "BeginAuthSessionViaCredentials"),
        data=data,
    )
    return _parse_begin_response(payload, "password login")


def begin_qr(session: requests.Session) -> dict:
    data = {
        "device_friendly_name": "steam-library-extractor",
        "platform_type": PLATFORM_WEB,
        "website_id": "Store",
    }
    payload = _request(
        session,
        "POST",
        _api_url("IAuthenticationService", "BeginAuthSessionViaQR"),
        data=data,
    )
    return _parse_begin_response(payload, "QR login")


def confirmation_types(begin_response: dict) -> list[int]:
    types = []
    for confirmation in begin_response.get("allowed_confirmations") or []:
        value = confirmation.get("confirmation_type")
        try:
            types.append(int(value))
        except (TypeError, ValueError):
            continue
    return types


def submit_guard_code(
    session: requests.Session, begin_response: dict, code: str, code_type: int
) -> None:
    data = {
        "client_id": begin_response["client_id"],
        "steamid": begin_response.get("steamid"),
        "code": code,
        "code_type": code_type,
    }
    payload = _request(
        session,
        "POST",
        _api_url("IAuthenticationService", "UpdateAuthSessionWithSteamGuardCode"),
        data=data,
    )
    if payload.get("error"):
        raise AuthError(f"Steam rejected the Guard code: {payload['error']}")


def handle_password_confirmations(
    session: requests.Session, begin_response: dict, code: Optional[str] = None
) -> None:
    types = confirmation_types(begin_response)

    if GUARD_DEVICE_CODE in types or GUARD_EMAIL_CODE in types:
        use_device_code = GUARD_DEVICE_CODE in types
        if code is None:
            if not sys.stdin.isatty():
                raise AuthError(
                    "Steam Guard code required. Re-run in a terminal with --code."
                )
            prompt = (
                "Steam Guard code from the mobile app: "
                if use_device_code
                else "Steam Guard email code: "
            )
            code = input(prompt).strip()
        submit_guard_code(
            session,
            begin_response,
            code,
            GUARD_DEVICE_CODE if use_device_code else GUARD_EMAIL_CODE,
        )
        return

    if GUARD_DEVICE_CONFIRMATION in types or GUARD_EMAIL_CONFIRMATION in types:
        print(
            "Approve the login in the Steam mobile app (or via the email link)...",
            file=sys.stderr,
        )
        return

    if GUARD_MACHINE_TOKEN in types:
        raise AuthError(
            "This account requires a trusted-device token. "
            "Use QR login (`python steam_auth.py`) instead."
        )

    if types and GUARD_NONE not in types:
        raise AuthError(
            "This account uses an unsupported Steam Guard method "
            f"(confirmation types: {types}). Use QR login instead."
        )


def poll_auth(
    session: requests.Session,
    begin_response: dict,
    *,
    timeout: int = 180,
    on_new_challenge=None,
    quiet: bool = False,
) -> dict:
    client_id = begin_response["client_id"]
    request_id = begin_response["request_id"]
    interval = max(1.0, float(begin_response.get("interval") or 5))
    deadline = time.monotonic() + timeout
    last_notice = 0.0

    while time.monotonic() < deadline:
        payload = _request(
            session,
            "POST",
            _api_url("IAuthenticationService", "PollAuthSessionStatus"),
            data={"client_id": client_id, "request_id": request_id},
        )
        if not isinstance(payload, dict):
            raise AuthError("Steam returned an unexpected login response.")
        response = payload.get("response") or {}

        if response.get("refresh_token"):
            return response

        if response.get("new_client_id"):
            client_id = response["new_client_id"]
        if response.get("new_challenge_url") and on_new_challenge:
            on_new_challenge(response["new_challenge_url"])

        if payload.get("error"):
            raise AuthError(f"Steam login failed: {payload['error']}")

        if not quiet and time.monotonic() - last_notice > 30:
            print("Waiting for Steam approval...", file=sys.stderr)
            last_notice = time.monotonic()

        time.sleep(interval)

    raise AuthError(
        "Timed out waiting for Steam approval. Run the login again."
    )


def render_qr(
    url: str,
    png_path: Optional[str] = DEFAULT_QR_PNG,
    *,
    print_ascii: bool = True,
) -> None:
    """Print the login URL as an ASCII QR code and save a PNG copy."""
    try:
        import qrcode
    except ImportError:
        print(f"Open this URL with the Steam mobile app: {url}")
        return

    qr = qrcode.QRCode(border=2, box_size=1)
    qr.add_data(url)
    qr.make(fit=True)

    if print_ascii:
        try:
            qr.print_ascii(invert=True)
        except Exception:  # noqa: BLE001,S110 - terminal fonts vary
            pass

    if png_path:
        try:
            # A larger module size keeps the PNG scannable on a phone.
            image_qr = qrcode.QRCode(border=4, box_size=10)
            image_qr.add_data(url)
            image_qr.make(fit=True)
            image = image_qr.make_image(
                fill_color="black", back_color="white"
            )
            image.save(png_path)
            print(f"QR code saved to {png_path}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - Pillow/qrcode errors vary
            print(f"Could not save the QR image: {exc}", file=sys.stderr)

    print(f"QR URL: {url}", file=sys.stderr)


# --------------------------------------------------------------------------
# Session minting
# --------------------------------------------------------------------------


def finalize_login(
    session: requests.Session, refresh_token: str, steamid: Optional[str] = None
) -> str:
    """Exchange a refresh token for fresh Steam web session cookies."""
    session_id = secrets.token_hex(12)
    for domain in (
        "store.steampowered.com",
        "steamcommunity.com",
        "help.steampowered.com",
    ):
        session.cookies.set("sessionid", session_id, domain=domain)

    login_headers = {
        "Origin": COMMUNITY_BASE,
        "Referer": COMMUNITY_BASE + "/",
    }
    try:
        payload = _request(
            session,
            "POST",
            LOGIN_BASE + "/jwt/finalizelogin",
            data={
                "nonce": refresh_token,
                "sessionid": session_id,
                "redir": COMMUNITY_BASE + "/login/home/?goto=",
            },
            headers=login_headers,
        )
    except AuthError as exc:
        message = str(exc)
        if "HTTP 401" in message or "HTTP 403" in message:
            raise AuthExpired(
                "Steam rejected the refresh token. "
                "Run `python steam_auth.py` to log in again."
            ) from exc
        raise

    if not isinstance(payload, dict):
        raise AuthExpired(
            "Steam returned an unexpected login response. "
            "Run `python steam_auth.py` to log in again."
        )

    if payload.get("error"):
        error = payload["error"]
        if str(error) == "15":
            raise AuthExpired(
                "Steam refresh token expired or was revoked. "
                "Run `python steam_auth.py` to log in again."
            )
        raise AuthExpired(
            "Steam rejected the refresh token "
            f"({error}). Run `python steam_auth.py` to log in again."
        )

    transfer_info = payload.get("transfer_info") or []
    if not transfer_info:
        raise AuthExpired(
            "Steam did not return a web session. The refresh token may have "
            "expired. Run `python steam_auth.py` to log in again."
        )

    steamid = str(payload.get("steamID") or steamid or "")
    for transfer in transfer_info:
        url = transfer.get("url")
        params = dict(transfer.get("params") or {})
        if steamid:
            params["steamID"] = steamid
        if not url:
            continue
        try:
            response = session.post(
                url, data=params, headers=login_headers, timeout=(10, 30)
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise AuthError(f"Could not finalize the Steam login: {exc}") from exc

    if not steamid:
        # Fall back to the steamLoginSecure cookie: "steamid||access_token".
        cookie = _cookie_value(
            session, "steamLoginSecure", "store.steampowered.com"
        )
        if cookie and "||" in cookie:
            steamid = cookie.split("||", 1)[0]

    return steamid


def _find_webapi_token(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        token = value.get("webapi_token")
        if isinstance(token, str) and token:
            return token
        for nested in value.values():
            found = _find_webapi_token(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_webapi_token(nested)
            if found:
                return found
    return None


def get_webapi_token(session: requests.Session) -> str:
    payload = _request(
        session,
        "GET",
        STORE_BASE + "/pointssummary/ajaxgetasyncconfig",
        headers={
            "Referer": STORE_BASE + "/",
            "Origin": STORE_BASE,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    token = _find_webapi_token(payload)
    if not token:
        raise AuthError(
            "Steam did not return a webapi_token. The store session was not "
            "authenticated. Run `python steam_auth.py` again."
        )
    return token


def _cookie_value(
    session: requests.Session, name: str, domain: Optional[str] = None
) -> Optional[str]:
    fallback = None
    for cookie in session.cookies:
        if cookie.name != name or not cookie.value:
            continue
        value = unquote(cookie.value)
        if domain is None or (cookie.domain or "").endswith(domain):
            return value
        fallback = value
    return fallback


def mint_web_token(
    auth: dict, auth_file: str = DEFAULT_AUTH_FILE
) -> tuple[str, requests.Session, str]:
    """Return ``(webapi_token, store_session, steamid)`` for an auth record."""
    refresh_token = auth.get("refresh_token")
    if not refresh_token:
        raise AuthError("auth file has no refresh_token. Run `python steam_auth.py`.")
    steamid = str(
        auth.get("steamid") or steamid_from_token(refresh_token) or ""
    )
    if not steamid:
        raise AuthError("auth file has no SteamID. Run `python steam_auth.py`.")

    session = new_session()
    finalize_login(session, refresh_token, steamid)

    # Steam may rotate the refresh token when finalizing a web session.
    rotated = _cookie_value(
        session, "steamRefresh_steam", "store.steampowered.com"
    )
    if (
        rotated
        and rotated != refresh_token
        and (
            is_refresh_token(rotated)
            or _is_durable_token(rotated, steamid)
        )
    ):
        auth["refresh_token"] = rotated
        auth["refresh_token_expires_at"] = _expiry_iso(rotated)
        try:
            save_auth(auth, auth_file)
        except OSError as exc:
            print(
                f"Warning: could not save the rotated refresh token: {exc}",
                file=sys.stderr,
            )

    try:
        token = get_webapi_token(session)
    except AuthError as exc:
        message = str(exc)
        if "HTTP 401" in message or "HTTP 403" in message:
            raise AuthExpired(
                "Steam rejected the store session. "
                "Run `python steam_auth.py` to log in again."
            ) from exc
        raise
    return token, session, steamid


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def save_auth(auth: dict, path: str = DEFAULT_AUTH_FILE) -> Path:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": 1,
        "refresh_token": auth["refresh_token"],
        "steamid": str(auth["steamid"]),
        "account_name": auth.get("account_name"),
        "created_at": auth.get("created_at") or _now_iso(),
        "refresh_token_expires_at": auth.get("refresh_token_expires_at")
        or _expiry_iso(auth["refresh_token"]),
    }

    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError:
        os.replace(temporary, path)
    return path


def load_auth(path: str = DEFAULT_AUTH_FILE) -> Optional[dict]:
    path = Path(path).expanduser()
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthError(f"Could not read {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise AuthError(
            f"{path} is not a valid auth file. Run `python steam_auth.py`."
        )

    if not payload.get("refresh_token") or not payload.get("steamid"):
        raise AuthError(
            f"{path} is missing refresh_token/steamid. "
            "Run `python steam_auth.py` to create it."
        )

    try:
        if path.stat().st_mode & 0o077:
            os.chmod(path, 0o600)
            print(
                f"Warning: tightened permissions on {path} to 600.",
                file=sys.stderr,
            )
    except OSError:
        pass

    expires_at = payload.get("refresh_token_expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry < datetime.now(timezone.utc):
                raise AuthExpired(
                    f"Steam refresh token expired on {expires_at}. "
                    "Run `python steam_auth.py` to log in again."
                )
        except (ValueError, TypeError):
            pass

    return payload


# --------------------------------------------------------------------------
# One-time login flow
# --------------------------------------------------------------------------


def run_login(
    auth_file: str = DEFAULT_AUTH_FILE,
    *,
    use_qr: bool = True,
    account: Optional[str] = None,
    password: Optional[str] = None,
    code: Optional[str] = None,
    timeout: int = 180,
    qr_png: Optional[str] = DEFAULT_QR_PNG,
    quiet: bool = False,
    verify: bool = False,
) -> dict:
    """Run the interactive login and save the refresh token."""
    session = new_session()
    begin: Optional[dict] = None

    if use_qr:
        try:
            begin = begin_qr(session)
        except AuthError as exc:
            print(f"QR login unavailable: {exc}", file=sys.stderr)
            if not sys.stdin.isatty():
                raise
            use_qr = False

    if use_qr and begin is not None:
        challenge_url = begin.get("challenge_url")
        if not challenge_url:
            raise AuthError("Steam did not return a QR challenge URL.")
        print(
            "Scan the QR code with the Steam mobile app, then approve the login.",
            file=sys.stderr,
        )
        render_qr(challenge_url, qr_png, print_ascii=not quiet)
        result = poll_auth(
            session,
            begin,
            timeout=timeout,
            on_new_challenge=lambda url: render_qr(
                url, qr_png, print_ascii=not quiet
            ),
            quiet=quiet,
        )
        account_name = account
    else:
        if account is None:
            if not sys.stdin.isatty():
                raise AuthError(
                    "No account name provided and stdin is not a terminal."
                )
            account = input("Steam account name: ").strip()
        if password is None:
            if not sys.stdin.isatty():
                raise AuthError(
                    "No password provided and stdin is not a terminal."
                )
            password = getpass("Steam password: ")

        begin = begin_credentials(session, account, password)
        account_name = account
        handle_password_confirmations(session, begin, code=code)
        result = poll_auth(session, begin, timeout=timeout, quiet=quiet)

    refresh_token = result.get("refresh_token")
    if not refresh_token:
        raise AuthError("Steam did not return a refresh token. Login failed.")

    steamid = str(
        result.get("steamid")
        or begin.get("steamid")
        or steamid_from_token(refresh_token)
        or ""
    )
    if not steamid:
        # QR logins do not always return a SteamID in the poll response.
        try:
            steamid = finalize_login(session, refresh_token)
        except AuthError as exc:
            raise AuthError(
                f"Login succeeded but the SteamID could not be determined: {exc}"
            ) from exc
    if not steamid:
        raise AuthError("Steam did not return a SteamID. Login failed.")

    auth = {
        "refresh_token": refresh_token,
        "steamid": steamid,
        "account_name": account_name or result.get("account_name"),
        "created_at": _now_iso(),
        "refresh_token_expires_at": _expiry_iso(refresh_token),
    }
    try:
        path = save_auth(auth, auth_file)
    except OSError as exc:
        raise AuthError(f"Could not save {auth_file}: {exc}") from exc
    print(f"Saved Steam credentials to {path} (mode 600).")
    if auth["refresh_token_expires_at"]:
        print(f"Refresh token expires: {auth['refresh_token_expires_at']}")

    if verify:
        try:
            verify_auth(auth, auth_file)
        except AuthError as exc:
            print(f"Verification warning: {exc}", file=sys.stderr)

    return auth


def verify_auth(auth: dict, auth_file: str = DEFAULT_AUTH_FILE) -> None:
    """Print a short summary that proves the token works."""
    token, session, steamid = mint_web_token(auth, auth_file)

    family = _request(
        session,
        "GET",
        _api_url("IFamilyGroupsService", "GetFamilyGroupForUser"),
        params={"access_token": token},
    )
    family_response = family.get("response") or {}
    family_group = family_response.get("family_groupid")
    if family_group and str(family_group) != "0":
        shared = _request(
            session,
            "GET",
            _api_url("IFamilyGroupsService", "GetSharedLibraryApps"),
            params={
                "access_token": token,
                "family_groupid": family_group,
                "include_own": "false",
            },
        )
        apps = (shared.get("response") or {}).get("apps", [])
        print(f"Family group: {family_group} ({len(apps)} shared apps)")
    elif family_response.get("is_not_member_of_any_group"):
        print("Family group: none (this account is not in Steam Families).")
    else:
        print("Family group: not found (fine if you do not use Steam Families).")

    owned = _request(
        session,
        "GET",
        _api_url("IPlayerService", "GetOwnedGames"),
        params={
            "access_token": token,
            "steamid": steamid,
            "include_appinfo": 1,
            "include_played_free_games": 1,
        },
    )
    games = (owned.get("response") or {}).get("games", [])
    print(f"Owned games: {len(games)}")
    print(f"SteamID64: {steamid}")


def main(argv: Optional[list[str]] = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=Path.cwd() / ".env")
        load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(
        description=(
            "Log in to Steam once and store a long-lived refresh token for "
            "library_extractor.py."
        )
    )
    parser.add_argument(
        "--no-qr",
        action="store_true",
        help="use account name + password + Steam Guard instead of a QR code",
    )
    parser.add_argument(
        "--account",
        help="Steam account name (password login)",
    )
    parser.add_argument(
        "--code",
        help="Steam Guard code (password login; otherwise prompted)",
    )
    parser.add_argument(
        "--auth-file",
        default=os.getenv("STEAM_AUTH_FILE", DEFAULT_AUTH_FILE),
        help=f"where to save the token (default: {DEFAULT_AUTH_FILE})",
    )
    parser.add_argument(
        "--qr-png",
        default=DEFAULT_QR_PNG,
        help=f"PNG copy of the QR code (default: {DEFAULT_QR_PNG})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="seconds to wait for approval (default: 180)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="do not print the ASCII QR code (the PNG is still written)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="check the token against the family and owned-games endpoints",
    )
    args = parser.parse_args(argv)

    try:
        run_login(
            auth_file=args.auth_file,
            use_qr=not args.no_qr,
            account=args.account,
            code=args.code,
            timeout=args.timeout,
            qr_png=args.qr_png,
            quiet=args.quiet,
            verify=args.verify,
        )
    except AuthError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nLogin cancelled.", file=sys.stderr)
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
