"""One-time interactive login: open a visible browser, persist the session.

The user completes the full corporate SSO / 2SV / device-trust flow once. The
resulting `user-data-dir` profile is the reusable token consumed headless at
runtime.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

from . import config
from .browser import DRIVE_HOME, LOGIN_HOST_MARKERS, _BASE_ARGS


async def _login(profile: Path) -> int:
    profile.mkdir(parents=True, exist_ok=True)
    print(f"Launching a visible browser with profile: {profile}")
    print("Log in to your CORPORATE Google account and open Drive.")
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            args=list(_BASE_ARGS),
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(DRIVE_HOME, wait_until="domcontentloaded")
        await asyncio.get_event_loop().run_in_executor(
            None,
            input,
            "\n>>> Finish login, make sure 'My Drive' is visible, then press Enter here... ",
        )
        url = page.url
        cookies = await ctx.cookies()
        names = {c["name"] for c in cookies}
        logged_in = not any(m in url for m in LOGIN_HOST_MARKERS) and "SAPISID" in names
        await ctx.close()

    if logged_in:
        print(f"\nLogin captured. Cookie jar persisted to {profile}")
        print("You can now run the MCP server: drive-session-mcp serve")
        return 0
    print(
        "\nDoes NOT look logged in (no SAPISID cookie / still on a login URL). Try again."
    )
    return 1


def run_login(profile: Path | None = None) -> int:
    """Synchronous entry point for the `login` CLI command."""
    return asyncio.run(_login(profile or config.profile_dir()))
