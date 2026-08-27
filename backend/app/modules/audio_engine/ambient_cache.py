"""Resolves abstract audio_tag requests to local files.

The AmbientAssetCache allows reading sessions to play localized ambient
tracks and handles graceful fallback to silence when assets are missing.
"""

from __future__ import annotations

import logging
import json
import httpx
from pathlib import Path

logger = logging.getLogger(__name__)


class AmbientAssetCache:
    """Resolves audio_tag to a local file path with 3-layer caching.
    
    Layers:
    1. Curated starter pack (assets/audio/)
    2. Runtime cache (runtime_audio_cache/)
    3. Dynamic Freesound fallback (downloads to runtime_audio_cache/)
    """

    def __init__(self, assets_dir: Path, freesound_key: str | None = None):
        """Initialize the cache with a directory for assets."""
        self._curated_dir = assets_dir
        self._runtime_dir = Path("runtime_audio_cache")
        self._key = freesound_key

    async def resolve(self, audio_tag: str) -> Path | None:
        """Resolve an audio_tag to a local path, or None if unavailable."""
        if not audio_tag:
            return None

        # Guard against directory traversal
        safe_tag = audio_tag.replace("/", "").replace("\\", "").replace(".", "")
        if not safe_tag:
            return None
            
        filename = f"{safe_tag}.mp3"

        # Layer 1: Curated
        local_curated = self._curated_dir / filename
        if local_curated.exists():
            return local_curated

        # Layer 2: Runtime cache
        local_runtime = self._runtime_dir / filename
        if local_runtime.exists():
            return local_runtime
            
        # Layer 3: Freesound
        if self._key:
            return await self._fetch_freesound(safe_tag, filename)

        return None

    async def _fetch_freesound(self, tag: str, filename: str) -> Path | None:
        try:
            query = f"{tag.replace('_', ' ')} ambient loop background atmosphere"
            
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://freesound.org/apiv2/search/text/",
                    params={
                        "query": query,
                        "filter": 'license:"Creative Commons 0" duration:[10 TO 120]',
                        "fields": "id,name,url,username,license,duration,previews,tags,avg_rating",
                        "token": self._key
                    },
                    timeout=10.0
                )
                if resp.status_code != 200:
                    logger.warning("Freesound API error: HTTP %d", resp.status_code)
                    return None
                    
                data = resp.json()
                if not data.get("results"):
                    return None
                    
                # Deterministic ranking: we rely on Freesound's relevance sorting
                # and take the first suitable result.
                best_match = data["results"][0]
                preview_url = best_match.get("previews", {}).get("preview-hq-mp3")
                if not preview_url:
                    return None
                    
                # Download
                dl_resp = await client.get(preview_url, timeout=30.0)
                if dl_resp.status_code != 200:
                    return None
                    
                self._runtime_dir.mkdir(parents=True, exist_ok=True)
                local_runtime = self._runtime_dir / filename
                local_runtime.write_bytes(dl_resp.content)
                
                # Write to runtime attribution
                attribution = {
                    "audio_tag": tag,
                    "sound_id": str(best_match["id"]),
                    "name": best_match["name"],
                    "creator": best_match["username"],
                    "license": best_match.get("license", "CC0"),
                    "source_url": best_match["url"],
                    "download_url": preview_url,
                    "filename": filename,
                    "duration_seconds": best_match["duration"],
                    "source_platform": "freesound"
                }
                
                attr_path = self._runtime_dir / "runtime_attribution.json"
                if attr_path.exists():
                    try:
                        history = json.loads(attr_path.read_text())
                    except json.JSONDecodeError:
                        history = []
                else:
                    history = []
                    
                history = [h for h in history if h.get("audio_tag") != tag]
                history.append(attribution)
                attr_path.write_text(json.dumps(history, indent=2))
                
                return local_runtime
                
        except Exception as e:
            logger.warning("Freesound fetch failed for %s: %s", tag, e)
            return None
