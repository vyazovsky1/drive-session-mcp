"""Calendar operations: list events and create events.

  * list_events  -> navigate to a calendar month view, intercept the internal
                    `minievents` POST response (protobuf-JSON), parse events.
  * create_event -> navigate to the eventedit URL with pre-filled query params,
                    click Save via Playwright, wait for navigation to confirm.
  * delete_event -> navigate to the event URL, click Delete via Playwright.

The same browser session (cookies) that works for Drive works here — Google
session cookies are scoped to .google.com, so no extra auth is needed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .browser import LOGIN_HOST_MARKERS
from .errors import CalendarError, SessionExpiredError

CALENDAR_BASE    = "https://calendar.google.com/calendar/u/0/r"
MINIEVENTS_MARKER = "minievents"
SETTLE_MS        = 6000


# ---------------------------------------------------------------------------
# Protobuf-JSON parsing
# ---------------------------------------------------------------------------

def _strip_xssi(raw: str) -> str:
    for prefix in (")]}'\n", ")]}'"):
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


def _parse_body(body: bytes) -> Any:
    raw = _strip_xssi(body.decode("utf-8", "ignore"))
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _ms_to_iso(ms: Any) -> str | None:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Event row normalisation
#
# minievents row (positional):
#   [0]  event_id        str   e.g. "abc123_20260615"
#   [1]  [start_ms, end_ms, all_day]
#   [2]  unknown int
#   [3]  unknown int     (0 = organiser, 2 = attendee?)
#   [4]  unknown int
#   [5]  unknown int
#   [6]  modified_ms     int
#   [7]  [title, description, is_organiser, ?, has_video_conf, ...]
#   [8]  rsvp_status     int   (0=needsAction, 1=accepted, 2=declined, 3=tentative)
#   [9]  [attendee_person_ids]
# ---------------------------------------------------------------------------

_RSVP = {0: "needsAction", 1: "accepted", 2: "declined", 3: "tentative"}


def normalize_event(row: list) -> dict[str, Any]:
    time_info = row[1] if len(row) > 1 and isinstance(row[1], list) else []
    details   = row[7] if len(row) > 7 and isinstance(row[7], list) else []

    start_ms = time_info[0] if len(time_info) > 0 else None
    end_ms   = time_info[1] if len(time_info) > 1 else None
    all_day  = bool(time_info[2]) if len(time_info) > 2 else False

    rsvp_code = row[8] if len(row) > 8 and isinstance(row[8], int) else None

    return {
        "id":               row[0] if row else None,
        "start":            _ms_to_iso(start_ms),
        "end":              _ms_to_iso(end_ms),
        "all_day":          all_day,
        "title":            details[0] if len(details) > 0 and details[0] else "(untitled)",
        "description":      details[1] if len(details) > 1 else "",
        "has_video_conf":   bool(details[4]) if len(details) > 4 else False,
        "rsvp":             _RSVP.get(rsvp_code, "unknown") if rsvp_code is not None else None,
    }


def _extract_events(data: Any) -> list[dict[str, Any]]:
    """Walk the minieventsaction.mer protobuf-JSON and collect all event rows."""
    events: list[dict] = []
    if not isinstance(data, list):
        return events
    # Structure: [["minieventsaction.mer", [[calendar_email, [rows...]]]]]
    for top in data:
        if not isinstance(top, list) or len(top) < 2:
            continue
        if top[0] != "minieventsaction.mer":
            continue
        for calendar_block in (top[1] or []):
            if not isinstance(calendar_block, list) or len(calendar_block) < 2:
                continue
            rows = calendar_block[1]
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, list) and row and isinstance(row[0], str):
                    events.append(normalize_event(row))
    return events


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _date_to_week_url(dt: datetime) -> str:
    return f"{CALENDAR_BASE}/week/{dt.year}/{dt.month}/{dt.day}"


def _parse_date(value: str) -> datetime:
    """Parse an ISO date/datetime string (date or datetime)."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:19], fmt[:len(value[:19])])
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {value!r}")


def _eventedit_url(title: str, start: str, end: str, description: str = "") -> str:
    """Build the Calendar eventedit pre-population URL.

    `start` / `end` accept ISO datetime ("2026-07-01T12:00:00") or date ("2026-07-01").
    """
    def _to_gcal_fmt(s: str) -> str:
        # all-day: YYYYMMDD; datetime: YYYYMMDDTHHMMSSZ
        s = s.strip()
        if "T" in s:
            dt = _parse_date(s)
            return dt.strftime("%Y%m%dT%H%M%SZ")
        dt = _parse_date(s)
        return dt.strftime("%Y%m%d")

    params = [
        f"text={quote(title)}",
        f"dates={_to_gcal_fmt(start)}/{_to_gcal_fmt(end)}",
    ]
    if description:
        params.append(f"details={quote(description)}")
    return f"https://calendar.google.com/calendar/r/eventedit?{'&'.join(params)}"


# ---------------------------------------------------------------------------
# list_events
# ---------------------------------------------------------------------------

DEFAULT_SETTLE_MS = 6000


