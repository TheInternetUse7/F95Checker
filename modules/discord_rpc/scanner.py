# --- BEGIN DISCORD RPC ---
"""
Process scanner that detects running games from the F95Checker library.
Uses psutil to enumerate running processes and match them against known game executables.
"""
import logging
import pathlib

import psutil
from common.structs import Game

log = logging.getLogger(__name__)

# System paths to skip (Windows built-in / Program Files)
_SKIP_PREFIXES = (
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
)


def build_exe_map() -> dict[str, Game]:
    """Build a reverse map from resolved executable paths to Game objects.

    Uses globals.games and globals.settings.default_exe_dir to resolve relative paths.

    Returns:
        dict mapping lowercase resolved exe path -> Game
    """
    from modules import globals

    exe_map: dict[str, Game] = {}
    default_exe_dir = globals.settings.default_exe_dir.get(globals.os, "")

    for game in globals.games.values():
        for exe in game.executables:
            if not exe:
                continue
            try:
                if pathlib.Path(exe).is_absolute():
                    resolved = pathlib.Path(exe).resolve()
                else:
                    if default_exe_dir:
                        resolved = (pathlib.Path(default_exe_dir) / exe).resolve()
                    else:
                        resolved = pathlib.Path(exe).resolve()
            except Exception:
                continue
            key = str(resolved).lower()
            exe_map[key] = game
            # Also map just the filename for cases where full path differs
            exe_name = resolved.name.lower()
            if exe_name not in exe_map:
                exe_map[exe_name] = game

    return exe_map


def scan_running_games() -> Game | None:
    """Scan running processes and find a game from the F95Checker library.

    Returns the first matched Game, or None if no game is detected.

    Implements the "child process rule": counts all processes from the matched game's
    parent directory. Only clears presence when count reaches 0.
    """
    exe_map = build_exe_map()
    if not exe_map:
        return None

    matched_game = None
    matched_dir = None

    for proc in psutil.process_iter(["exe", "name"]):
        try:
            exe_path = proc.info.get("exe")
            if not exe_path:
                continue

            exe_lower = exe_path.lower()

            # Skip system processes
            skip = False
            for prefix in _SKIP_PREFIXES:
                if exe_lower.startswith(prefix):
                    skip = True
                    break
            if skip:
                continue

            # Try full path match
            game = exe_map.get(exe_lower)
            if game is None:
                # Try filename-only match
                exe_name = pathlib.Path(exe_lower).name
                game = exe_map.get(exe_name)

            if game is not None:
                matched_game = game
                matched_dir = str(pathlib.Path(exe_path).parent).lower()
                break

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue

    return matched_game


def count_processes_in_dir(dir_path: str) -> int:
    """Count running processes whose executable is in the given directory.

    Used to implement the child process rule: only clear Discord presence when all
    processes from the game's directory have exited.
    """
    count = 0
    for proc in psutil.process_iter(["exe"]):
        try:
            exe = proc.info.get("exe")
            if exe and str(pathlib.Path(exe).parent).lower() == dir_path:
                count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
    return count


# --- END DISCORD RPC ---