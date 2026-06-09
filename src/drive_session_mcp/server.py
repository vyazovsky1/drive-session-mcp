"""FastMCP stdio server exposing `drive_search` and `drive_fetch`.

The persistent browser session is created lazily (first tool call) and torn down
on shutdown via the server lifespan, so startup is instant and a missing login
surfaces as a clear error on first use.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import drive
from .browser import BrowserSession


@asynccontextmanager
async def _lifespan(server: FastMCP):
    session = BrowserSession()
    try:
        yield {"session": session}
    finally:
        await session.aclose()


mcp = FastMCP("drive-session", lifespan=_lifespan)


def _session(ctx: Context) -> BrowserSession:
    return ctx.request_context.lifespan_context["session"]


@mcp.tool()
async def drive_search(
    ctx: Context,
    query: str,
    filters: dict[str, Any] | None = None,
    limit: int = drive.DEFAULT_SEARCH_LIMIT,
) -> list[dict[str, Any]]:
    """Search Google Drive and return matching file metadata.

    Args:
        query: Free-text search. May include Drive operators (e.g. ``type:pdf``).
        filters: Optional structured filters mapped to Drive search operators,
            e.g. ``{"type": "document", "owner": "me", "after": "2025-01-01"}``.
        limit: Maximum number of results to return (default 20). Larger values
            scroll the results to load more pages.

    Returns a list of files, each with: ``id``, ``name``, ``mimeType``, ``owner``,
    ``folder`` (parent id), ``modified``, and ``export_format`` (suggested format
    for Google-native docs; ``null`` for binary files). Pass ``export_format`` to
    ``drive_fetch`` for native docs.
    """
    return await drive.search(_session(ctx), query, filters, limit=limit)


@mcp.tool()
async def drive_fetch(
    ctx: Context,
    file_id: str,
    dest_dir: str | None = None,
    export_format: str | None = None,
    mime_type: str | None = None,
    modified: str | None = None,
) -> dict[str, Any]:
    """Download a Drive file locally, auto-exporting Google-native docs.

    Fetches are cached in a ``.drive_metadata.json`` manifest in the destination
    dir. Re-fetching the same file returns the existing local copy without
    downloading again, as long as it is still on disk and -- when ``modified`` is
    passed -- matches the recorded value.

    Args:
        file_id: The Drive file id (from ``drive_search``).
        dest_dir: Destination directory. Defaults to the configured download dir.
        export_format: Export format for Google-native docs (e.g. ``txt``,``pdf``,
            ``docx``, ``xlsx``). Use the ``export_format`` hint from search.
        mime_type: The file's mime type (from search); lets fetch pick the right
            export endpoint. Omit for binary files.
        modified: The file's ``modified`` ("date updated") value from search.
            When provided, a cached copy is reused only if its recorded value
            matches, so an updated document is re-downloaded.

    Returns ``{path, bytes, format, exported, id, url, modified, fetched_at,
    cached}`` (``cached`` is True when served from the local manifest).
    """
    return await drive.fetch(
        _session(ctx),
        file_id,
        dest_dir=dest_dir,
        export_format=export_format,
        mime_type=mime_type,
        modified=modified,
    )


def run() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")
