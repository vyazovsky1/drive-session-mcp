"""Drive operations: search (page-nav + response intercept) and fetch.

  * search    -> navigate the headless page to drive.google.com/drive/search?q=...
                 and intercept the internal files-list JSON (HTTP 200).
  * export    -> docs.google.com/.../export?format=...   (Google-native docs)
  * download  -> drive.google.com/uc?id=...&export=download   (binary files)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import config
from .browser import LOGIN_HOST_MARKERS
from .errors import (
    AccessDeniedError,
    DriveError,
    FileNotFoundError,
    SessionExpiredError,
)

SEARCH_URL = "https://drive.google.com/drive/search?q={q}"

# The Drive web app fetches search results from this internal RPC. It returns
# "application/json+protobuf" -- a nested *positional* JSON array (not an object
# with an `items` key). Field positions confirmed by recon against the live tenant.
SEARCH_RPC_MARKER = "SearchItems"

# Positional indices within a SearchItems result row.
_ROW_ID = 0
_ROW_PARENTS = 1
_ROW_NAME = 2
_ROW_MIME = 3
_ROW_MODIFIED_MS = 10

# A Drive file id is a longish base64url-ish token; used to recognize result rows
# inside the otherwise-unlabeled protobuf-JSON array.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")

# Google-native mime -> (default export format, export URL template).
GOOGLE_NATIVE: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "pdf",
        "https://docs.google.com/document/d/{id}/export?format={fmt}",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "xlsx",
        "https://docs.google.com/spreadsheets/d/{id}/export?format={fmt}",
    ),
    "application/vnd.google-apps.presentation": (
        "pdf",
        "https://docs.google.com/presentation/d/{id}/export/{fmt}",
    ),
}

# Default template used when an export is requested but the mime type is unknown.
_DEFAULT_EXPORT_TMPL = GOOGLE_NATIVE["application/vnd.google-apps.document"][1]


# --------------------------------------------------------------------------- #
# Query building
# --------------------------------------------------------------------------- #
def build_query(query: str, filters: dict[str, Any] | None = None) -> str:
    """Compose a Drive search `q=` expression from free text + structured filters.

    Known filter keys map to Drive search operators; unknown keys pass through as
    `key:value`. `query` may already contain operators and is kept verbatim.
    """
    parts: list[str] = []
    if query and query.strip():
        parts.append(query.strip())
    for key, value in (filters or {}).items():
        if value is None or value == "":
            continue
        parts.append(f"{key}:{value}")
    return " ".join(parts)


def search_url(query: str, filters: dict[str, Any] | None = None) -> str:
    return SEARCH_URL.format(q=quote(build_query(query, filters)))


# --------------------------------------------------------------------------- #
# SearchItems protobuf-JSON parsing
# --------------------------------------------------------------------------- #
def parse_protojson(body: bytes) -> Any:
    """Decode a Google "json+protobuf" body, stripping the XSSI prefix."""
    raw = body.decode("utf-8", "ignore")
    for prefix in (")]}'\n", ")]}'"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _looks_like_row(node: Any) -> bool:
    """True if `node` is a SearchItems result row: [id, parents, name, mime, ...]."""
    return (
        isinstance(node, list)
        and len(node) > _ROW_MIME
        and isinstance(node[_ROW_ID], str)
        and _ID_RE.match(node[_ROW_ID]) is not None
        and isinstance(node[_ROW_NAME], str)
        and isinstance(node[_ROW_MIME], str)
        and "/" in node[_ROW_MIME]
    )


def find_rows(node: Any, out: list[list]) -> None:
    """Recursively collect result rows from the nested protobuf-JSON array."""
    if _looks_like_row(node):
        out.append(node)
    elif isinstance(node, list):
        for child in node:
            find_rows(child, out)


def _ms_to_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def _folder_of_row(row: list) -> str | None:
    parents = row[_ROW_PARENTS] if len(row) > _ROW_PARENTS else None
    if isinstance(parents, list) and parents and isinstance(parents[0], str):
        return parents[0]
    return None


def normalize_row(row: list) -> dict[str, Any]:
    """Map a positional SearchItems row to our stable metadata shape.

    Owner is not carried inline in the search response (it appears only as a
    person-id reference), so it is reported as None for now.
    """
    mime = row[_ROW_MIME] if len(row) > _ROW_MIME else ""
    native = GOOGLE_NATIVE.get(mime)
    modified = row[_ROW_MODIFIED_MS] if len(row) > _ROW_MODIFIED_MS else None
    return {
        "id": row[_ROW_ID],
        "name": row[_ROW_NAME] or "(untitled)",
        "mimeType": mime,
        "owner": None,
        "folder": _folder_of_row(row),
        "modified": _ms_to_iso(modified),
        "export_format": native[0] if native else None,
    }


# --------------------------------------------------------------------------- #
# Login-page detection (port of probe.looks_like_login_page)
# --------------------------------------------------------------------------- #
def looks_like_login_page(body: bytes, headers: dict) -> bool:
    ctype = (headers.get("content-type", "") or "").lower()
    if "text/html" not in ctype:
        return False
    head = body[:4000].decode("utf-8", "ignore").lower()
    return any(
        m in head for m in ("sign in", "accounts.google.com", "couldn't sign you in")
    )


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
DEFAULT_SEARCH_LIMIT = 20


async def search(
    session,
    query: str,
    filters: dict[str, Any] | None = None,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    settle_ms: int = 6000,
) -> list[dict[str, Any]]:
    """Search Drive and return up to `limit` normalized file metadata rows.

    Navigates the headless page to the Drive search URL and intercepts the
    internal SearchItems RPC response (protobuf-JSON), which is the call that
    carries the result rows. Drive returns one page of rows per RPC; when more
    than that are requested, the results list is scrolled to trigger further
    SearchItems calls until `limit` rows are collected (or no more arrive).
    """
    limit = max(1, int(limit))
    async with session.lock:
        ctx = await session.context()
        page = await session.page()

        seen_ids: set[str] = set()
        rows: list[list] = []

        async def on_response(resp) -> None:
            if SEARCH_RPC_MARKER not in resp.url:
                return
            try:
                body = await resp.body()
            except Exception:
                return
            data = parse_protojson(body)
            if data is None:
                return
            found: list[list] = []
            find_rows(data, found)
            for r in found:
                fid = r[_ROW_ID]
                if fid not in seen_ids:
                    seen_ids.add(fid)
                    rows.append(r)

        ctx.on("response", on_response)
        try:
            await page.goto(search_url(query, filters), wait_until="domcontentloaded")
            if any(m in page.url for m in LOGIN_HOST_MARKERS):
                raise SessionExpiredError(
                    "Drive redirected to login. Run `drive-session-mcp login` and retry."
                )
            await page.wait_for_timeout(settle_ms)

            # Scroll to load more pages until we have `limit` rows or growth stops.
            stagnant = 0
            while len(rows) < limit and stagnant < 3:
                before = len(rows)
                await page.mouse.move(640, 400)
                await page.mouse.wheel(0, 6000)
                await page.wait_for_timeout(1500)
                stagnant = stagnant + 1 if len(rows) <= before else 0
        finally:
            ctx.remove_listener("response", on_response)

        return [normalize_row(r) for r in rows[:limit]]


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
_CD_FILENAME = re.compile(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", re.IGNORECASE)


def _filename_from_headers(headers: dict, fallback: str) -> str:
    cd = headers.get("content-disposition", "") or ""
    m = _CD_FILENAME.search(cd)
    if m:
        return Path(m.group(1)).name
    return fallback


def _resolve_export(mime_type: str | None, export_format: str | None):
    """Return (is_export, fmt, url_template) for a fetch request."""
    if mime_type and mime_type in GOOGLE_NATIVE:
        default_fmt, tmpl = GOOGLE_NATIVE[mime_type]
        return True, (export_format or default_fmt), tmpl
    if export_format:
        # Export requested but mime unknown -> assume a document-style export.
        return True, export_format, _DEFAULT_EXPORT_TMPL
    return False, None, None


# Exports redirect to googleusercontent.com and can be slow for large files, so
# allow well beyond Playwright's 30s default.
FETCH_TIMEOUT_MS = 180_000


async def fetch(
    session,
    file_id: str,
    dest_dir: str | None = None,
    export_format: str | None = None,
    mime_type: str | None = None,
    *,
    timeout_ms: int = FETCH_TIMEOUT_MS,
) -> dict[str, Any]:
    """Download one file to disk, auto-exporting Google-native docs.

    Returns {path, bytes, format, exported}. Raises SessionExpiredError if the
    server hands back a login page instead of file content.
    """
    dest = Path(dest_dir).expanduser() if dest_dir else config.download_dir()
    dest.mkdir(parents=True, exist_ok=True)

    ctx = await session.context()
    is_export, fmt, tmpl = _resolve_export(mime_type, export_format)

    if is_export:
        url = tmpl.format(id=file_id, fmt=fmt)
        fallback_name = f"{file_id}.{fmt}"
    else:
        url = f"https://drive.google.com/uc?id={file_id}&export=download"
        fallback_name = file_id

    resp = await ctx.request.get(url, timeout=timeout_ms)
    body = await resp.body()
    headers = dict(resp.headers)

    if looks_like_login_page(body, headers):
        raise SessionExpiredError(
            "Got a login page instead of file content. "
            "Run `drive-session-mcp login` and retry."
        )
    if resp.status == 404:
        raise FileNotFoundError(
            f"No Drive file with id '{file_id}' (HTTP 404). "
            "Check the id - copy it from `drive-session-mcp search`."
        )
    if resp.status == 403:
        raise AccessDeniedError(
            f"Access denied to file '{file_id}' (HTTP 403). "
            "Your account may not have permission to open it."
        )
    if not resp.ok or len(body) <= 256:
        raise DriveError(
            f"Fetch failed for '{file_id}': HTTP {resp.status}, {len(body)} bytes."
        )

    name = _filename_from_headers(headers, fallback_name)
    out = dest / name
    out.write_bytes(body)
    return {
        "path": str(out),
        "bytes": len(body),
        "format": fmt,
        "exported": is_export,
    }
