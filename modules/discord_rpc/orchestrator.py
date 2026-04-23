# --- BEGIN DISCORD RPC ---
"""
Orchestrator for Discord Rich Presence sidecar.

Manages the state machine (IDLE <-> PLAYING), runs the scanner
on a daemon thread, and coordinates between scanner, uploader, and rpc_client.
"""

import json
import logging
import pathlib
import threading
import time

log = logging.getLogger(__name__)

# State machine states
_STATE_IDLE = "IDLE"
_STATE_PLAYING = "PLAYING"

# Scan interval in seconds
_SCAN_INTERVAL = 5

# Config file name (stored in F95Checker data directory)
_CONFIG_FILENAME = "rpc_config.json"

# Default config
_DEFAULT_CONFIG = {
    "discord_rpc_enabled": True,
}

DEFAULT_CLIENT_ID = 1345123985415458858

# Module state
_state = _STATE_IDLE
_current_game_id = None
_current_game_dir = None
_thread = None
_stop_event = threading.Event()
_config = dict(_DEFAULT_CONFIG)
_config_path = None


def _get_config_path() -> pathlib.Path:
    """Get the config file path, lazy-initializing from globals."""
    global _config_path
    if _config_path is None:
        from modules import globals
        _config_path = globals.data_path / _CONFIG_FILENAME
    return _config_path


def load_config():
    """Load RPC config from disk."""
    global _config
    try:
        path = _get_config_path()
        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
                _config.update(saved)
            log.info(f"Loaded RPC config: enabled={_config['discord_rpc_enabled']}")
    except Exception as e:
        log.debug(f"Failed to load RPC config: {e}")


def save_config():
    """Save RPC config to disk."""
    try:
        path = _get_config_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_config, f, indent=2)
    except Exception as e:
        log.debug(f"Failed to save RPC config: {e}")


def get_config(key: str, default=None):
    """Get a config value."""
    return _config.get(key, default)


def set_config(key: str, value):
    """Set a config value and save to disk."""
    _config[key] = value
    save_config()


def get_cache_path() -> pathlib.Path:
    """Get the RPC image cache path."""
    from modules.discord_rpc import uploader
    return uploader._get_cache_path()


def clear_image_cache():
    """Clear the RPC image cache."""
    from modules.discord_rpc import uploader
    uploader.clear_cache()


def is_enabled() -> bool:
    """Check if Discord RPC is enabled in config."""
    return _config.get("discord_rpc_enabled", True)


def _run():
    """Main loop for the Discord RPC background thread."""
    global _state, _current_game_id, _current_game_dir

    from modules.discord_rpc import rpc_client, uploader, scanner

    # Load caches
    uploader.load_cache()
    load_config()

    log.info("Discord RPC orchestrator started")

    while not _stop_event.is_set():
        try:
            if not is_enabled():
                # If disabled, clear presence and idle
                if _state == _STATE_PLAYING:
                    rpc_client.clear_presence()
                    _state = _STATE_IDLE
                    _current_game_id = None
                    _current_game_dir = None
                _stop_event.wait(_SCAN_INTERVAL)
                continue

            # Scan for running games
            matched_game = scanner.scan_running_games()

            if matched_game is not None:
                # A game is running
                if _state == _STATE_IDLE or _current_game_id != matched_game.id:
                    # New game detected - transition to PLAYING
                    _state = _STATE_PLAYING
                    _current_game_id = matched_game.id

                    # Determine the game directory for child process tracking
                    if matched_game.executables:
                        try:
                            exe_path = matched_game.executables[0]
                            if pathlib.Path(exe_path).is_absolute():
                                _current_game_dir = str(pathlib.Path(exe_path).parent).lower()
                            else:
                                from modules import globals
                                default_exe_dir = globals.settings.default_exe_dir.get(globals.os, "")
                                if default_exe_dir:
                                    _current_game_dir = str(
                                        (pathlib.Path(default_exe_dir) / exe_path).parent
                                    ).lower()
                                else:
                                    _current_game_dir = str(pathlib.Path(exe_path).parent).lower()
                        except Exception:
                            _current_game_dir = None
                    else:
                        _current_game_dir = None

                    # Get image URL (from cache or upload)
                    image_url = None
                    try:
                        image_url = uploader.get_image_url(matched_game.id, matched_game.name)
                    except Exception as e:
                        log.debug(f"Image upload failed: {e}")

                    # Update Discord presence
                    rpc_client.update_presence(
                        title=matched_game.name,
                        image_url=image_url,
                        state="Playing",
                    )
                else:
                    # Same game still running - verify process count
                    if _current_game_dir:
                        proc_count = scanner.count_processes_in_dir(_current_game_dir)
                        if proc_count == 0:
                            # All processes from game dir exited - go IDLE
                            log.info(f"Game processes exited: {matched_game.name}")
                            _state = _STATE_IDLE
                            _current_game_id = None
                            _current_game_dir = None
                            rpc_client.clear_presence()
            else:
                # No game running
                if _state == _STATE_PLAYING:
                    # Transition from PLAYING to IDLE
                    game_name = "unknown"
                    if _current_game_id:
                        try:
                            from modules import globals
                            game = globals.games.get(_current_game_id)
                            if game:
                                game_name = game.name
                        except Exception:
                            pass
                    log.info(f"Game stopped: {game_name}")
                    _state = _STATE_IDLE
                    _current_game_id = None
                    _current_game_dir = None
                    rpc_client.clear_presence()

        except Exception as e:
            log.warning(f"Discord RPC orchestrator error: {e}")

        # Wait for next scan cycle
        _stop_event.wait(_SCAN_INTERVAL)

    # Cleanup on exit
    rpc_client.clear_presence()
    rpc_client.disconnect()
    log.info("Discord RPC orchestrator stopped")


def start_background_thread():
    """Start the Discord RPC background thread."""
    global _thread
    if _thread is not None and _thread.is_alive():
        log.warning("Discord RPC thread already running")
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_run, name="DiscordRPC", daemon=True)
    _thread.start()
    log.info("Discord RPC background thread started")


def stop():
    """Stop the Discord RPC background thread."""
    global _thread, _state, _current_game_id, _current_game_dir
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=10)
        _thread = None
    _state = _STATE_IDLE
    _current_game_id = None
    _current_game_dir = None
    log.info("Discord RPC stopped")


def is_running() -> bool:
    """Check if the Discord RPC background thread is running."""
    return _thread is not None and _thread.is_alive()
# --- END DISCORD RPC ---