# --- BEGIN DISCORD RPC ---
"""
Wrapper for pypresence Discord Rich Presence client.
Handles connection lifecycle and graceful error recovery.
"""

import logging
import time

log = logging.getLogger(__name__)

# Discord application client ID for F95Checker
# You must register your own application at https://discord.com/developers/applications
# and replace this ID with yours, or set it via the config file below.
DEFAULT_CLIENT_ID = 1345123985415458858  # Placeholder - replace with your Discord App ID

_rpc = None
_connected = False
_last_connect_attempt = 0
_RECONNECT_INTERVAL = 30  # seconds between reconnection attempts
_StatusDisplayType = None


def _get_client_id() -> int:
    """Get the Discord Client ID from config, falling back to default."""
    try:
        from modules.discord_rpc import orchestrator
        configured = orchestrator.get_config("discord_client_id")
        if configured:
            return int(configured)
    except Exception:
        pass
    return DEFAULT_CLIENT_ID


def connect() -> bool:
    """Connect to Discord RPC. Returns True on success."""
    global _rpc, _connected, _last_connect_attempt, _StatusDisplayType

    now = time.time()
    if _connected and _rpc is not None:
        return True

    # Throttle reconnection attempts
    if now - _last_connect_attempt < _RECONNECT_INTERVAL:
        return False

    _last_connect_attempt = now

    try:
        from pypresence import Presence
        from pypresence.types import StatusDisplayType
        _rpc = Presence(str(_get_client_id()))
        _rpc.connect()
        _connected = True
        log.info("Discord RPC connected")
        _StatusDisplayType = StatusDisplayType
        return True
    except Exception as e:
        log.debug(f"Discord RPC connection failed: {e}")
        _rpc = None
        _connected = False
        return False


def disconnect():
    """Disconnect from Discord RPC."""
    global _rpc, _connected
    if _rpc is not None:
        try:
            _rpc.close()
        except Exception:
            pass
        _rpc = None
    _connected = False


def update_presence(
    title: str,
    image_url: str = None,
    details: str = "",
    state: str = "",
    start: int | None = None,
):
    """Update Discord Rich Presence with game info.

    Args:
        title: Game name to display
        image_url: URL of game thumbnail (from Litterbox upload)
        details: Secondary detail line text
        state: Tertiary state line text
        start: Unix timestamp for the session start
    """
    if not connect():
        return

    try:
        kwargs = {
            "name": title,
            "details": details,
            "state": state,
            "large_image": image_url or "",
            "large_text": title,
            "start": start or int(time.time()),
            "status_display_type": _StatusDisplayType.NAME,
        }
        kwargs = {k: v for k, v in kwargs.items() if v}
        _rpc.update(**kwargs)
        log.info(f"Discord RPC updated: {title}")
    except ConnectionResetError:
        log.debug("Discord connection reset, will reconnect")
        disconnect()
    except BrokenPipeError:
        log.debug("Discord pipe broken, will reconnect")
        disconnect()
    except Exception as e:
        log.debug(f"Discord RPC update failed: {e}")
        disconnect()


def clear_presence():
    """Clear the current Discord Rich Presence."""
    if not _connected or _rpc is None:
        return
    try:
        _rpc.clear()
        log.info("Discord RPC cleared")
    except Exception as e:
        log.debug(f"Discord RPC clear failed: {e}")
        disconnect()


# --- END DISCORD RPC ---