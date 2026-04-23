# --- BEGIN DISCORD RPC ---
"""
Image upload pipeline for Discord Rich Presence.
Uploads game thumbnails to Litterbox (temporary image hosting) and caches the URLs locally.
"""
import json
import logging
import pathlib
import time

import requests

log = logging.getLogger(__name__)

# Litterbox API endpoint for anonymous uploads
LITTERBOX_UPLOAD_URL = "https://litterbox.catbox.moe/resources/internals/api.php"
# Duration options: "1h", "12h", "24h", "1w"
LITTERBOX_EXPIRY = "24h"

# Cache file name (stored in F95Checker data directory)
CACHE_FILENAME = "rpc_cache.json"
# Cache format: {"<game_id>": {"url": "...", "uploaded_at": <unix_ts>}}

_cache = {}
_cache_path = None


def _get_cache_path() -> pathlib.Path:
    """Get the cache file path, lazy-initializing from globals."""
    global _cache_path
    if _cache_path is None:
        from modules import globals
        _cache_path = globals.data_path / CACHE_FILENAME
    return _cache_path


def load_cache():
    """Load the URL cache from disk."""
    global _cache
    try:
        path = _get_cache_path()
        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                _cache = json.load(f)
            log.info(f"Loaded RPC image cache ({len(_cache)} entries)")
        else:
            _cache = {}
    except Exception as e:
        log.debug(f"Failed to load RPC cache: {e}")
        _cache = {}


def save_cache():
    """Save the URL cache to disk."""
    try:
        path = _get_cache_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_cache, f, indent=2)
    except Exception as e:
        log.debug(f"Failed to save RPC cache: {e}")


def clear_cache():
    """Delete the cache file and reset the in-memory cache."""
    global _cache, _cache_path
    _cache = {}
    try:
        path = _get_cache_path()
        if path.is_file():
            path.unlink()
            log.info("RPC image cache cleared")
    except Exception as e:
        log.debug(f"Failed to delete RPC cache file: {e}")


def get_image_url(game_id: int, game_name: str) -> str | None:
    """Get a Discord-compatible image URL for a game.

    Checks cache first. If not cached, attempts to upload the game's local thumbnail to Litterbox.

    Args:
        game_id: The game's thread ID
        game_name: The game's name (for logging)

    Returns:
        URL string or None if no image available
    """
    # Check cache first
    cached = _cache.get(str(game_id))
    if cached:
        url = cached.get("url")
        # Validate cached URL with a HEAD request
        try:
            resp = requests.head(url, timeout=5, allow_redirects=True)
            if resp.status_code < 400:
                log.debug(f"Cache hit for {game_name} (id={game_id})")
                return url
        except Exception:
            pass
        # URL is stale, re-upload
        log.debug(f"Cache URL stale for {game_name}, re-uploading")

    # Find the game's local image file
    from modules import globals
    image_path = None
    for ext in ("png", "jpg", "jpeg", "gif", "webp"):
        candidate = globals.images_path / f"{game_id}.{ext}"
        if candidate.is_file():
            image_path = candidate
            break

    if image_path is None:
        log.debug(f"No local image for {game_name} (id={game_id})")
        return None

    # Upload to Litterbox
    url = _upload_to_litterbox(image_path, game_name)
    if url:
        # Save to cache
        _cache[str(game_id)] = {
            "url": url,
            "uploaded_at": int(time.time()),
        }
        save_cache()
    return url


def _upload_to_litterbox(image_path: pathlib.Path, game_name: str) -> str | None:
    """Upload an image file to Litterbox.

    Returns the URL of the uploaded image, or None on failure.
    """
    try:
        with open(image_path, "rb") as f:
            files = {"fileToUpload": (image_path.name, f)}
            data = {
                "reqtype": "fileupload",
                "time": LITTERBOX_EXPIRY,
            }
            resp = requests.post(
                LITTERBOX_UPLOAD_URL,
                files=files,
                data=data,
                timeout=60,
            )
        if resp.status_code == 200 and resp.text.startswith("https://"):
            url = resp.text.strip()
            log.info(f"Uploaded image for {game_name}: {url}")
            return url
        else:
            log.warning(f"Litterbox upload failed for {game_name}: {resp.status_code} {resp.text[:200]}")
            return None
    except Exception as e:
        log.warning(f"Litterbox upload error for {game_name}: {e}")
        return None


# --- END DISCORD RPC ---