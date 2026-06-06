"""Shared exception types."""

from __future__ import annotations


class SessionExpiredError(RuntimeError):
    """Raised when the persisted browser session is no longer authenticated.

    The Drive web app redirects to accounts.google.com, or a fetch returns a
    login page instead of file bytes. Recovery (for now) is manual: re-run
    `drive-session-mcp login`.
    """


class NotLoggedInError(RuntimeError):
    """Raised when no usable profile/session exists yet (login never ran)."""


class DriveError(RuntimeError):
    """Base class for Drive operation failures with a user-facing message."""


class FileNotFoundError(DriveError):  # noqa: A001 - intentional domain-specific name
    """Raised when a file id does not exist or is not accessible (HTTP 404)."""


class AccessDeniedError(DriveError):
    """Raised when the session may not access the file (HTTP 403)."""
