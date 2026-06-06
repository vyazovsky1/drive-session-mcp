# drive-session-mcp

A Model Context Protocol (MCP) server that **searches and fetches Google Drive
files by reusing a browser session you authenticate once** — with **no OAuth2
client credentials and no GCP project**.

It works on corporate Workspace tenants that enforce Context-Aware Access: a
one-time `login` opens a visible browser for the full SSO / 2SV / device-trust
flow, and the persisted Playwright profile *is* the token. At runtime the server
reuses that profile **headless and silent** (`--headless=new`), so no window
ever appears.

## Tools

| Tool | What it does |
|------|--------------|
| `drive_search(query, filters?, limit?)` | Search Drive (default 20 results, `limit` to change); returns `id`, `name`, `mimeType`, `owner`, `folder`, `modified`, and an `export_format` hint for Google-native docs. |
| `drive_fetch(file_id, dest_dir?, export_format?, mime_type?)` | Downloads a file locally, auto-exporting Google-native docs (Doc→pdf/docx, Sheet→xlsx, Slides→pdf). |

## Setup (one time)

```powershell
cd C:\Dev\google-drive-wrap
pip install -e .
python -m playwright install chromium
```

> **PATH note:** `pip` may install the `drive-session-mcp` console script into a
> per-user Scripts dir that isn't on your `PATH` (it prints a warning if so). You
> can either run every command as a module — `python -m drive_session_mcp.cli
> <command>` — which always works, or add that Scripts dir to `PATH` once:
>
> ```cmd
> setx PATH "%PATH%;C:\Users\<you>\AppData\Roaming\Python\Python314\Scripts"
> ```
>
> (open a new terminal afterward). The examples below show both forms.

### Authenticate once

```powershell
drive-session-mcp login
# or, if the script isn't on PATH:
python -m drive_session_mcp.cli login
```

A visible Chromium opens. Complete the full corporate SSO, make sure **My Drive**
is visible, then press Enter in the terminal. You should see `Login captured.`

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `DRIVE_MCP_PROFILE` | `%LOCALAPPDATA%\drive-session-mcp\profile` | Persistent browser profile (the session jar). |
| `DRIVE_MCP_DOWNLOAD_DIR` | `%USERPROFILE%\Downloads\drive-session-mcp` | Default fetch destination (overridden per call by `dest_dir`). |

`--profile <dir>` on any command overrides `DRIVE_MCP_PROFILE`.

## Verify it works

### One-shot smoke test

```powershell
drive-session-mcp selftest --query report
# or:
python -m drive_session_mcp.cli selftest --query report
```

Runs a real headless search, then fetches one Google-native doc **and** one
binary file from the results, and asserts each landed on disk (`on disk: YES`).
No window appears. If it reports the session isn't reachable, re-run `login`.

### Verify each step manually

```powershell
# 1. search — prints id / mime / export_format for each match
drive-session-mcp search --query "report" --type document
#    --limit changes how many results come back (default 20):
drive-session-mcp search --query "report" --limit 50

# 2. fetch one of those ids; prints the saved path and confirms it exists on disk
drive-session-mcp fetch --id <FILE_ID> --mime application/vnd.google-apps.document --format pdf

# binary file (no --format/--mime needed):
drive-session-mcp fetch --id <FILE_ID>

# choose where it lands:
drive-session-mcp fetch --id <FILE_ID> --dest C:\temp\drive-out
```

`fetch` prints `VERIFIED: file exists on disk` on success. Then open the file
from the download dir (default `%USERPROFILE%\Downloads\drive-session-mcp`) to
confirm the contents.

## Register with Claude Code

```powershell
claude mcp add drive-session -- drive-session-mcp serve
# or, if the script isn't on PATH:
claude mcp add drive-session -- python -m drive_session_mcp.cli serve
```

## Register with Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "drive-session": {
      "command": "drive-session-mcp",
      "args": ["serve"]
    }
  }
}
```

If `drive-session-mcp` isn't on PATH, use the full path to the console script (or
`"command": "python", "args": ["-m", "drive_session_mcp.cli", "serve"]`).

## Notes & limitations (MVP)

- **Session expiry** is surfaced as a clear error (*"run `drive-session-mcp
  login`"*); auto-refresh is deferred.
- Search loads results by scrolling until `limit` is reached; very large result
  sets (hundreds) are not exhaustively paged.
- Single account, no concurrent downloads, Drive only.
- The persisted profile is sensitive — it grants access to your Drive. Keep it
  local and private (it's excluded by `.gitignore`).

## License

MIT — see [LICENSE](LICENSE).
