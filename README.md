# Steam Library Extractor (Owned + Family Sharing)

Export your Steam library — owned games **and** Steam Families / Family Sharing
games — to **CSV** and **JSON**, enriched with store metadata. The output is
ready to upload to an LLM or query locally.

The normal flow is fully unattended after a **one-time login**:

```
python steam_auth.py       # once: scan a QR code with the Steam app
python library_extractor.py  # every run: no human input
```

---

## 1) Quick start

### Prerequisites

- Python 3.10+
- A Steam account (the mobile app is recommended for QR login)

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### One-time login

```bash
python steam_auth.py
```

The script prints a QR code (and writes `steam_login_qr.png`). Scan it with the
**Steam mobile app** and approve the login. It then saves `auth.json` with a
long-lived refresh token.

No password is typed, and no password is stored.

If you do not use the Steam mobile app:

```bash
python steam_auth.py --no-qr
```

Enter your account name, password (hidden), and one Steam Guard code.

### Agent-driven login

An agent can run the login without ever seeing the password:

```bash
python steam_auth.py --quiet --qr-png /tmp/steam_login_qr.png
```

Run it in the background, show the PNG to the user, and wait for the process to
exit. The user scans the QR and approves; the agent only sees "saved".

### Extract

```bash
python library_extractor.py
```

That's it. On every run the script exchanges the refresh token for a fresh
Steam web session, mints a new `webapi_token`, fetches owned + family games,
enriches them, and writes the output files.

---

## 2) What the login does

`steam_auth.py` uses Steam's `IAuthenticationService` — the same login flow the
Steam client uses:

1. `BeginAuthSessionViaQR` returns a challenge URL (rendered as a QR code).
2. You approve it in the Steam mobile app.
3. `PollAuthSessionStatus` returns a **refresh token**; the SteamID64 is
   decoded from the token's claims.
4. The refresh token is saved to `auth.json` with mode `600`.

On each extraction run:

1. `login.steampowered.com/jwt/finalizelogin` exchanges the refresh token for
   fresh Steam web cookies.
2. `store.steampowered.com/pointssummary/ajaxgetasyncconfig` returns a new
   `webapi_token`.
3. That token is used for `IFamilyGroupsService` (and, when possible,
   `GetOwnedGames`).

The refresh token is long-lived, but Steam can revoke it (password change,
"sign out of all devices", security event). When that happens the extractor
exits with code `2` and tells you to run `python steam_auth.py` again.

### Security

- `auth.json` is a **bearer credential** — treat it like a password.
- It is created with mode `600` and is gitignored.
- The Steam password is never stored; it is only used for the optional
  password login.
- Revoke the session from Steam if the file leaks (change password or sign out
  of all devices), then run `python steam_auth.py` again.
- Run the login on the machine/network that will run the extractor. Steam may
  bind the session to the login location, so copying `auth.json` to a different
  IP can require a fresh login.

---

## 3) Output

By default the files are written to the current directory:

| File | Contents |
|---|---|
| `steam_library_data.csv` | One row per game |
| `steam_library_data.json` | Same data as JSON |
| `steam_library_manifest.json` | Run summary: counts, auth source, failed appids, timings |
| `.cache/appdetails/` | Raw store metadata, used for resume |

Columns: `app_id`, `name`, `source` (`Owned` / `Family Sharing`),
`playtime_hours`, `developer`, `publisher`, `genres`, `categories`,
`metacritic_score`, `release_date`, `price`, `windows`, `mac`, `linux`,
`metadata_status`.

`metadata_status` is one of:

- `ok` — metadata fetched normally
- `not_found` — the app has no store page (delisted, tool, etc.)
- `failed` — Steam/network failed after retries (the game is kept; retry later)

`--out-dir` changes the destination:

```bash
python library_extractor.py --out-dir exports/
```

---

## 4) Options

```
python library_extractor.py [options]

--auth-file PATH     refresh-token file (default: auth.json)
--login              run the Steam login flow before extracting
--no-auth            ignore auth.json, use only .env credentials (CI mode)
--login-timeout N    seconds to wait for approval (default: 180)
--owned-only         skip Steam Families
--out-dir DIR        output directory (default: .)
--cache-dir DIR      metadata cache (default: .cache/appdetails)
--no-cache           ignore and do not write the cache
--delay SECONDS      delay between store requests (default: 1.5)
--limit N            only process the first N games (test run)
--quiet              do not print the ASCII QR code during --login
--verbose            debug logging
```