async def list_events(
    session,
    start: str,
    end: str,
    *,
    settle_ms: int = DEFAULT_SETTLE_MS,
) -> list[dict[str, Any]]:
    """List calendar events between `start` and `end` (ISO date strings).

    Navigates to the Calendar month view(s) covering the requested range and
    intercepts the ``minievents`` POST responses to collect event data.
    Returns events sorted by start time; events outside the requested range
    are filtered out.
    """
    try:
        start_dt = _parse_date(start)
        end_dt   = _parse_date(end)
    except ValueError as exc:
        raise CalendarError(str(exc)) from exc

    from datetime import timedelta

    # Navigate week-by-week: the week view is what triggers `minievents`.
    # Build a list of Monday-anchored week starts covering [start_dt, end_dt].
    week_starts: list[datetime] = []
    cur = start_dt - timedelta(days=start_dt.weekday())  # Monday of start week
    while cur <= end_dt:
        week_starts.append(cur)
        cur += timedelta(weeks=1)

    start_ms = start_dt.timestamp() * 1000
    end_ms   = end_dt.timestamp() * 1000

    all_events: list[dict] = []

    async with session.lock:
        ctx  = await session.context()
        page = await session.page()

        captured: list[bytes] = []

        async def on_resp(resp) -> None:
            if MINIEVENTS_MARKER not in resp.url:
                return
            try:
                body = await resp.body()
                captured.append(body)
            except Exception:
                pass

        ctx.on("response", on_resp)
        try:
            for week_start in week_starts:
                url = _date_to_week_url(week_start)
                await page.goto(url, wait_until="domcontentloaded")
                if any(m in page.url for m in LOGIN_HOST_MARKERS):
                    raise SessionExpiredError(
                        "Calendar redirected to login. Run `google-browser-mcp login`."
                    )
                await page.wait_for_timeout(settle_ms)
        finally:
            ctx.remove_listener("response", on_resp)

    for body in captured:
        data = _parse_body(body)
        if data:
            all_events.extend(_extract_events(data))

    # Deduplicate by ID, then filter to requested range
    seen: set[str] = set()
    result: list[dict] = []
    for ev in all_events:
        if ev["id"] in seen:
            continue
        seen.add(ev["id"])
        ev_start = ev.get("start")
        if ev_start:
            try:
                ev_ms = datetime.fromisoformat(ev_start).timestamp() * 1000
                if ev_ms < start_ms or ev_ms > end_ms:
                    continue
            except Exception:
                pass
        result.append(ev)

    result.sort(key=lambda e: e.get("start") or "")
    return result


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------

_SAVE_SELECTORS = [
    'button:has-text("Save")',
    '[aria-label="Save"]',
    '[data-view-name="save"]',
    'button[jsname="r8qRAd"]',
]


async def create_event(
    session,
    title: str,
    start: str,
    end: str,
    description: str = "",
    *,
    settle_ms: int = 4000,
) -> dict[str, Any]:
    """Create a calendar event by navigating to the event editor and clicking Save.

    `start` / `end` are ISO datetime strings (e.g. ``"2026-07-01T12:00:00"``).
    All-day events: pass date-only strings (``"2026-07-01"``).

    Returns ``{status, title, start, end}`` on success.
    """
    url = _eventedit_url(title, start, end, description)

    async with session.lock:
        ctx  = await session.context()
        page = await session.page()

        await page.goto(url, wait_until="domcontentloaded")
        if any(m in page.url for m in LOGIN_HOST_MARKERS):
            raise SessionExpiredError(
                "Calendar redirected to login. Run `google-browser-mcp login`."
            )
        await page.wait_for_timeout(settle_ms)

        clicked = False
        for sel in _SAVE_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            try:
                await page.get_by_role("button", name="Save").first.click(timeout=3000)
                clicked = True
            except Exception:
                pass

        if not clicked:
            raise CalendarError(
                "Could not find the Save button in the event editor. "
                "The Calendar UI may have changed."
            )

        # Wait for navigation away from eventedit (indicates save completed)
        try:
            await page.wait_for_url(
                lambda u: "eventedit" not in u,
                timeout=10_000,
            )
        except Exception:
            pass  # might already have navigated

    return {"status": "created", "title": title, "start": start, "end": end}


# ---------------------------------------------------------------------------
# delete_event
# ---------------------------------------------------------------------------

_DELETE_SELECTORS = [
    '[aria-label="Delete event"]',
    '[data-tooltip="Delete event"]',
    'button:has-text("Delete")',
]

_CONFIRM_SELECTORS = [
    'button:has-text("OK")',
    'button:has-text("Delete")',
    '[aria-label="OK"]',
]


async def delete_event(
    session,
    event_id: str,
    *,
    settle_ms: int = 4000,
) -> dict[str, Any]:
    """Delete a calendar event by clicking the trash icon on its detail page.

    `event_id` is the string id returned by ``list_events``.
    Navigates to the Calendar search URL to locate the event first.
    """
    # Navigate to calendar home and let the user's event data load
    async with session.lock:
        ctx  = await session.context()
        page = await session.page()

        await page.goto(CALENDAR_BASE, wait_until="domcontentloaded")
        if any(m in page.url for m in LOGIN_HOST_MARKERS):
            raise SessionExpiredError(
                "Calendar redirected to login. Run `google-browser-mcp login`."
            )
        await page.wait_for_timeout(settle_ms)

        # Try to find and click the event chip by data-eventid or aria label
        found = False
        try:
            chip = page.locator(f'[data-eventid="{event_id}"]').first
            if await chip.is_visible(timeout=2000):
                await chip.click()
                found = True
        except Exception:
            pass

        if not found:
            raise CalendarError(
                f"Event '{event_id}' not found in the current calendar view. "
                "Use list_events to confirm the id and ensure the event is in the visible date range."
            )

        await page.wait_for_timeout(1500)

        clicked_delete = False
        for sel in _DELETE_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    clicked_delete = True
                    break
            except Exception:
                continue

        if not clicked_delete:
            raise CalendarError("Could not find the Delete button in the event detail popup.")

        # Confirm deletion if a dialog appears
        await page.wait_for_timeout(1000)
        for sel in _CONFIRM_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    break
            except Exception:
                continue

    return {"status": "deleted", "id": event_id}
