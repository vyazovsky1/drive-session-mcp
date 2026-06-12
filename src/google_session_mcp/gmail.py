"""Gmail operations: search threads, read messages, send email.

  * search        -> navigate to Gmail search URL, extract thread list from DOM
  * get_thread    -> navigate to thread URL, extract message content from DOM
  * send_email    -> navigate to compose URL with pre-filled fields, click Send
  * save_draft    -> same as send_email but close the compose window instead

The DOM selectors used here target Gmail's classic/standard web app class names,
which have been stable for years. They are intentionally not using the obfuscated
JS-bundle API (which changes with each deploy) to reduce fragility.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from .browser import LOGIN_HOST_MARKERS
from .errors import GmailError, SessionExpiredError

GMAIL_BASE     = "https://mail.google.com/mail/u/0"
GMAIL_SEARCH   = GMAIL_BASE + "/#search/{q}"
GMAIL_THREAD   = GMAIL_BASE + "/#all/{thread_id}"
GMAIL_COMPOSE  = GMAIL_BASE + "/?view=cm&to={to}&su={subject}&body={body}&fs=1&tf=c"

SETTLE_MS      = 7000
THREAD_SETTLE  = 5000

# ---------------------------------------------------------------------------
# DOM extraction — thread list
#
# These class names target the classic Gmail web app. They are obfuscated but
# have remained stable across many years. If Google changes them, the selectors
# return empty strings gracefully (no crash).
# ---------------------------------------------------------------------------

_EXTRACT_THREADS_JS = r"""() => {
    const threads = [];
    document.querySelectorAll('tr.zA').forEach(row => {
        // Thread ID: new Gmail UI stores it on a child span, not the tr itself
        const threadSpan = row.querySelector('[data-legacy-thread-id]');
        const thread_id  = threadSpan ? threadSpan.getAttribute('data-legacy-thread-id') : '';

        // Subject text is the content of .bqe (read) or .bog (unread) span
        const subjectEl = row.querySelector('.bqe') || row.querySelector('.bog');
        // Sender: .yW is the sender cell; .zF is the display name inside
        const senderEl  = row.querySelector('.yW .zF') || row.querySelector('.yW');
        // Snippet: .y2 holds the preview text
        const snippetEl = row.querySelector('.y2');
        // Date: .xW holds the formatted date/time
        const dateEl    = row.querySelector('.xW span[title]') || row.querySelector('.xW');

        threads.push({
            id:      thread_id,
            subject: subjectEl ? subjectEl.textContent.trim() : '',
            sender:  senderEl  ? (senderEl.getAttribute('email') || senderEl.textContent.trim()) : '',
            snippet: snippetEl ? snippetEl.textContent.trim() : '',
            date:    dateEl    ? (dateEl.getAttribute('title') || dateEl.textContent.trim()) : '',
            unread:  row.classList.contains('zE'),
        });
    });
    return threads;
}"""


# ---------------------------------------------------------------------------
# DOM extraction — thread/message content
# ---------------------------------------------------------------------------

_EXTRACT_MESSAGES_JS = r"""() => {
    const messages = [];
    // Each message block in a thread
    document.querySelectorAll('.adn').forEach(block => {
        // Sender name and email
        const nameEl  = block.querySelector('.gD');
        const emailEl = nameEl;   // email is in the 'email' attribute of .gD
        // Date tooltip
        const dateEl  = block.querySelector('.g3, .gH .gI span, [data-tooltip]');
        // Message body (.a3s is the decoded HTML body container)
        const bodyEl  = block.querySelector('.a3s');
        // Subject is the same for all messages in thread, grab from page title area
        const subject = document.querySelector('h2.hP')?.textContent?.trim() || '';
        messages.push({
            subject:    subject,
            from_name:  nameEl  ? nameEl.textContent.trim() : '',
            from_email: nameEl  ? (nameEl.getAttribute('email') || '') : '',
            date:       dateEl  ? (dateEl.getAttribute('data-tooltip') || dateEl.textContent.trim()) : '',
            body:       bodyEl  ? bodyEl.innerText.trim() : '',
        });
    });
    return messages;
}"""


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

DEFAULT_RESULT_LIMIT = 20


async def search(
    session,
    query: str,
    *,
    max_results: int = DEFAULT_RESULT_LIMIT,
    settle_ms: int = SETTLE_MS,
) -> list[dict[str, Any]]:
    """Search Gmail and return matching thread metadata.

    Navigates to the Gmail search URL and extracts the thread list from the
    rendered DOM. Returns a list of dicts with ``id``, ``subject``, ``sender``,
    ``snippet``, ``date``, and ``unread``.
    """
    # Do NOT percent-encode the query — Gmail's hash-fragment router treats
    # %3A differently from :, breaking operators like "in:inbox".
    url = f"{GMAIL_BASE}/#search/{query}"

    async with session.lock:
        ctx  = await session.context()
        page = await session.page()

        await page.goto(url, wait_until="domcontentloaded")
        if any(m in page.url for m in LOGIN_HOST_MARKERS):
            raise SessionExpiredError(
                "Gmail redirected to login. Run `google-browser-mcp login`."
            )

        # Wait for the thread-list rows to render before extracting.
        try:
            await page.wait_for_selector("tr.zA", timeout=10_000)
        except Exception:
            pass  # empty inbox or slow load — try extracting anyway

        threads: list[dict] = await page.evaluate(_EXTRACT_THREADS_JS)

    # Filter out rows with no id (rendering artifacts)
    threads = [t for t in threads if t.get("id")]
    return threads[:max_results]


# ---------------------------------------------------------------------------
# get_thread
# ---------------------------------------------------------------------------

async def get_thread(
    session,
    thread_id: str,
    *,
    settle_ms: int = THREAD_SETTLE,
) -> list[dict[str, Any]]:
    """Fetch all messages in a Gmail thread.

    `thread_id` is the hex id returned by ``search``.
    Returns a list of message dicts with ``subject``, ``from_name``,
    ``from_email``, ``date``, and ``body``.
    """
    url = GMAIL_THREAD.format(thread_id=thread_id)

    async with session.lock:
        ctx  = await session.context()
        page = await session.page()

        await page.goto(url, wait_until="domcontentloaded")
        if any(m in page.url for m in LOGIN_HOST_MARKERS):
            raise SessionExpiredError(
                "Gmail redirected to login. Run `google-browser-mcp login`."
            )
        await page.wait_for_timeout(settle_ms)

        messages: list[dict] = await page.evaluate(_EXTRACT_MESSAGES_JS)

    return messages


# ---------------------------------------------------------------------------
# send_email  (compose + click Send)
# ---------------------------------------------------------------------------

_SEND_SELECTORS = [
    '[aria-label*="Send" i]',
    '[data-tooltip*="Send" i]',
    '.T-I.J-J5-Ji.aoO.v7.T-I-atl.L3',   # classic Gmail send button class
]

_COMPOSE_READY_SEL = [
    '[aria-label="To"]',
    '[name="to"]',
    '.agP.aFw',  # To field in compose window
]


async def send_email(
    session,
    to: str,
    subject: str,
    body: str,
    *,
    settle_ms: int = 4000,
) -> dict[str, Any]:
    """Compose and send an email.

    Navigates to the Gmail compose URL with pre-filled ``to``, ``subject``, and
    ``body``, waits for the compose window to be ready, then clicks Send.

    Returns ``{status, to, subject}`` on success.

    Warning: this sends a real email. Confirm recipients before calling.
    """
    url = GMAIL_COMPOSE.format(
        to=quote(to),
        subject=quote(subject),
        body=quote(body),
    )

    async with session.lock:
        ctx  = await session.context()
        page = await session.page()

        await page.goto(url, wait_until="domcontentloaded")
        if any(m in page.url for m in LOGIN_HOST_MARKERS):
            raise SessionExpiredError(
                "Gmail redirected to login. Run `google-browser-mcp login`."
            )

        # Wait for compose window to be ready
        ready = False
        for sel in _COMPOSE_READY_SEL:
            try:
                await page.wait_for_selector(sel, timeout=6000)
                ready = True
                break
            except Exception:
                continue

        if not ready:
            await page.wait_for_timeout(settle_ms)

        # Click Send
        clicked = False
        for sel in _SEND_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            raise GmailError(
                "Could not find the Send button in the compose window. "
                "The Gmail UI may have changed."
            )

        await page.wait_for_timeout(2000)

    return {"status": "sent", "to": to, "subject": subject}


# ---------------------------------------------------------------------------
# save_draft  (compose + close to save as draft)
# ---------------------------------------------------------------------------

_CLOSE_SELECTORS = [
    '[aria-label*="Close" i]',
    '[aria-label*="Minimize" i]',
    '.Ha.Z',   # close icon in compose window
]


async def save_draft(
    session,
    to: str,
    subject: str,
    body: str,
    *,
    settle_ms: int = 4000,
) -> dict[str, Any]:
    """Save an email as a draft without sending.

    Same as ``send_email`` but closes the compose window instead of clicking
    Send, triggering Gmail's auto-save behaviour.

    Returns ``{status, to, subject}`` on success.
    """
    url = GMAIL_COMPOSE.format(
        to=quote(to),
        subject=quote(subject),
        body=quote(body),
    )

    async with session.lock:
        ctx  = await session.context()
        page = await session.page()

        await page.goto(url, wait_until="domcontentloaded")
        if any(m in page.url for m in LOGIN_HOST_MARKERS):
            raise SessionExpiredError(
                "Gmail redirected to login. Run `google-browser-mcp login`."
            )

        for sel in _COMPOSE_READY_SEL:
            try:
                await page.wait_for_selector(sel, timeout=6000)
                break
            except Exception:
                continue

        await page.wait_for_timeout(settle_ms)

        # Close the compose window — Gmail auto-saves as draft
        for sel in _CLOSE_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    break
            except Exception:
                continue

        await page.wait_for_timeout(1500)

    return {"status": "draft_saved", "to": to, "subject": subject}