Examples:

```bash
# Quick test with 10 games
python library_extractor.py --limit 10 --out-dir test/

# Owned games only (no family endpoint)
python library_extractor.py --owned-only

# Re-login and extract in one command
python library_extractor.py --login
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success (including an empty library) |
| `1` | Fatal error |
| `2` | Authentication/configuration problem (run `steam_auth.py`) |
| `3` | Finished, but some metadata fetches failed (data is still written) |

Exit code `3` is useful in a scheduler: the extraction succeeded, but a later
run with the cache should fill in the missing metadata.

---

## 5) Scheduling

After the one-time login, a cron job needs no interaction:

```cron
0 8 * * * cd /path/to/Extract_Steam_Library && .venv/bin/python library_extractor.py >> extract.log 2>&1
```

The metadata cache makes reruns cheap: already-fetched appids are skipped, and
only failures are retried.

A systemd timer works the same way — point `ExecStart` at the extractor and set
`WorkingDirectory` to the repo.

---

## 6) Manual / CI fallback

If you do not want to store a refresh token, fill `.env` (copy `.env_sample`)
and run the extractor directly. Use `--no-auth` to ignore an existing
`auth.json` and force the `.env` path.

### A. Steam Web API key (`STEAM_API_KEY`)

Permanent, one-time:

1. Log into Steam in a browser.
2. Open <https://steamcommunity.com/dev/apikey>.
3. Register a key (domain can be `localhost`).
4. Copy it to `.env`.

### B. Steam64 ID (`STEAM_ID`)

Your 17-digit SteamID64. It is also decoded automatically from `auth.json` or
from a `WEBAPI_TOKEN` JWT, so you usually do not need to set it.

### C. Store `WEBAPI_TOKEN` (family sharing, ~24 h)

This is the old manual method. It expires after about a day:

1. Log into the Steam Store in a browser.
2. Open <https://store.steampowered.com/pointssummary/ajaxgetasyncconfig>.
3. Copy the `webapi_token` value to `.env`.

Without a token, the script still exports owned games. With `WEBAPI_TOKEN`, it
also exports family-shared games.

### `.env`

```env
STEAM_API_KEY=...
STEAM_ID=...
WEBAPI_TOKEN=...
```

`.env` is gitignored. Do not wrap values in quotes.

---

## 7) What the extractor does

1. Resolves credentials: `WEBAPI_TOKEN` → `auth.json` refresh token →
   `STEAM_API_KEY`.
2. Fetches owned games from `IPlayerService/GetOwnedGames`.
3. Fetches shared games from `IFamilyGroupsService` (if a token is available).
4. Merges them; games you own are tagged `Owned`, shared-only games are
   `Family Sharing`.
5. Fetches store metadata from `store.steampowered.com/api/appdetails`, with:
   - a per-appid disk cache (resume after a crash),
   - exponential backoff on HTTP 429/5xx,
   - clear reporting of failures in `steam_library_manifest.json`.
6. Writes CSV, JSON, and the manifest atomically.

---

## 8) Troubleshooting

- **`Steam refresh token expired or was revoked`** — run
  `python steam_auth.py` again.
- **`No SteamID available`** — run `python steam_auth.py`, or set `STEAM_ID`.
- **`Owned games ... rejected`** — the session access token may be rejected for
  `GetOwnedGames`; set a permanent `STEAM_API_KEY` in `.env` as a fallback.
- **Family library empty** — you may not be in a Steam Family group, the token
  may be expired, or the family library is excluded from sharing.
- **Owned games empty** — Steam profile privacy: set **Game details** to
  public.
- **`429` / rate limit** — the cache resumes where it left off; increase
  `--delay` if it keeps happening.
- **QR code is not scannable** — open `steam_login_qr.png`, or run
  `python steam_auth.py --no-qr`.
- **Password login asks for a captcha** — use the QR login instead.
- **`auth.json` permission warning** — the script tightens it to `600`
  automatically.

---

## Development

```bash
python -m unittest discover -s tests -v
```

The tests cover the auth-file handling, JWT parsing, password encryption,
library merging, metadata records, cache round-trips, and the family-auth
failure path. They do not need a Steam account.

---

## Notes

- Steam Families endpoints are undocumented and may change without notice.
- Automated login is not an officially supported Steam API use case. This is a
  personal, low-frequency tool; keep request rates reasonable.
- Metadata results are cached, so deleting `.cache/` forces a full refresh.
