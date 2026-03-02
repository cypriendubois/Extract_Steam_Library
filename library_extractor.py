import os
import requests
import time
import json
import csv
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ================= CONFIGURATION =================
STEAM_API_KEY = os.getenv("STEAM_API_KEY")
STEAM_ID = os.getenv("STEAM_ID")
WEBAPI_TOKEN = os.getenv("WEBAPI_TOKEN")
# =================================================

def validate_credentials():
    """Ensure required credentials are loaded."""
    if not STEAM_API_KEY or not STEAM_ID:
        print("Error: STEAM_API_KEY or STEAM_ID is missing.")
        print("Please ensure your .env file is set up correctly in the same directory.")
        exit(1)

def get_owned_games():
    """Fetch games owned by the user account."""
    url = "http://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
    params = {
        'key': STEAM_API_KEY,
        'steamid': STEAM_ID,
        'format': 'json',
        'include_appinfo': 1,
        'include_played_free_games': 1
    }
    
    print("Fetching owned games...")
    response = requests.get(url, params=params).json()
    if 'response' in response and 'games' in response['response']:
        return {game['appid']: game for game in response['response']['games']}
    return {}

def get_family_games():
    """Fetch games available via Steam Family Sharing."""
    if not WEBAPI_TOKEN:
        print("No WEBAPI_TOKEN provided in .env. Skipping Family Sharing games.")
        return {}
        
    print("Fetching Family Group ID...")
    group_url = f"https://api.steampowered.com/IFamilyGroupsService/GetFamilyGroupForUser/v1/?access_token={WEBAPI_TOKEN}"
    
    try:
        group_resp = requests.get(group_url).json()
        family_groupid = group_resp.get('response', {}).get('family_groupid')
        
        if not family_groupid:
            print("No Family Group found or WEBAPI_TOKEN is invalid/expired.")
            return {}

        print("Fetching shared Family games...")
        shared_url = f"https://api.steampowered.com/IFamilyGroupsService/GetSharedLibraryApps/v1/?access_token={WEBAPI_TOKEN}&family_groupid={family_groupid}&include_own=false"
        shared_resp = requests.get(shared_url).json()
        
        apps = shared_resp.get('response', {}).get('apps', [])
        return {app['appid']: {'appid': app['appid'], 'playtime_forever': 0} for app in apps}
        
    except Exception as e:
        print(f"Error fetching family games: {e}")
        return {}

def get_store_metadata(app_id):
    """Fetch detailed metadata from the Steam Store API."""
    url = f"https://store.steampowered.com/api/appdetails?appids={app_id}"
    try:
        response = requests.get(url)
        # Handle Steam's strict rate limiting
        if response.status_code == 429:
            print(f"\nRate limit hit at app {app_id}! Sleeping for 60 seconds...")
            time.sleep(60)
            response = requests.get(url)
            
        data = response.json()
        if data and str(app_id) in data and data[str(app_id)]['success']:
            return data[str(app_id)]['data']
    except Exception as e:
        pass
    return {}

def main():
    validate_credentials()
    
    owned_games = get_owned_games()
    family_games = get_family_games()
    
    # Merge libraries and flag family shared items
    all_games = owned_games.copy()
    for app_id, data in family_games.items():
        if app_id not in all_games:
            all_games[app_id] = data
            all_games[app_id]['source'] = 'Family Sharing'
        else:
            all_games[app_id]['source'] = 'Owned'
            
    for app_id in owned_games:
        if 'source' not in all_games[app_id]:
            all_games[app_id]['source'] = 'Owned'

    final_dataset = []
    total_games = len(all_games)
    
    if total_games == 0:
        print("No games found. Please check your Steam Profile privacy settings.")
        return
        
    print(f"\nFound {total_games} unique games. Extracting Store metadata...")

    for index, (app_id, game_info) in enumerate(all_games.items(), 1):
        print(f"Processing {index}/{total_games}: AppID {app_id}...", end="\r")
        
        meta = get_store_metadata(app_id)
        
        # Skip non-games (like dedicated servers, videos, or tools)
        if meta.get('type') and meta.get('type') != 'game':
            continue

        # Extract LLM-relevant fields
        playtime_hours = round(game_info.get('playtime_forever', 0) / 60, 2)
        categories = ", ".join([c.get('description', '') for c in meta.get('categories', [])])
        genres = ", ".join([g.get('description', '') for g in meta.get('genres', [])])
        is_free = meta.get('is_free', False)
        
        game_record = {
            'app_id': app_id,
            'name': meta.get('name') or game_info.get('name', f"Unknown App {app_id}"),
            'source': game_info.get('source'),
            'playtime_hours': playtime_hours,
            'developer': ", ".join(meta.get('developers', [])),
            'publisher': ", ".join(meta.get('publishers', [])),
            'genres': genres,
            'categories': categories,
            'metacritic_score': meta.get('metacritic', {}).get('score', ''),
            'release_date': meta.get('release_date', {}).get('date', ''),
            'price': "Free" if is_free else meta.get('price_overview', {}).get('final_formatted', ''),
            'windows': meta.get('platforms', {}).get('windows', False),
            'mac': meta.get('platforms', {}).get('mac', False),
            'linux': meta.get('platforms', {}).get('linux', False)
        }
        final_dataset.append(game_record)
        
        # Respect Store API rate limits (~200 requests / 5 mins)
        time.sleep(1.5)

    # Export to JSON
    with open('steam_library_data.json', 'w', encoding='utf-8') as f:
        json.dump(final_dataset, f, indent=4, ensure_ascii=False)

    # Export to CSV
    if final_dataset:
        with open('steam_library_data.csv', 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=final_dataset[0].keys())
            writer.writeheader()
            writer.writerows(final_dataset)

    print(f"\n\nDone! Exported {len(final_dataset)} games to steam_library_data.csv and .json")

if __name__ == "__main__":
    main()
