"""Command-line entry point: `login`, `serve` (default), `search`, `fetch`, `selftest`."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from . import config, drive
from .browser import BrowserSession
from .errors import DriveError, NotLoggedInError, SessionExpiredError
from .login import run_login

FOLDER_MIME = "application/vnd.google-apps.folder"


def _fetchable(results: list[dict]) -> tuple[dict | None, dict | None]:
    """Pick a representative (native doc, binary file), skipping folders."""
    native = next((r for r in results if r.get("export_format")), None)
    binary = next(
        (
            r
            for r in results
            if r["mimeType"]
            and r["mimeType"] != FOLDER_MIME
            and not r["mimeType"].startswith("application/vnd.google-apps")
        ),
        None,
    )
    return native, binary


def _print_results(results: list[dict], limit: int = 20) -> None:
    print(f"{len(results)} result(s):")
    for r in results[:limit]:
        hint = f"  export_format={r['export_format']}" if r.get("export_format") else ""
        print(f"  - {r['name']}")
        print(f"      id={r['id']}  mime={r['mimeType']}{hint}")
        meta = [f"{k}={r[k]}" for k in ("owner", "folder", "modified") if r.get(k)]
        if meta:
            print(f"      {'  '.join(meta)}")


async def _do_search(
    profile: Path | None, query: str, ftype: str | None, limit: int
) -> int:
    session = BrowserSession(profile)
    try:
        filters = {"type": ftype} if ftype else None
        results = await drive.search(session, query, filters, limit=limit)
        _print_results(results, limit=limit)
        return 0
    finally:
        await session.aclose()


async def _do_fetch(
    profile: Path | None,
    file_id: str,
    dest: str | None,
    fmt: str | None,
    mime: str | None,
    modified: str | None,
    name: str | None = None,
) -> int:
    session = BrowserSession(profile)
    try:
        info = await drive.fetch(
            session,
            file_id,
            dest_dir=dest,
            export_format=fmt,
            mime_type=mime,
            modified=modified,
            name=name,
        )
        ok = Path(info["path"]).exists() and (info["bytes"] or 0) > 0
        origin = "from cache" if info.get("cached") else "downloaded"
        print(f"fetched ({origin}) -> {info}")
        print("VERIFIED: file exists on disk" if ok else "WARNING: file missing/empty")
        return 0 if ok else 1
    finally:
        await session.aclose()


async def _selftest(profile: Path | None, query: str) -> int:
    """Headless end-to-end: search, then fetch a real doc + a real binary.

    Asserts each fetched file lands on disk.
    """
    session = BrowserSession(profile)
    try:
        print(f"[selftest] profile: {session.profile}")
        health = await session.health()
        print(f"[selftest] health: {health}")
        if not health["drive_reachable"]:
            print("[selftest] NOT reachable -- session likely expired. Run `login`.")
            return 2

        results = await drive.search(session, query)
        print(f"[selftest] search('{query}') -> {len(results)} files")
        _print_results(results, limit=5)
        if not results:
            print("[selftest] no results; search path OK but nothing to fetch.")
            return 0

        native, binary = _fetchable(results)
        targets = [t for t in (native, binary) if t]
        if not targets:
            print("[selftest] results were all folders; rerun with a query that "
                  "matches files, e.g. --query type:document")
            return 0

        rc = 0
        for t in targets:
            kind = "native-export" if t.get("export_format") else "binary"
            info = await drive.fetch(
                session,
                t["id"],
                export_format=t.get("export_format"),
                mime_type=t["mimeType"],
                name=t.get("name"),
            )
            on_disk = Path(info["path"]).exists() and info["bytes"] > 0
            print(f"[selftest] {kind}: {t['name']} -> {info}")
            print(f"[selftest]   on disk: {'YES' if on_disk else 'NO'}")
            rc = rc or (0 if on_disk else 1)
        return rc
    finally:
        await session.aclose()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="drive-session-mcp",
        description="Browser-session Google Drive MCP server.",
    )
    p.add_argument(
        "--profile",
        help="persistent browser profile dir (overrides DRIVE_MCP_PROFILE)",
    )
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("login", help="open a visible browser to authenticate once")
    sub.add_parser("serve", help="run the MCP stdio server (default)")

    sc = sub.add_parser("search", help="search Drive and print results")
    sc.add_argument("--query", required=True, help="search text (may include operators)")
    sc.add_argument("--type", dest="ftype", help="type filter, e.g. document, pdf, spreadsheet")
    sc.add_argument("--limit", type=int, default=drive.DEFAULT_SEARCH_LIMIT,
                    help=f"max results to return (default {drive.DEFAULT_SEARCH_LIMIT})")

    fc = sub.add_parser("fetch", help="download one file by id")
    fc.add_argument("--id", required=True, help="Drive file id (from `search`)")
    fc.add_argument("--dest", help="destination dir (defaults to configured download dir)")
    fc.add_argument("--format", dest="fmt", help="export format for native docs (pdf/docx/xlsx/txt)")
    fc.add_argument("--mime", help="file mime type (lets fetch pick the export endpoint)")
    fc.add_argument("--modified", help="file's modified date from search; reuses a cached "
                    "copy only when it matches (re-fetches updated docs)")
    fc.add_argument("--name", help="original Drive document name (recorded in the manifest)")

    st = sub.add_parser("selftest", help="headless search+fetch smoke test")
    st.add_argument("--query", default="report", help="search query for the smoke test")

    args = p.parse_args(argv)
    profile = Path(args.profile).expanduser() if args.profile else None
    cmd = args.cmd or "serve"

    if profile is not None:
        # Make the override visible to BrowserSession (used by the server lifespan).
        os.environ[config.ENV_PROFILE] = str(profile)

    eff = profile or config.profile_dir()
    try:
        if cmd == "login":
            return run_login(profile)
        if cmd == "search":
            return asyncio.run(_do_search(eff, args.query, args.ftype, args.limit))
        if cmd == "fetch":
            return asyncio.run(
                _do_fetch(eff, args.id, args.dest, args.fmt, args.mime, args.modified, args.name)
            )
        if cmd == "selftest":
            return asyncio.run(_selftest(eff, args.query))
    except (NotLoggedInError, SessionExpiredError, DriveError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # serve: import lazily so the other commands don't require the mcp package path.
    from .server import run

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
