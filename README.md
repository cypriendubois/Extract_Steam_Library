# Steam Library Extractor (Owned + Family Sharing)
Simple Python utility to extract your Steam library info

This repo lets you export your Steam game library (owned games + Steam Families / Family Sharing games) into **CSV** and **JSON**, with enriched metadata per game, so you can upload the output to an LLM (e.g., Perplexity) and query your collection.

---

## 1) Get the API credentials

You need 3 values:

### A. Steam Web API Key (`STEAM_API_KEY`)
1. Log into Steam in your browser.
2. Open: https://steamcommunity.com/dev/apikey
3. Register for an API key (the “Domain name” field can be something like `localhost`).
4. Copy the generated key (looks like a 32-character string).

### B. Your Steam64 ID (`STEAM_ID`)
You need your 17-digit SteamID64.
Common ways to find it:
- Steam client: **View profile** → **Edit Profile** → **Account Details** (Steam may show the SteamID on that page).
- Or use your profile URL and resolve it with a SteamID finder site.

### C. Web API token (`WEBAPI_TOKEN`) for Family Sharing
Steam Families endpoints require a web session token, which can expire periodically.

1. Log into the Steam Store in your browser.
2. Open this URL while logged in:
   - https://store.steampowered.com/pointssummary/ajaxgetasyncconfig
3. The response is JSON; locate the field:
   - `webapi_token`
4. Copy its value and use it as `WEBAPI_TOKEN`.

Notes:
- If the token is missing/invalid/expired, the script will still work for your owned library, but **family-shared games will be skipped**.
- If your profile/library privacy settings are restrictive, Steam may return incomplete results.

---

## 2) Fill the env file

The sample env file is named **`.env_sample`**.

### Step-by-step
1. Copy `.env_sample` to `.env` (same folder as the script).
2. Edit `.env` and paste your real values.

Example commands (Linux/macOS):
```bash
cp .env_sample .env
nano .env
```

Example commands (Windows PowerShell):

```powershell
Copy-Item .env_sample .env
notepad .env
```


### `.env_sample` format

Your `.env_sample` should look like this:

```env
# Steam Developer API Key (32 characters)
STEAM_API_KEY=your_32_character_api_key_here

# Your Steam64 ID (17 digits)
STEAM_ID=your_17_digit_steam64_id_here

# Web API Session Token (for Family Sharing / Steam Families; expires periodically)
WEBAPI_TOKEN=your_long_webapi_session_token_here
```

Important:

- Do not wrap values in quotes unless you know you need to.
- Keep `.env` private (don’t commit it to git).

---

## 3) What the script does

The Python script is named **`library_extractor.py`**.

It performs these actions:

1. Loads `STEAM_API_KEY`, `STEAM_ID`, and `WEBAPI_TOKEN` from `.env`.
2. Fetches your **owned** games list from Steam Web API (including appid, name, playtime).
3. If `WEBAPI_TOKEN` is present and valid, fetches **family-shared** games via Steam Families endpoints.
4. Merges owned + family-shared games into one dataset and tags each game with a `source` field (e.g., `Owned` vs `Family Sharing`).
5. For each appid, queries the Steam Store “appdetails” endpoint to enrich metadata (name, developer, publisher, genres, categories, release date, platforms, Metacritic score when available, price when available).
6. Exports two files in the working directory:
    - `steam_library_data.csv`
    - `steam_library_data.json`

Performance / rate limiting:

- The script sleeps between Store metadata requests to reduce the chance of hitting rate limits.
- Large libraries may take a while.

---

## 4) How to run it

### Prerequisites

- Python 3.10+ recommended
- Install dependencies:

```bash
pip install requests python-dotenv
```


### Run

From the folder containing `.env` and `library_extractor.py`:

```bash
python library_extractor.py
```


### Output

After it finishes, you should see:

- `steam_library_data.csv`
- `steam_library_data.json`

You can then upload either file to Perplexity (or another LLM tool that supports file upload) and ask questions like:

- “List my top 20 games by playtime.”
- “Find unplayed co-op games that support Linux.”
- “Recommend a backlog game similar to Dark Souls based on genres/tags.”

---

## Troubleshooting

- **Owned games is empty**: Check Steam profile privacy settings (Game Details / Library visibility).
- **Family sharing returns nothing**: Your `WEBAPI_TOKEN` may be expired, invalid, or you may not be in a Steam Family group.
- **429 / rate limit**: Re-run later, or increase the sleep interval in the script.
- **Missing metadata**: Some appids (tools, servers, videos) may not return “game” type data and can be skipped.
