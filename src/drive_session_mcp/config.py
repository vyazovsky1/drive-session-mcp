"""Runtime configuration: filesystem locations, resolved from env with defaults.

Single source of truth shared by `login`, `BrowserSession`, and `drive.fetch`.
The persistent profile dir *is* the auth token, so it lives in a stable
per-user location independent of the current working directory.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Environment variable overrides.
ENV_PROFILE = "DRIVE_MCP_PROFILE"
ENV_DOWNLOAD_DIR = "DRIVE_MCP_DOWNLOAD_DIR"


def _local_app_data() -> Path:
    """Best-effort per-user data root, cross-platform."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base)
    # XDG / fallback
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base)
    return Path.home() / ".local" / "share"


def _downloads_root() -> Path:
    if os.name == "nt":
        user = os.environ.get("USERPROFILE")
        if user:
            return Path(user) / "Downloads"
    return Path.home() / "Downloads"


def profile_dir() -> Path:
    """Persistent Playwright profile dir (the reusable session jar)."""
    override = os.environ.get(ENV_PROFILE)
    if override:
        return Path(override).expanduser()
    return _local_app_data() / "drive-session-mcp" / "profile"


def download_dir() -> Path:
    """Default destination for fetched files (per-call dest_dir overrides this)."""
    override = os.environ.get(ENV_DOWNLOAD_DIR)
    if override:
        return Path(override).expanduser()
    return _downloads_root() / "drive-session-mcp"
