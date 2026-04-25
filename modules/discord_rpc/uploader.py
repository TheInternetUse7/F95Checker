# --- BEGIN DISCORD RPC ---
"""
Image upload pipeline for Discord Rich Presence.
Selects the best RPC image source, uploads it to Litterbox, and caches the
discovery state locally so RPC launches do not repeatedly hit remote services.
"""
import hashlib
import json
import logging
import pathlib
import re
import time
from urllib.parse import urlparse

from PIL import Image
import requests

log = logging.getLogger(__name__)

# Litterbox API endpoint for anonymous uploads
LITTERBOX_UPLOAD_URL = "https://litterbox.catbox.moe/resources/internals/api.php"
# Duration options: "1h", "12h", "24h", "1w"
LITTERBOX_EXPIRY = "1w"

# VNDB API endpoint
VNDB_API_URL = "https://api.vndb.org/kana/vn"

# Cache file name (stored in F95Checker data directory)
CACHE_FILENAME = "rpc_cache.json"
NEGATIVE_LOOKUP_COOLDOWN = 24 * 60 * 60
MIN_SQUARENESS_ADVANTAGE = 0.05
REQUEST_TIMEOUT = 30

_VNDB_LINK_RE = re.compile(
    r"""href=["'](?:https?:)?//(?:www\.)?vndb\.org/(v\d+)(?:[/?#][^"']*)?["']""",
    re.IGNORECASE,
)
_REQUEST_HEADERS = {
    "User-Agent": "F95Checker Discord RPC",
}
_CONTENT_TYPE_EXTS = {
    "image/avif": ".avif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

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
                loaded = json.load(f)
            _cache = {
                str(game_id): _normalize_cache_entry(entry)
                for game_id, entry in loaded.items()
            }
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
    """Get a Discord-compatible image URL for a game."""
    cache_key = str(game_id)
    cache_entry = _normalize_cache_entry(_cache.get(cache_key))
    now = int(time.time())

    f95_candidate = _build_f95_candidate(game_id)
    old_f95_fingerprint = cache_entry["f95"]["fingerprint"]
    if f95_candidate:
        cache_entry["f95"] = {
            "fingerprint": f95_candidate["key"],
            "dims": f95_candidate["dims"],
        }
    else:
        cache_entry["f95"] = {"fingerprint": "", "dims": []}
    f95_changed = cache_entry["f95"]["fingerprint"] != old_f95_fingerprint

    if _should_refresh_vndb(cache_entry, now):
        _refresh_vndb_metadata(game_id, game_name, cache_entry, now)

    vndb_candidate = _build_vndb_candidate(cache_entry)
    selected = _choose_best_candidate(f95_candidate, vndb_candidate)
    cached_url = cache_entry["url"]
    cached_url_alive = _is_remote_url_alive(cached_url) if cached_url else False

    if selected is None:
        if cached_url_alive:
            return cached_url
        log.debug(f"No RPC image candidate available for {game_name} (id={game_id})")
        _cache[cache_key] = cache_entry
        save_cache()
        return None

    selected_changed = _selected_changed(cache_entry, selected)
    if cached_url_alive and not selected_changed and not f95_changed:
        log.debug(
            f"RPC image cache hit for {game_name} (id={game_id}, source={selected['source']})"
        )
        return cached_url

    uploaded_url = _upload_candidate(selected, game_name)
    if uploaded_url:
        cache_entry["selected"] = {
            "source": selected["source"],
            "key": selected["key"],
            "dims": selected["dims"],
        }
        cache_entry["url"] = uploaded_url
        cache_entry["uploaded_at"] = now
        _cache[cache_key] = cache_entry
        save_cache()
        return uploaded_url

    _cache[cache_key] = cache_entry
    save_cache()
    if cached_url_alive:
        return cached_url
    return None


def _normalize_cache_entry(entry: dict | None) -> dict:
    """Normalize old and new cache entries to the current structure."""
    normalized = {
        "url": "",
        "uploaded_at": 0,
        "selected": {
            "source": "",
            "key": "",
            "dims": [],
        },
        "f95": {
            "fingerprint": "",
            "dims": [],
        },
        "vndb": {
            "checked_at": 0,
            "negative": False,
            "id": "",
            "image_url": "",
            "dims": [],
            "authed": False,
        },
    }
    if not isinstance(entry, dict):
        return normalized

    normalized["url"] = str(entry.get("url", "") or "")
    normalized["uploaded_at"] = _normalize_int(entry.get("uploaded_at", 0))

    selected = entry.get("selected", {})
    if isinstance(selected, dict):
        normalized["selected"]["source"] = str(selected.get("source", "") or "")
        normalized["selected"]["key"] = str(selected.get("key", "") or "")
        normalized["selected"]["dims"] = _normalize_dims(selected.get("dims"))

    f95 = entry.get("f95", {})
    if isinstance(f95, dict):
        normalized["f95"]["fingerprint"] = str(f95.get("fingerprint", "") or "")
        normalized["f95"]["dims"] = _normalize_dims(f95.get("dims"))

    vndb = entry.get("vndb", {})
    if isinstance(vndb, dict):
        normalized["vndb"]["checked_at"] = _normalize_int(vndb.get("checked_at", 0))
        normalized["vndb"]["negative"] = bool(vndb.get("negative", False))
        normalized["vndb"]["id"] = str(vndb.get("id", "") or "")
        normalized["vndb"]["image_url"] = str(vndb.get("image_url", "") or "")
        normalized["vndb"]["dims"] = _normalize_dims(vndb.get("dims"))
        normalized["vndb"]["authed"] = bool(vndb.get("authed", False))

    # Migrate the original simple cache format.
    if not normalized["url"] and isinstance(entry.get("url"), str):
        normalized["url"] = entry["url"]
    if not normalized["uploaded_at"]:
        normalized["uploaded_at"] = _normalize_int(entry.get("uploaded_at", 0))
    return normalized


def _normalize_int(value) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _normalize_dims(value) -> list[int]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            width = int(value[0])
            height = int(value[1])
            if width > 0 and height > 0:
                return [width, height]
        except Exception:
            pass
    return []


def _find_local_image_path(game_id: int) -> pathlib.Path | None:
    from modules import globals

    for ext in ("png", "jpg", "jpeg", "gif", "webp", "avif"):
        candidate = globals.images_path / f"{game_id}.{ext}"
        if candidate.is_file():
            return candidate
    return None


def _build_f95_candidate(game_id: int) -> dict | None:
    image_path = _find_local_image_path(game_id)
    if image_path is None:
        return None
    dims = _get_image_dims(image_path)
    return {
        "source": "f95",
        "key": _fingerprint_file(image_path),
        "dims": dims,
        "path": image_path,
    }


def _build_vndb_candidate(cache_entry: dict) -> dict | None:
    vndb = cache_entry["vndb"]
    if not vndb["image_url"]:
        return None
    return {
        "source": "vndb",
        "key": vndb["image_url"],
        "dims": vndb["dims"],
        "url": vndb["image_url"],
    }


def _fingerprint_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _get_image_dims(path: pathlib.Path) -> list[int]:
    try:
        with Image.open(path) as image:
            width, height = image.size
        if width > 0 and height > 0:
            return [width, height]
    except Exception as e:
        log.debug(f"Failed to read image dimensions from {path}: {e}")
    return []


def _should_refresh_vndb(cache_entry: dict, now: int) -> bool:
    vndb = cache_entry["vndb"]
    if not vndb["checked_at"]:
        return True
    if not vndb["authed"]:
        return True
    if vndb["negative"] and (now - vndb["checked_at"]) >= NEGATIVE_LOOKUP_COOLDOWN:
        return True
    return False


def _refresh_vndb_metadata(game_id: int, game_name: str, cache_entry: dict, now: int):
    game = _get_game(game_id)
    if game is None:
        return

    thread_url = getattr(game, "url", "") or f"https://f95zone.to/threads/{game_id}"
    thread_html = _fetch_thread_html(thread_url)
    if not thread_html:
        return

    vndb_id = _extract_vndb_id(thread_html)
    if not vndb_id:
        cache_entry["vndb"] = {
            "checked_at": now,
            "negative": True,
            "id": "",
            "image_url": "",
            "dims": [],
            "authed": True,
        }
        log.debug(f"No VNDB link found for {game_name} (id={game_id})")
        return

    vndb_image = _fetch_vndb_image(vndb_id)
    if vndb_image is None:
        return

    cache_entry["vndb"] = {
        "checked_at": now,
        "negative": not bool(vndb_image["image_url"]),
        "id": vndb_id,
        "image_url": vndb_image["image_url"],
        "dims": vndb_image["dims"],
        "authed": True,
    }
    if vndb_image["image_url"]:
        log.debug(f"Resolved VNDB image for {game_name} (id={game_id}, vndb={vndb_id})")


def _get_game(game_id: int):
    try:
        from modules import globals
        return globals.games.get(game_id)
    except Exception:
        return None


def _fetch_thread_html(thread_url: str) -> str | None:
    try:
        resp = requests.get(
            thread_url,
            headers=_REQUEST_HEADERS,
            cookies=_get_f95_cookies(),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code < 400:
            return resp.text
        log.debug(f"F95 thread fetch failed: {resp.status_code} {thread_url}")
    except Exception as e:
        log.debug(f"F95 thread fetch error for {thread_url}: {e}")
    return None


def _get_f95_cookies() -> dict:
    try:
        from modules import globals
        if isinstance(globals.cookies, dict) and globals.cookies:
            return globals.cookies
    except Exception:
        pass
    return {}


def _extract_vndb_id(thread_html: str) -> str:
    match = _VNDB_LINK_RE.search(thread_html)
    if match:
        return match.group(1).lower()
    return ""


def _fetch_vndb_image(vndb_id: str) -> dict | None:
    try:
        resp = requests.post(
            VNDB_API_URL,
            headers={
                **_REQUEST_HEADERS,
                "Content-Type": "application/json",
            },
            json={
                "filters": ["id", "=", vndb_id],
                "fields": "image{url,dims,thumbnail,thumbnail_dims}",
                "results": 1,
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("results") or []
        if not items:
            return {
                "image_url": "",
                "dims": [],
            }
        image = (items[0] or {}).get("image") or {}
        image_url = str(image.get("thumbnail") or image.get("url") or "")
        dims = _normalize_dims(image.get("thumbnail_dims") or image.get("dims"))
        return {
            "image_url": image_url,
            "dims": dims,
        }
    except Exception as e:
        log.debug(f"VNDB image lookup failed for {vndb_id}: {e}")
        return None


def _choose_best_candidate(f95_candidate: dict | None, vndb_candidate: dict | None) -> dict | None:
    if f95_candidate is None:
        return vndb_candidate
    if vndb_candidate is None:
        return f95_candidate

    f95_score = _squareness_score(f95_candidate["dims"])
    vndb_score = _squareness_score(vndb_candidate["dims"])

    if f95_score is None and vndb_score is None:
        return f95_candidate
    if f95_score is None:
        return vndb_candidate
    if vndb_score is None:
        return f95_candidate
    if vndb_score >= f95_score + MIN_SQUARENESS_ADVANTAGE:
        return vndb_candidate
    return f95_candidate


def _squareness_score(dims: list[int]) -> float | None:
    dims = _normalize_dims(dims)
    if not dims:
        return None
    return min(dims) / max(dims)


def _selected_changed(cache_entry: dict, selected: dict) -> bool:
    previous = cache_entry["selected"]
    return (
        previous["source"] != selected["source"] or
        previous["key"] != selected["key"] or
        previous["dims"] != selected["dims"]
    )


def _is_remote_url_alive(url: str) -> bool:
    try:
        resp = requests.head(url, headers=_REQUEST_HEADERS, timeout=5, allow_redirects=True)
        return resp.status_code < 400
    except Exception:
        return False


def _upload_candidate(candidate: dict, game_name: str) -> str | None:
    if candidate["source"] == "f95":
        return _upload_to_litterbox(candidate["path"], game_name)
    if candidate["source"] == "vndb":
        return _upload_remote_image(candidate["url"], game_name)
    return None


def _upload_remote_image(source_url: str, game_name: str) -> str | None:
    try:
        resp = requests.get(source_url, headers=_REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        filename = _derive_remote_filename(source_url, resp.headers.get("Content-Type", ""))
        return _upload_bytes_to_litterbox(resp.content, filename, game_name)
    except Exception as e:
        log.warning(f"Remote image download error for {game_name}: {e}")
        return None


def _derive_remote_filename(source_url: str, content_type: str) -> str:
    parsed = urlparse(source_url)
    suffix = pathlib.Path(parsed.path).suffix.lower()
    if not suffix:
        suffix = _CONTENT_TYPE_EXTS.get(content_type.split(";")[0].strip().lower(), ".jpg")
    return f"rpc_vndb{suffix}"


def _upload_to_litterbox(image_path: pathlib.Path, game_name: str) -> str | None:
    """Upload a local image file to Litterbox."""
    try:
        with open(image_path, "rb") as f:
            return _upload_fileobj_to_litterbox(f, image_path.name, game_name)
    except Exception as e:
        log.warning(f"Litterbox upload error for {game_name}: {e}")
        return None


def _upload_bytes_to_litterbox(data: bytes, filename: str, game_name: str) -> str | None:
    """Upload in-memory image bytes to Litterbox."""
    try:
        return _upload_fileobj_to_litterbox(data, filename, game_name)
    except Exception as e:
        log.warning(f"Litterbox upload error for {game_name}: {e}")
        return None


def _upload_fileobj_to_litterbox(file_obj, filename: str, game_name: str) -> str | None:
    files = {"fileToUpload": (filename, file_obj)}
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
    log.warning(f"Litterbox upload failed for {game_name}: {resp.status_code} {resp.text[:200]}")
    return None


# --- END DISCORD RPC ---
