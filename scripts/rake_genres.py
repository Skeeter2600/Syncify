import asyncio
import os
import argparse
import sys
import httpx

# Avoid UnicodeEncodeError on Windows terminal output
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

async def rake_genres(url: str, api_key: str, dry_run: bool = True):
    """
    Connects to Jellyfin, finds all audio tracks with comma-separated genres,
    splits them, and updates the metadata on the server.
    """
    headers = {
        'X-Emby-Authorization': f'MediaBrowser Token="{api_key}"',
        'Content-Type': 'application/json'
    }
    
    print(f"Connecting to Jellyfin at {url}...")
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1. Get an Admin User ID (needed to fetch all items globally)
        resp = await client.get(f"{url}/Users", headers=headers)
        resp.raise_for_status()
        users = resp.json()
        if not users:
            print("Error: No users found on this Jellyfin instance.")
            return
            
        # Prefer an admin user
        admin_user = next((u for u in users if u.get("Policy", {}).get("IsAdministrator")), users[0])
        user_id = admin_user["Id"]
        print(f"Running as user: {admin_user['Name']}")
        
        # 2. Fetch all music items (tracks, albums, artists) that have Genres
        print("Fetching music library (this may take a moment)...")
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Audio,MusicAlbum,MusicArtist",
            "Fields": "Genres"
        }
        resp = await client.get(f"{url}/Users/{user_id}/Items", headers=headers, params=params)
        resp.raise_for_status()
        
        items = resp.json().get("Items", [])
        print(f"Found {len(items)} audio tracks.")
        
        fixed_count = 0
        
        # 3. Process each item
        for item in items:
            original_genres = item.get("Genres", [])
            if not original_genres:
                continue
                
            new_genres = []
            needs_fix = False
            
            for genre in original_genres:
                if "," in genre:
                    needs_fix = True
                    # Split by comma and strip whitespace
                    split_genres = [g.strip() for g in genre.split(",")]
                    new_genres.extend(split_genres)
                else:
                    new_genres.append(genre)
                    
            # Deduplicate just in case
            new_genres = list(dict.fromkeys(new_genres))
            
            if needs_fix:
                print(f"Found issue in: {item.get('Name', 'Unknown Track')} (ID: {item['Id']})")
                print(f"  Old: {original_genres}")
                print(f"  New: {new_genres}")
                
                if not dry_run:
                    # To update, we must fetch the full item object first
                    item_id = item["Id"]
                    full_item_resp = await client.get(f"{url}/Users/{user_id}/Items/{item_id}", headers=headers)
                    full_item = full_item_resp.json()
                    
                    # Overwrite genres
                    full_item["Genres"] = new_genres
                    
                    # Post it back
                    update_resp = await client.post(f"{url}/Items/{item_id}", headers=headers, json=full_item)
                    if update_resp.status_code in (200, 204):
                        print("  -> Updated successfully.")
                        fixed_count += 1
                    else:
                        print(f"  -> Failed to update (HTTP {update_resp.status_code})")
                        
    if dry_run:
        print("\n[DRY RUN COMPLETE] Rerun with --execute to apply these changes.")
    else:
        print(f"\n[COMPLETE] Fixed {fixed_count} tracks.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jellyfin Genre Rake - Splits comma-separated genres.")
    parser.add_argument("--url", help="Jellyfin Server URL (e.g. http://localhost:8096)", required=True)
    parser.add_argument("--api-key", help="Jellyfin API Key", required=True)
    parser.add_argument("--execute", action="store_true", help="Actually apply the changes (default is dry-run)")
    
    args = parser.parse_args()
    
    url = args.url.rstrip("/")
    asyncio.run(rake_genres(url, args.api_key, dry_run=not args.execute))
