"""Persistent Playwright Chromium session management.

The persisted `user-data-dir` profile is the auth token. At runtime we reuse it
in **new headless** mode (`--headless=new`) -- the mode that survives corporate
Context-Aware Access -- so no window ever appears.

A single long-lived context + page is reused across MCP tool calls; an
asyncio.Lock serializes search navigations (MVP: no concurrency).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from . import config
from .errors import NotLoggedInError

DRIVE_HOME = "https://drive.google.com/drive/my-drive"
# A URL on any of these hosts/paths means we got bounced to login.
LOGIN_HOST_MARKERS = ("accounts.google.com", "ServiceLogin", "signin")
# Session cookies that prove an authenticated Google session is present.
SESSION_COOKIES = ("SAPISID", "__Secure-1PSID")

# Launch args for the validated "new headless" mode.
_BASE_ARGS = ["--no-first-run", "--no-default-browser-check"]


class BrowserSession:
    """Owns one persistent Chromium context for the server's lifetime."""

    def __init__(self, profile: Path | None = None, *, headed: bool = False) -> None:
        self._profile = profile or config.profile_dir()
        self._headed = headed
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    @property
    def profile(self) -> Path:
        return self._profile

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def start(self) -> BrowserContext:
        """Launch (once) and return the persistent context."""
        if self._ctx is not None:
            return self._ctx
        if not self._headed and not self._profile.exists():
            raise NotLoggedInError(
                f"No Drive session at {self._profile}. Run `drive-session-mcp login` first."
            )
        self._profile.mkdir(parents=True, exist_ok=True)

        args = list(_BASE_ARGS)
        launch_kwargs: dict = {"user_data_dir": str(self._profile), "args": args}
        if self._headed:
            launch_kwargs["headless"] = False
        else:
            # "new headless": full browser, headless, far less fingerprintable than
            # the default headless-shell. This is the configuration the probe passed.
            launch_kwargs["headless"] = False
            args.append("--headless=new")

        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(**launch_kwargs)
        return self._ctx

    async def page(self) -> Page:
        """Return the reusable page, creating it on first use."""
        ctx = await self.start()
        if self._page is None or self._page.is_closed():
            self._page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        return self._page

    async def context(self) -> BrowserContext:
        return await self.start()

    async def has_session_cookies(self) -> bool:
        """True if the profile carries Google session cookies."""
        ctx = await self.start()
        cookies = await ctx.cookies()
        names = {c["name"] for c in cookies if "google.com" in c.get("domain", "")}
        return "SAPISID" in names

    async def health(self) -> dict:
        """Probe whether the session is currently usable.

        Returns a dict: {cookies: bool, drive_reachable: bool, url: str}.
        """
        page = await self.page()
        has_cookies = await self.has_session_cookies()
        await page.goto(DRIVE_HOME, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        url = page.url
        reachable = not any(m in url for m in LOGIN_HOST_MARKERS)
        return {"cookies": has_cookies, "drive_reachable": reachable, "url": url}

    async def aclose(self) -> None:
        if self._ctx is not None:
            await self._ctx.close()
            self._ctx = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None
        self._page = None

    async def __aenter__(self) -> "BrowserSession":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()
