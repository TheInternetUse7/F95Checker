# --- BEGIN DISCORD RPC ---
"""
Discord Rich Presence sidecar module for F95Checker.
Detects running games from the library and shows them on Discord.
"""
from modules.discord_rpc.orchestrator import start_background_thread, stop, is_running

__all__ = ["start_background_thread", "stop", "is_running"]
# --- END DISCORD RPC ---