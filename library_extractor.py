#!/usr/bin/env python3
"""Export a Steam library (owned + Steam Families) to CSV/JSON.

Credentials are resolved in this order:

1. ``WEBAPI_TOKEN`` in ``.env`` (manual override, useful for CI)
2. ``auth.json`` created by ``steam_auth.py`` (refresh token, fully automatic)
3. ``STEAM_API_KEY`` in ``.env`` (owned games only; permanent fallback)

If a refresh token is available, the script mints a fresh Steam web session on
every run, so no browser interaction or token copying is needed.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

from steam_auth import (
    AuthError,
    AuthExpired,
    load_auth,
    mint_web_token,
    run_login,
    steamid_from_token,
)

OWNED_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
FAMILY_GROUP_URL = (
    "https://api.steampowered.com/IFamilyGroupsService/GetFamilyGroupForUser/v1/"
)
FAMILY_APPS_URL = (
    "https://api.steampowered.com/IFamilyGroupsService/GetSharedLibraryApps/v1/"
)
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

DEFAULT_AUTH_FILE = "auth.json"
DEFAULT_CACHE_DIR = ".cache/appdetails"
USER_AGENT = "steam-library-extractor/2.0"

FIELDS = [
    "app_id",
    "name",
    "source",
    "playtime_hours",
    "developer",
    "publisher",
    "genres",
    "categories",
    "metacritic_score",
    "release_date",
    "price",
    "windows",
    "mac",
    "linux",
    "metadata_status",
]

logger = logging.getLogger("steam_library")


class SteamAPIError(Exception):
    """The Steam API returned an error we could not recover from."""


class CredentialsError(SteamAPIError):
    """Steam rejected the API key/token."""


class RateLimitError(SteamAPIError):
    """Steam kept returning 429 after all retries."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_text(text: str, limit: int = 200) -> str:
    """Turn an HTML error page into one readable line."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _retry_after(response: requests.Response) -> Optional[int]:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _get_json(
    session: requests.Session,
    url: str,
    params: dict,
    *,
    timeout: tuple = (10, 30),
    retries: int = 3,
) -> dict:
    """GET JSON with retries for 429/5xx and clear credential errors."""
    params = dict(params)
    params.setdefault("format", "json")
    last_error: Optional[Exception] = None

    for attempt in range(retries + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2**attempt)
                continue
            raise SteamAPIError(f"Network error contacting Steam: {exc}") from exc

        if response.status_code in (401, 403):
            snippet = _clean_text(response.text)
            raise CredentialsError(
                f"Steam rejected the credentials (HTTP {response.status_code}). {snippet}"
            )

        if response.status_code == 429:
            wait = _retry_after(response) or min(60, 5 * (2**attempt))
            last_error = RateLimitError(f"Steam rate limit (HTTP 429) on {url}")
            if attempt < retries:
                logger.warning("Rate limited by Steam; sleeping %ss.", wait)
                time.sleep(wait)
                continue
            raise last_error

        if response.status_code >= 500:
            last_error = SteamAPIError(
                f"Steam returned HTTP {response.status_code}: "
                f"{_clean_text(response.text)}"
            )
            if attempt < retries:
                time.sleep(2**attempt)
                continue
            raise last_error

        if response.status_code >= 400:
            snippet = _clean_text(response.text)
            raise SteamAPIError(
                f"Steam returned HTTP {response.status_code}: {snippet}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2**attempt)
                continue
            raise SteamAPIError(
                f"Steam returned non-JSON content: {_clean_text(response.text)}"
            ) from exc

        if not isinstance(payload, dict):
            raise SteamAPIError("Steam returned an unexpected response shape.")
        return payload

    raise SteamAPIError(str(last_error) if last_error else "Unknown Steam API error.")


def _fetch_owned_with(
    session: requests.Session,
    steamid: str,
    auth_params: dict,
    label: str,
) -> dict:
    params = {
        **auth_params,
        "steamid": steamid,
        "include_appinfo": 1,
        "include_played_free_games": 1,
    }
    payload = _get_json(session, OWNED_URL, params)
    response = payload.get("response") or {}
    games = response.get("games")
    if games is None:
        logger.warning(
            "Steam returned no 'games' field via %s. "
            "The profile may be private or the library empty.",
            label,
        )
        return {}
    return {
        _int(game.get("appid")): game
        for game in games
        if game.get("appid") is not None
    }


def get_owned_games(
    session: requests.Session,
    steamid: str,
    *,
    api_key: Optional[str] = None,
    access_token: Optional[str] = None,
) -> dict:
    """Fetch owned games, preferring the session token and falling back to a key."""
    attempts: list[tuple[str, dict]] = []
    if access_token:
        attempts.append(("access token", {"access_token": access_token}))
    if api_key:
        attempts.append(("API key", {"key": api_key}))

    if not attempts:
        raise CredentialsError(
            "No Steam API key or access token. "
            "Run `python steam_auth.py` or set STEAM_API_KEY in .env."
        )

    errors = []
    for label, auth_params in attempts:
        try:
            return _fetch_owned_with(session, steamid, auth_params, label)
        except CredentialsError as exc:
            errors.append(f"{label}: {exc}")
            logger.warning("Owned games via %s failed: %s", label, exc)

    raise CredentialsError("; ".join(errors))


def get_family_games(
    session: requests.Session, access_token: str
) -> tuple[dict, str]:
    """Fetch Steam Families shared games. Returns (games, status)."""
    group_payload = _get_json(
        session, FAMILY_GROUP_URL, {"access_token": access_token}
    )
    group_response = group_payload.get("response") or {}
    family_group_id = group_response.get("family_groupid")

    if not family_group_id or str(family_group_id) == "0":
        if group_response.get("is_not_member_of_any_group"):
            logger.info("This account is not in a Steam Family group.")
            return {}, "not_member"
        return {}, "not_found"

    apps_payload = _get_json(
        session,
        FAMILY_APPS_URL,
        {
            "access_token": access_token,
            "family_groupid": family_group_id,
            "include_own": "false",
        },
    )
    apps = (apps_payload.get("response") or {}).get("apps") or []

    family: dict[int, dict] = {}
    for app in apps:
        appid = _int(app.get("appid"), default=-1)
        if appid < 0:
            continue
        family[appid] = {
            "appid": appid,
            "name": app.get("name"),
            # The family endpoint exposes playtime in minutes.
            "playtime_forever": _int(app.get("rt_playtime")),
            "source": "Family Sharing",
        }

    logger.info("Family group %s: %d shared apps.", family_group_id, len(family))
    return family, "ok"


def merge_libraries(owned: dict, family: dict) -> dict[int, dict]:
    """Merge owned + shared libraries, with owned games taking precedence."""
    merged: dict[int, dict] = {}
    for appid, game in owned.items():
        appid = _int(appid)
        merged[appid] = {
            "appid": appid,
            "name": game.get("name"),
            "playtime_forever": _int(game.get("playtime_forever")),
            "source": "Owned",
        }

    for appid, game in family.items():
        appid = _int(appid)
        if appid in merged:
            continue
        merged[appid] = {
            "appid": appid,
            "name": game.get("name") or f"Unknown App {appid}",
            "playtime_forever": _int(game.get("playtime_forever")),
            "source": "Family Sharing",
        }

    return merged


# --------------------------------------------------------------------------
# Store metadata (with cache + retries)
# --------------------------------------------------------------------------


def _cache_file(cache_dir: Path, appid: int) -> Path:
    return Path(cache_dir) / f"{appid}.json"


def read_cache(cache_dir: Path, appid: int) -> Optional[dict]:
    path = _cache_file(cache_dir, appid)
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) and "success" in entry else None


def write_cache(cache_dir: Path, appid: int, entry: dict) -> None:
    path = _cache_file(cache_dir, appid)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(entry, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError as exc:
        logger.debug("Could not write metadata cache for %s: %s", appid, exc)


def fetch_appdetails(
    session: requests.Session,
    appid: int,
    cache_dir: Path,
    *,
    use_cache: bool = True,
    max_retries: int = 4,
) -> tuple[Optional[dict], str]:
    """Return ``(cache_entry, source)``; ``None`` means the fetch failed."""
    if use_cache:
        cached = read_cache(cache_dir, appid)
        if cached is not None:
            return cached, "cache"

    for attempt in range(max_retries + 1):
        try:
            response = session.get(
                APPDETAILS_URL,
                params={"appids": appid, "l": "english"},
                timeout=(10, 30),
            )
        except requests.RequestException as exc:
            if attempt < max_retries:
                wait = min(60, 2**attempt * 5)
                logger.warning(
                    "Metadata request for %s failed (%s); retrying in %ss.",
                    appid,
                    exc,
                    wait,
                )
                time.sleep(wait)
                continue
            logger.error("Metadata request for %s failed permanently: %s", appid, exc)
            return None, "failed"

        if response.status_code == 429:
            wait = _retry_after(response) or min(300, 30 * (2**attempt))
            if attempt < max_retries:
                logger.warning(
                    "Rate limited on app %s; sleeping %ss.", appid, wait
                )
                time.sleep(wait)
                continue
            logger.error("Gave up on app %s after repeated 429 responses.", appid)
            return None, "failed"

        if response.status_code in (403, 404):
            entry = {
                "appid": appid,
                "success": False,
                "data": {},
                "fetched_at": now_iso(),
            }
            if use_cache:
                write_cache(cache_dir, appid, entry)
            return entry, "not_found"

        if response.status_code >= 500:
            if attempt < max_retries:
                wait = min(60, 2**attempt * 5)
                logger.warning(
                    "Steam returned %s for app %s; retrying in %ss.",
                    response.status_code,
                    appid,
                    wait,
                )
                time.sleep(wait)
                continue
            logger.error(
                "Steam kept returning %s for app %s.", response.status_code, appid
            )
            return None, "failed"

        if response.status_code >= 400:
            logger.warning(
                "Steam returned %s for app %s; treating it as unavailable.",
                response.status_code,
                appid,
            )
            entry = {
                "appid": appid,
                "success": False,
                "data": {},
                "fetched_at": now_iso(),
            }
            if use_cache:
                write_cache(cache_dir, appid, entry)
            return entry, "not_found"

        try:
            payload = response.json()
        except ValueError:
            if attempt < max_retries:
                wait = min(60, 2**attempt * 5)
                logger.warning(
                    "Invalid JSON for app %s; retrying in %ss.", appid, wait
                )
                time.sleep(wait)
                continue
            logger.error("Invalid JSON for app %s after all retries.", appid)
            return None, "failed"

        item = payload.get(str(appid)) if isinstance(payload, dict) else None
        if not isinstance(item, dict):
            entry = {
                "appid": appid,
                "success": False,
                "data": {},
                "fetched_at": now_iso(),
            }
        else:
            entry = {
                "appid": appid,
                "success": bool(item.get("success")),
                "data": item.get("data") or {},
                "fetched_at": now_iso(),
            }

        if use_cache:
            write_cache(cache_dir, appid, entry)
        return entry, "network"

    return None, "failed"


def build_record(
    appid: int,
    game: dict,
    entry: Optional[dict],
    metadata_status: str,
) -> Optional[dict]:
    """Build one CSV/JSON row. Returns ``None`` for non-game entries."""
    if entry is None:
        meta: dict = {}
        metadata_status = "failed"
    elif not entry.get("success"):
        meta = {}
        metadata_status = "not_found"
    else:
        meta = entry.get("data") or {}

    app_type = meta.get("type")
    if app_type and app_type != "game":
        return None

    genres = ", ".join(
        genre.get("description", "")
        for genre in (meta.get("genres") or [])
        if genre.get("description")
    )
    categories = ", ".join(
        category.get("description", "")
        for category in (meta.get("categories") or [])
        if category.get("description")
    )
    platforms = meta.get("platforms") or {}
    price_overview = meta.get("price_overview") or {}
    release_date = meta.get("release_date") or {}
    metacritic = meta.get("metacritic") or {}

    return {
        "app_id": appid,
        "name": meta.get("name")
        or game.get("name")
        or f"Unknown App {appid}",
        "source": game.get("source") or "Owned",
        "playtime_hours": round(_int(game.get("playtime_forever")) / 60, 2),
        "developer": ", ".join(meta.get("developers") or []),
        "publisher": ", ".join(meta.get("publishers") or []),
        "genres": genres,
        "categories": categories,
        "metacritic_score": metacritic.get("score", ""),
        "release_date": release_date.get("date", ""),
        "price": "Free"
        if meta.get("is_free")
        else price_overview.get("final_formatted", ""),
        "windows": bool(platforms.get("windows", False)),
        "mac": bool(platforms.get("mac", False)),
        "linux": bool(platforms.get("linux", False)),
        "metadata_status": metadata_status,
    }


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def write_manifest(out_dir: Path, manifest: dict) -> Path:
    path = Path(out_dir) / "steam_library_manifest.json"
    manifest["updated_at"] = now_iso()
    _atomic_write_text(
        path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    )
    return path


def write_outputs(out_dir: Path, records: list[dict], manifest: dict) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "steam_library_data.json"
    csv_path = out_dir / "steam_library_data.csv"

    _atomic_write_text(
        json_path,
        json.dumps(records, indent=2, ensure_ascii=False) + "\n",
    )

    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=FIELDS,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(records)
    _atomic_write_text(csv_path, buffer.getvalue())

    manifest["outputs"] = {
        "json": str(json_path),
        "csv": str(csv_path),
        "manifest": str(out_dir / "steam_library_manifest.json"),
    }
    write_manifest(out_dir, manifest)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export owned and Steam Families games to CSV/JSON. "
            "Run `python steam_auth.py` once to enable unattended runs."
        )
    )
    parser.add_argument(
        "--auth-file",
        default=os.getenv("STEAM_AUTH_FILE", DEFAULT_AUTH_FILE),
        help=f"refresh-token file (default: {DEFAULT_AUTH_FILE})",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="run the Steam login flow before extracting",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="ignore auth.json and use only .env credentials (CI/manual mode)",
    )
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=180,
        help="seconds to wait for QR/Guard approval (default: 180)",
    )
    parser.add_argument(
        "--owned-only",
        action="store_true",
        help="skip Steam Families and export owned games only",
    )
    parser.add_argument(
        "--out-dir",
        default=".",
        help="directory for CSV/JSON/manifest (default: current directory)",
    )
    parser.add_argument(
        "--cache-dir",
        default=DEFAULT_CACHE_DIR,
        help=f"metadata cache directory (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="ignore and do not write the metadata cache",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="seconds between store metadata requests (default: 1.5)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="only process the first N games (useful for a test run)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="do not print the ASCII QR code during --login",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    return parser.parse_args(argv)


def _fail(
    manifest: dict,
    out_dir: Path,
    status: str,
    message: str,
    code: int,
) -> int:
    logger.error("%s", message)
    manifest["status"] = status
    manifest["errors"].append(message)
    write_manifest(out_dir, manifest)
    return code


def _run(
    args: argparse.Namespace,
    manifest: dict,
    out_dir: Path,
    started: float,
) -> int:
    auth_file = args.auth_file or os.getenv("STEAM_AUTH_FILE", DEFAULT_AUTH_FILE)
    api_key = (os.getenv("STEAM_API_KEY") or "").strip() or None
    manual_token = (os.getenv("WEBAPI_TOKEN") or "").strip() or None
    steam_id = (os.getenv("STEAM_ID") or "").strip() or None

    auth = None
    access_token: Optional[str] = None
    token_source: Optional[str] = None

    if args.login:
        if args.no_auth:
            return _fail(
                manifest,
                out_dir,
                "auth_error",
                "--no-auth cannot be combined with --login.",
                2,
            )
        if manual_token:
            logger.warning("--login requested; ignoring WEBAPI_TOKEN.")
            manual_token = None
        try:
            load_auth(auth_file)  # only check; we are about to replace it
        except AuthError as exc:
            logger.warning("Existing auth is not usable: %s", exc)
        try:
            auth = run_login(
                auth_file=auth_file,
                timeout=args.login_timeout,
                quiet=args.quiet,
            )
        except AuthError as exc:
            return _fail(
                manifest,
                out_dir,
                "auth_error",
                f"Steam login failed: {exc}",
                2,
            )
    elif args.no_auth:
        # Manual/CI mode: ignore a stale auth.json entirely.
        auth = None
    elif manual_token:
        # Do not touch auth.json: it may be stale, and the manual token wins.
        auth = None
    else:
        try:
            auth = load_auth(auth_file)
        except AuthError as exc:
            return _fail(manifest, out_dir, "auth_error", str(exc), 2)

    if auth:
        steam_id = steam_id or auth.get("steamid")
        try:
            access_token, _store_session, steam_id = mint_web_token(
                auth, auth_file
            )
            token_source = "refresh_token"
        except AuthExpired as exc:
            return _fail(manifest, out_dir, "auth_error", str(exc), 2)
        except AuthError as exc:
            return _fail(
                manifest,
                out_dir,
                "auth_error",
                f"Could not refresh the Steam session: {exc}",
                2,
            )
    elif manual_token:
        access_token = manual_token
        token_source = "WEBAPI_TOKEN"
        steam_id = steam_id or steamid_from_token(manual_token)

    if not steam_id:
        return _fail(
            manifest,
            out_dir,
            "auth_error",
            "No SteamID available. Run `python steam_auth.py` or set STEAM_ID in .env.",
            2,
        )

    if not access_token and not api_key:
        return _fail(
            manifest,
            out_dir,
            "auth_error",
            "No credentials available. Run `python steam_auth.py` "
            "or set STEAM_API_KEY in .env.",
            2,
        )

    if token_source:
        token_steamid = steamid_from_token(access_token or "")
        if token_steamid and token_steamid != steam_id:
            logger.warning(
                "STEAM_ID (%s) does not match the login token account (%s).",
                steam_id,
                token_steamid,
            )

    manifest["steam_id"] = steam_id
    manifest["auth_source"] = token_source or ("api_key" if api_key else "none")

    api_session = requests.Session()
    api_session.headers.update(
        {"User-Agent": USER_AGENT, "Accept": "application/json"}
    )

    try:
        owned = get_owned_games(
            api_session,
            steam_id,
            api_key=api_key,
            access_token=access_token,
        )
    except CredentialsError as exc:
        hint = (
            " Run `python steam_auth.py` to refresh the login."
            if token_source == "refresh_token"
            else " Check STEAM_API_KEY / WEBAPI_TOKEN in .env."
        )
        return _fail(
            manifest,
            out_dir,
            "auth_error",
            f"Could not fetch owned games: {exc}{hint}",
            2,
        )
    except SteamAPIError as exc:
        return _fail(
            manifest,
            out_dir,
            "fatal",
            f"Could not fetch owned games: {exc}",
            1,
        )

    family: dict = {}
    family_status = "disabled" if args.owned_only else "skipped"
    family_auth_failed = False
    if args.owned_only:
        logger.info("Skipping Steam Families (--owned-only).")
    elif not access_token:
        family_status = "no_token"
        logger.info("No family token available; exporting owned games only.")
    else:
        try:
            family, family_status = get_family_games(api_session, access_token)
        except CredentialsError as exc:
            hint = (
                " Run `python steam_auth.py` to refresh the login."
                if token_source == "refresh_token"
                else " Check WEBAPI_TOKEN in .env."
            )
            family_status = "auth_error"
            family_auth_failed = True
            message = f"Could not fetch family games: {exc}{hint}"
            manifest["errors"].append(message)
            logger.error("%s", message)
        except SteamAPIError as exc:
            family_status = "error"
            manifest["warnings"].append(f"Family library unavailable: {exc}")
            logger.warning("Family library unavailable: %s", exc)

    manifest["family_status"] = family_status

    all_games = merge_libraries(owned, family)
    manifest["counts"] = {
        "owned": len(owned),
        "family": len(family),
        "total": len(all_games),
    }
    logger.info(
        "Found %d owned + %d family = %d unique games.",
        len(owned),
        len(family),
        len(all_games),
    )

    if not all_games:
        manifest["status"] = "auth_error" if family_auth_failed else "empty"
        write_outputs(out_dir, [], manifest)
        logger.warning(
            "No games found. Check Steam privacy settings (Game details must be public)."
        )
        return 2 if family_auth_failed else 0

    items = list(all_games.items())
    if args.limit and args.limit > 0:
        items = items[: args.limit]
        logger.info("Limiting this run to %d games (--limit).", len(items))

    store_session = requests.Session()
    store_session.headers.update(
        {"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    cache_dir = Path(args.cache_dir).expanduser()

    records: list[dict] = []
    failed_appids: list[int] = []
    non_games = 0
    not_found = 0
    total = len(items)

    for index, (appid, game) in enumerate(items, 1):
        entry, _source = fetch_appdetails(
            store_session,
            appid,
            cache_dir,
            use_cache=not args.no_cache,
        )

        if entry is None:
            failed_appids.append(appid)
            record = build_record(appid, game, None, "failed")
        elif not entry.get("success"):
            not_found += 1
            record = build_record(appid, game, entry, "not_found")
        else:
            record = build_record(appid, game, entry, "ok")

        if record is None:
            non_games += 1
        else:
            records.append(record)

        if index % 25 == 0 or index == total:
            logger.info(
                "Processed %d/%d (%.1f%%).",
                index,
                total,
                100 * index / total,
            )

        if index < total and args.delay > 0:
            time.sleep(args.delay + random.uniform(0, 0.25))

    manifest["counts"].update(
        {
            "exported": len(records),
            "non_games": non_games,
            "metadata_failed": len(failed_appids),
            "metadata_not_found": not_found,
        }
    )
    manifest["failed_appids"] = failed_appids
    manifest["duration_seconds"] = round(time.monotonic() - started, 1)
    manifest["status"] = (
        "partial" if (failed_appids or family_auth_failed) else "ok"
    )

    write_outputs(out_dir, records, manifest)
    logger.info(
        "Done: exported %d games to %s", len(records), out_dir
    )
    if family_auth_failed:
        logger.error(
            "Family games were not exported; owned games are still valid. "
            "See steam_library_manifest.json."
        )
        return 2
    if failed_appids:
        logger.warning(
            "%d games had metadata fetch failures; see steam_library_manifest.json.",
            len(failed_appids),
        )
        return 3
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    # Load .env before parsing args so STEAM_AUTH_FILE defaults are picked up.
    # Support both "run from the repo" and "run from another directory".
    load_dotenv(dotenv_path=Path.cwd() / ".env")
    load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    manifest = {
        "generated_at": now_iso(),
        "status": "running",
        "auth_source": None,
        "steam_id": None,
        "family_status": "skipped",
        "counts": {},
        "failed_appids": [],
        "warnings": [],
        "errors": [],
        "outputs": {},
    }

    try:
        return _run(args, manifest, out_dir, started)
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
        manifest["duration_seconds"] = round(time.monotonic() - started, 1)
        write_manifest(out_dir, manifest)
        logger.warning("Interrupted. Partial results were not exported.")
        return 130
    except Exception as exc:  # noqa: BLE001 - last-resort guard for unattended runs
        logger.exception("Unexpected error")
        manifest["status"] = "fatal"
        manifest["errors"].append(str(exc))
        manifest["duration_seconds"] = round(time.monotonic() - started, 1)
        write_manifest(out_dir, manifest)
        return 1


if __name__ == "__main__":
    sys.exit(main())
