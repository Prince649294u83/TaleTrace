import urllib.request
import re
import json
import time
import os
import ssl

ssl._create_default_https_context = ssl._create_unverified_context

tags = [
    "forest", "ocean", "river", "rain_light", "thunderstorm", "wind",
    "library", "tavern", "castle", "city", "marketplace", "fireplace",
    "peaceful", "wonder", "suspense", "sorrow", "joy", "fear", "mystery",
    "comedy", "battle", "chase", "dungeon", "space", "magic", "stealth",
    "morning", "night", "snowfall", "neutral_narration"
]

manifest = []

def search_freesound(tag):
    # Map tag to a good query
    query = tag.replace("_", "+") + "+ambient+loop"
    url = f"https://freesound.org/search/?q={query}&f=license%3A%22Creative+Commons+0%22"
    
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req) as response:
            html = response.read().decode('utf-8')
            links = re.findall(r'href=["\'](/people/[^/]+/sounds/\d+/)["\']', html)
            
            # De-duplicate links while preserving order
            unique_links = []
            for link in links:
                if link not in unique_links:
                    unique_links.append(link)
                    
            for link in unique_links[:3]: # Try first few results
                sound_url = 'https://freesound.org' + link
                req2 = urllib.request.Request(sound_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req2) as res2:
                    sound_html = res2.read().decode('utf-8')
                    # verify CC0
                    if 'Creative Commons 0' in sound_html or 'publicdomain/zero/1.0' in sound_html:
                        mp3_links = re.findall(r'https://cdn\.freesound\.org/previews/[^"\']+?-hq\.mp3', sound_html)
                        if mp3_links:
                            sound_id_match = re.search(r'/sounds/(\d+)/', link)
                            sound_id = sound_id_match.group(1) if sound_id_match else "unknown"
                            creator_match = re.search(r'/people/([^/]+)/', link)
                            creator = creator_match.group(1) if creator_match else "unknown"
                            
                            # get duration approximately
                            dur_match = re.search(r'property="duration" content="PT([^"]+)"', sound_html)
                            duration = 30
                            if dur_match:
                                dur_str = dur_match.group(1)
                                # like 00M30S or 1M12S
                                m = re.search(r'(?:(\d+)M)?(\d+(?:\.\d+)?)S', dur_str)
                                if m:
                                    mins = float(m.group(1)) if m.group(1) else 0
                                    secs = float(m.group(2))
                                    duration = int(mins * 60 + secs)
                            
                            print(f"Found {tag}: {mp3_links[0]}")
                            return {
                                "audio_tag": tag,
                                "sound_id": sound_id,
                                "name": tag.replace('_', ' ').title() + " Ambient",
                                "creator": creator,
                                "license": "CC0",
                                "source_url": sound_url,
                                "download_url": mp3_links[0],
                                "filename": f"{tag}.mp3",
                                "duration_seconds": duration,
                                "source_platform": "freesound"
                            }
            return None
    except Exception as e:
        print(f"Error searching {tag}: {e}")
        return None

for tag in tags:
    print(f"Searching for {tag}...")
    result = search_freesound(tag)
    if result:
        manifest.append(result)
    time.sleep(2)

os.makedirs('assets/audio', exist_ok=True)
with open('assets/audio/attribution.json', 'w') as f:
    json.dump(manifest, f, indent=2)

print(f"Found {len(manifest)} sounds out of {len(tags)}")
