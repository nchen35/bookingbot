#!/usr/bin/env python3
"""
UCLA Rec Booking Sniper

Commands log in automatically when the saved session is missing or expired:
credentials come from UCLA_USERNAME / UCLA_PASSWORD (env or .env), and you
approve the Duo push on your phone.

Usage:
  uv run bookingbot.py login                                 # force a fresh login
  uv run bookingbot.py list                                  # interactive sport/date
  uv run bookingbot.py list pb tmr                           # positional + aliases
  uv run bookingbot.py list --sport tennis --date saturday
  uv run bookingbot.py book                                  # fully interactive
  uv run bookingbot.py b t sat 10 3                          # tennis, Sat 10 AM, court 3
  uv run bookingbot.py book --sport pickleball --date saturday --time 10AM
  uv run bookingbot.py book --sport tennis --date +3 --time 2PM --headed
  uv run bookingbot.py book --sport pickleball --date tomorrow --time "10:00 AM" --dry-run
"""

import argparse
import asyncio
import getpass
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Config ────────────────────────────────────────────────────────────────────

SESSION_FILE = Path(__file__).parent / "session.json"
SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
# Optional KEY=VALUE file holding UCLA_USERNAME / UCLA_PASSWORD (gitignored).
ENV_FILE = Path(__file__).parent / ".env"
LOGIN_TIMEOUT_SECS = 180  # how long auto-login waits for SSO + Duo approval
BOOKING_URL = "https://secure.recreation.ucla.edu/booking"
LA_TZ = ZoneInfo("America/Los_Angeles")
BOOKING_ADVANCE_HOURS = 72  # slots open this many hours before the court time
# The facility page shows this many date tabs: today plus the next 3 days. A
# date's tab appears at midnight Pacific, VISIBLE_DATE_TABS - 1 days beforehand.
VISIBLE_DATE_TABS = 4
TAB_APPEAR_GRACE_SECS = 10  # wait this long past midnight before looking for a new tab
TAB_VERIFY_TIMEOUT_SECS = 300  # keep retrying a newly-due tab this long before giving up

# Text on the facility cards as it appears on the booking page.
# Adjust these if the site changes its labels — run with --dry-run to verify navigation.
FACILITY_CARD_NAMES = {
    "pickleball": "SCRC Pickleball Courts",
    "tennis": "SCRC Tennis Courts",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def is_session_expired(url: str) -> bool:
    return any(x in url.lower() for x in (
        "login", "sign-in", "idp.ucla.edu", "shibboleth", "shb.ais.ucla.edu", "logon.ucla.edu",
    ))


async def check_logged_in(page) -> bool:
    """Return True if the page shows a logged-in state.
    Checks both the URL (for redirects) and whether the 'Sign In' button is visible.
    """
    if is_session_expired(page.url):
        return False
    try:
        sign_in = page.get_by_text("Sign In", exact=True)
        visible = await sign_in.is_visible(timeout=3000)
        return not visible
    except Exception:
        return True  # assume logged in if the check itself fails


def format_date_tab(date: datetime) -> str:
    """Format a datetime as it may appear on the booking date tab.
    The site has been observed to use both 'APR 14' and 'TUE APR 14' formats.
    Returns the longer 'TUE APR 14' form; click_date_tab falls back to the short form.
    """
    return date.strftime("%a %b %d").replace(" 0", " ").upper()  # e.g. "TUE APR 14"


def format_date_tab_short(date: datetime) -> str:
    """Short form without weekday, e.g. 'APR 14'."""
    return date.strftime("%b %d").replace(" 0", " ").upper()


def parse_slot_start_minute(s: str) -> int | None:
    """Return the start time of a slot label (e.g. '10 - 10:50 AM', '12 - 12:50 PM')
    as minute-of-day (0-1439), or None if unparseable."""
    m = re.match(
        r'\s*(\d{1,2})(?::(\d{2}))?\s*-\s*\d{1,2}(?::\d{2})?\s*(AM|PM)',
        s.upper(),
    )
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    return hour * 60 + minute


def time_matches_slot(user_time: str, slot_text: str) -> bool:
    """
    Return True if a user-supplied time like '10:00 AM' matches a slot text
    like '10 - 10:30 AM' or '10:30 - 11 AM'.
    Matches on start-hour, start-minute (if non-zero), and AM/PM.
    """
    t = datetime.strptime(user_time.strip().upper(), "%I:%M %p")
    hour_12 = t.hour % 12 or 12  # 0 → 12
    minute = t.minute
    ampm = "AM" if t.hour < 12 else "PM"

    if minute == 0:
        prefix = f"{hour_12} -"      # "10 -" matches "10 - 10:30 AM"
    else:
        prefix = f"{hour_12}:{minute:02d}"  # "10:30" matches "10:30 - 11 AM"

    return prefix in slot_text and ampm in slot_text


# ── Input shortcuts ───────────────────────────────────────────────────────────

WEEKDAYS = {
    "monday": 0, "mon": 0, "mo": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "tu": 1,
    "wednesday": 2, "wed": 2, "we": 2,
    "thursday": 3, "thu": 3, "thurs": 3, "th": 3,
    "friday": 4, "fri": 4, "fr": 4,
    "saturday": 5, "sat": 5, "sa": 5,
    "sunday": 6, "sun": 6, "su": 6,
}

TOMORROW_ALIASES = ("tomorrow", "tmr", "tmrw", "tom")

SPORT_ALIASES = {
    "tennis": "tennis", "t": "tennis", "ten": "tennis",
    "pickleball": "pickleball", "p": "pickleball", "pb": "pickleball", "pickle": "pickleball",
}

# Reservations currently run 8 AM - 8 PM (last slot starts 7 PM), so a bare
# hour is unambiguous: 8-11 → AM, 12 and 1-7 → PM.
FIRST_AM_HOUR = 8


def parse_sport(s: str) -> str:
    sport = SPORT_ALIASES.get(s.strip().lower())
    if sport is None:
        raise ValueError(
            f"Invalid sport '{s}'. Use tennis (t, ten) or pickleball (p, pb, pickle)."
        )
    return sport


def parse_time_shortcut(s: str) -> str:
    """
    Normalize flexible time input to canonical 'H:MM AM/PM' form.
    Accepts: '10AM', '2pm', '10:30 AM', '10:00AM', '2:15 pm', and bare
    '10' / '2' / '10:30' (AM/PM inferred from the 8 AM - 8 PM court hours).
    """
    raw = s.strip().upper().replace(" ", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(AM|PM)?$", raw)
    if not m:
        raise ValueError(
            f"Invalid time format: '{s}'. Try '10', '2PM', or '10:30 AM'."
        )
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if not (1 <= hour <= 12) or not (0 <= minute <= 59):
        raise ValueError(f"Time out of range: '{s}'.")
    ampm = m.group(3) or ("AM" if FIRST_AM_HOUR <= hour < 12 else "PM")
    return f"{hour}:{minute:02d} {ampm}"


def parse_date_shortcut(s: str) -> str:
    """
    Normalize flexible date input to canonical 'YYYY-MM-DD' form.
    Accepts:
      - YYYY-MM-DD          ('2026-04-14')
      - 'today', 'tomorrow'
      - weekday names       ('saturday', 'sat') → next occurrence (7 days if today)
      - '+N' or 'N'         → N days from today
    """
    raw = s.strip().lower()
    today = datetime.now(LA_TZ).date()

    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        pass

    if raw == "today":
        return today.isoformat()
    if raw in TOMORROW_ALIASES:
        return (today + timedelta(days=1)).isoformat()

    if raw.startswith("+") or raw.lstrip("+").isdigit():
        try:
            n = int(raw.lstrip("+"))
            return (today + timedelta(days=n)).isoformat()
        except ValueError:
            pass

    if raw in WEEKDAYS:
        target = WEEKDAYS[raw]
        days_ahead = (target - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7  # "saturday" on a Saturday → next Saturday
        return (today + timedelta(days=days_ahead)).isoformat()

    raise ValueError(
        f"Invalid date: '{s}'. Try 'YYYY-MM-DD', 'today', 'tomorrow', "
        f"a weekday name like 'saturday', or '+3'."
    )


def format_day(d) -> str:
    """'Sat Sep 26' — for user-facing messages about date tabs."""
    return d.strftime("%a %b %d").replace(" 0", " ")


def date_tab_visible_at(date_str: str) -> datetime:
    """Midnight Pacific on the day the given date's tab first appears on the site."""
    d = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=VISIBLE_DATE_TABS - 1)
    return d.replace(tzinfo=LA_TZ)


def check_not_past(date_str: str) -> str:
    """Validator: reject dates before today (Pacific)."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    today = datetime.now(LA_TZ).date()
    if d < today:
        raise ValueError(f"{format_day(d)} is in the past (today is {format_day(today)}).")
    return date_str


def check_tab_visible(date_str: str) -> str:
    """Validator: reject dates whose tab isn't on the facility page right now
    (anything outside today .. today + VISIBLE_DATE_TABS - 1)."""
    check_not_past(date_str)
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    today = datetime.now(LA_TZ).date()
    last = today + timedelta(days=VISIBLE_DATE_TABS - 1)
    if d > last:
        appears = date_tab_visible_at(date_str)
        raise ValueError(
            f"{format_day(d)} isn't on the site yet — only {format_day(today)} through "
            f"{format_day(last)} are shown. Its tab appears {format_day(appears)} at 12:00 AM."
        )
    return date_str


def prompt_until_valid(prompt: str, validator, default: str | None = None) -> str:
    """
    Repeatedly prompt until `validator(raw)` returns a value without raising.
    Returns the validator's return value (normalized form).
    """
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            raw = default
        try:
            return validator(raw)
        except ValueError as e:
            print(f"  {e}")


def resolve_sport(sport: str | None) -> str:
    if sport:
        try:
            return parse_sport(sport)
        except ValueError as e:
            print(f"  {e}")
    return prompt_until_valid("Sport (t/tennis, pb/pickleball)", parse_sport)


def resolve_date(date_str: str | None, check=None) -> str:
    """Parse/prompt for a date. `check` (e.g. check_tab_visible) is an extra
    validator run on the parsed 'YYYY-MM-DD'; failing it re-prompts."""
    def validate(raw: str) -> str:
        parsed = parse_date_shortcut(raw)
        return check(parsed) if check else parsed

    if date_str:
        try:
            return validate(date_str)
        except ValueError as e:
            print(f"  {e}")
    return prompt_until_valid(
        "Date (YYYY-MM-DD, today, tmr, sat, +3)",
        validate,
    )


def resolve_time(time_str: str | None) -> str:
    if time_str:
        try:
            return parse_time_shortcut(time_str)
        except ValueError as e:
            print(f"  {e}")
    return prompt_until_valid(
        "Time (e.g. 10, 2, 10:30, 2PM)",
        parse_time_shortcut,
    )


VALID_COURTS = [2, 3, 4, 5, 6]
COURT_TAB_PREFIX = "SCRC - "


def resolve_court(court_str: str | None, sport: str) -> str | None:
    """Return a court tab label like 'SCRC - 3', or None for any court.
    Only meaningful for tennis; returns None for pickleball."""
    if sport != "tennis":
        return None
    if court_str is None:
        def _v(raw: str) -> str | None:
            raw = raw.strip().lower()
            if raw in ("", "any"):
                return ""
            try:
                n = int(raw)
            except ValueError:
                raise ValueError(f"Must be a court number ({VALID_COURTS[0]}-{VALID_COURTS[-1]}) or 'any'.")
            if n not in VALID_COURTS:
                raise ValueError(f"Must be {VALID_COURTS[0]}-{VALID_COURTS[-1]} or 'any'.")
            return f"{COURT_TAB_PREFIX}{n}"
        result = prompt_until_valid(
            f"Court ({VALID_COURTS[0]}-{VALID_COURTS[-1]} or 'any')",
            _v,
            default="any",
        )
        return result or None
    court_str = court_str.strip().lower()
    if court_str == "any":
        return None
    try:
        n = int(court_str)
    except ValueError:
        print(f"Invalid court '{court_str}'. Must be {VALID_COURTS[0]}-{VALID_COURTS[-1]} or 'any'.")
        return resolve_court(None, sport)
    if n not in VALID_COURTS:
        print(f"Invalid court '{court_str}'. Must be {VALID_COURTS[0]}-{VALID_COURTS[-1]}.")
        return resolve_court(None, sport)
    return f"{COURT_TAB_PREFIX}{n}"


def resolve_booking_args(
    sport: str | None, date_str: str | None, time_str: str | None, court_str: str | None = None
):
    """Normalize provided args and interactively prompt for any missing ones."""
    sport = resolve_sport(sport)
    # Future dates are fine for book — it waits for the tab to appear.
    date_str = resolve_date(date_str, check=check_not_past)
    return sport, date_str, resolve_time(time_str), resolve_court(court_str, sport)


# ── Login ─────────────────────────────────────────────────────────────────────

# True once the page is back on a recreation.ucla.edu page with the Sign In
# button gone. After the SSO update, login lands on the home page
# (secure.recreation.ucla.edu) rather than /booking, so '/booking' isn't required.
LOGGED_IN_JS = """() => {
    const url = window.location.href;
    if (!url.includes('recreation.ucla.edu')) return false;
    if (url.includes('idp.ucla.edu') || url.includes('shibboleth')) return false;
    const signInVisible = Array.from(document.querySelectorAll('*')).some(
        el => el.children.length === 0 &&
              (el.textContent || '').trim() === 'Sign In' &&
              el.offsetParent !== null
    );
    return !signInVisible;
}"""

# Duo prompt buttons worth clicking while waiting for approval.
DUO_HELPER_BUTTONS = (
    "Yes, trust browser", "Yes, this is my device", "Send me a Push", "Try again",
)


class LoginError(RuntimeError):
    pass


_credentials: tuple[str, str] | None = None  # cached so a re-login mid-wait doesn't re-prompt


def _read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key] = val
    return values


def load_credentials() -> tuple[str, str]:
    """UCLA credentials from env vars, then .env, then an interactive prompt."""
    global _credentials
    if _credentials:
        return _credentials
    file_vals = _read_env_file()
    user = os.environ.get("UCLA_USERNAME") or file_vals.get("UCLA_USERNAME", "")
    pw = os.environ.get("UCLA_PASSWORD") or file_vals.get("UCLA_PASSWORD", "")
    if not (user and pw) and sys.stdin.isatty():
        print("UCLA_USERNAME / UCLA_PASSWORD not set (env or .env) — enter them now "
              "(not saved).")
        try:
            user = user or input("UCLA Logon ID: ").strip()
            pw = pw or getpass.getpass("UCLA password: ")
        except EOFError:
            user = pw = ""
    if not (user and pw):
        raise LoginError(f"Set UCLA_USERNAME and UCLA_PASSWORD in the environment or {ENV_FILE}.")
    _credentials = (user, pw)
    return _credentials


async def _try_click(locator, timeout: int = 400) -> bool:
    try:
        if await locator.is_visible():
            await locator.click(timeout=timeout)
            return True
    except Exception:
        pass
    return False


async def auto_login(p, headless: bool = True) -> dict:
    """Log in unattended: types UCLA credentials, user approves the Duo push.

    Seeds the browser from the previous SESSION_FILE, which carries the Duo
    cookies and the UCLA IdP session cookie. If the IdP session is still alive
    server-side, this completes with no password or Duo push at all. Saves
    SESSION_FILE and returns the storage state. Raises LoginError on failure.
    """
    print(f"\nLogging in to UCLA ({'headless' if headless else 'headed'})...")
    browser = await p.chromium.launch(headless=headless)
    context = await browser.new_context(
        storage_state=str(SESSION_FILE) if SESSION_FILE.exists() else None,
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()
    try:
        await page.goto(BOOKING_URL, wait_until="domcontentloaded")

        submitted_at: float | None = None
        last_sign_in_click = 0.0
        deadline = time.monotonic() + LOGIN_TIMEOUT_SECS
        while True:
            if time.monotonic() > deadline:
                if submitted_at is not None:
                    raise LoginError("Timed out waiting for Duo approval.")
                raise LoginError(f"Timed out before reaching the UCLA login form (url={page.url}).")

            host = (urlparse(page.url).hostname or "").lower()
            if host.endswith("recreation.ucla.edu") and not is_session_expired(page.url):
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except PlaywrightTimeout:
                    pass
                try:
                    if await page.evaluate(LOGGED_IN_JS):
                        break
                except Exception:
                    pass  # navigated mid-evaluate; re-check next loop
                # Logged out on the rec site: open the SSO flow.
                if not await _try_click(page.get_by_text("UCLA Logon", exact=False).first):
                    if time.monotonic() - last_sign_in_click > 5:
                        if await _try_click(page.get_by_text("Sign In", exact=True).first):
                            last_sign_in_click = time.monotonic()
            else:
                pw_box = page.locator('input[type="password"]').first
                pw_visible = False
                try:
                    pw_visible = await pw_box.is_visible()
                except Exception:
                    pass
                if pw_visible and submitted_at is None:
                    user, pw = load_credentials()
                    await page.locator('input[type="text"], input[type="email"]').first.fill(user)
                    await pw_box.fill(pw)
                    if not await _try_click(
                        page.get_by_role("button", name=re.compile("sign in", re.I)).first, 5000
                    ) and not await _try_click(
                        page.locator('input[type="submit"], button[type="submit"]').first, 5000
                    ):
                        await pw_box.press("Enter")
                    submitted_at = time.monotonic()
                    print(">>> Credentials submitted. Approve the Duo push on your phone. <<<")
                elif pw_visible and time.monotonic() - submitted_at > 10:
                    # Still on the login form well after submitting.
                    raise LoginError("UCLA login form is still showing — username/password rejected?")
                for label in DUO_HELPER_BUTTONS:
                    await _try_click(page.get_by_text(label, exact=False).first)

            await page.wait_for_timeout(1000)

        if submitted_at is None:
            print("Signed in via saved SSO session (no Duo needed).")
        # Land on the booking page so the saved session has the right state.
        try:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle")
        except Exception:
            pass
        state = await context.storage_state(path=str(SESSION_FILE))
        print(f"Logged in. Session saved to {SESSION_FILE}")
        return state
    finally:
        try:
            await browser.close()
        except Exception:
            pass


async def open_session(p, headless: bool):
    """Launch a browser on BOOKING_URL with a valid session, auto-logging in if
    the saved session is missing or expired. Returns (browser, context, page);
    exits on login failure."""
    browser = await p.chromium.launch(headless=headless)
    if SESSION_FILE.exists():
        context = await browser.new_context(storage_state=str(SESSION_FILE))
        page = await context.new_page()
        await page.goto(BOOKING_URL)
        await page.wait_for_load_state("networkidle")
        if await check_logged_in(page):
            return browser, context, page
        await context.close()
        print("Saved session has expired.")
    else:
        print("No saved session.")

    try:
        await auto_login(p, headless=headless)
    except LoginError as e:
        await browser.close()
        print(f"\nError: Login failed: {e}")
        print("Retry, or run 'uv run bookingbot.py login --manual' to log in by hand.")
        sys.exit(1)

    context = await browser.new_context(storage_state=str(SESSION_FILE))
    page = await context.new_page()
    await page.goto(BOOKING_URL)
    await page.wait_for_load_state("networkidle")
    if not await check_logged_in(page):
        await browser.close()
        print("\nError: Logged in, but the booking site still shows you signed out.")
        sys.exit(1)
    return browser, context, page


async def relogin_context(p, context, headless: bool) -> None:
    """Auto-login and load the fresh cookies into an already-open context."""
    state = await auto_login(p, headless=headless)
    await context.clear_cookies()
    await context.add_cookies(state["cookies"])


async def cmd_login(manual: bool, headless: bool):
    if not manual:
        async with async_playwright() as p:
            try:
                await auto_login(p, headless=headless)
            except LoginError as e:
                print(f"\nError: Login failed: {e}")
                print("Retry, or run 'uv run bookingbot.py login --manual' to log in by hand.")
                sys.exit(1)
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(BOOKING_URL)

        print("\n=== UCLA Rec Login ===")
        print("A browser window has opened. Please complete these steps in the browser:")
        print("  1. Click 'Sign In'")
        print("  2. Click 'Click here for UCLA Logon'")
        print("  3. Enter your username and password on the UCLA SSO page")
        print("  4. Approve the Duo authentication prompt")
        print()
        print("Waiting for login to complete (timeout: 5 minutes)...")

        try:
            await page.wait_for_function(LOGGED_IN_JS, timeout=300_000)  # 5 minutes
        except PlaywrightTimeout:
            print("Error: Timed out waiting for login. Please try again.")
            await browser.close()
            sys.exit(1)

        # Navigate to the booking page so the saved session has the right
        # cookies/state for subsequent commands.
        try:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
        except Exception:
            pass
        await page.wait_for_load_state("networkidle")

        await context.storage_state(path=str(SESSION_FILE))
        print(f"\nSession saved to {SESSION_FILE}")
        print("Run 'uv run bookingbot.py book --help' to see booking options.")
        await browser.close()


# ── Book: navigation helpers ──────────────────────────────────────────────────

async def navigate_to_facility(page, sport: str):
    """Navigate from the booking home to the facility page for the given sport."""
    await page.goto(BOOKING_URL)
    await page.wait_for_load_state("networkidle")

    if not await check_logged_in(page):
        raise RuntimeError("Session expired — re-run the command to log in again")

    card_name = FACILITY_CARD_NAMES[sport]
    initial_url = page.url

    # Try structure-aware selectors first (the card's <a>/<button>), falling
    # back to text. A plain text match can land on a <span> that isn't actually
    # the clickable element, so the click "succeeds" without navigating.
    name_re = re.compile(re.escape(card_name), re.IGNORECASE)
    candidates = [
        page.get_by_role("link", name=name_re),
        page.get_by_role("button", name=name_re),
        page.locator(f'a:has-text("{card_name}")'),
        page.locator(f'button:has-text("{card_name}")'),
        page.get_by_text(card_name, exact=False),
    ]

    last_err: Exception | None = None
    clicked = False
    for loc in candidates:
        try:
            el = loc.first
            if await el.count() == 0:
                continue
            await el.scroll_into_view_if_needed(timeout=3000)
            await el.click(timeout=5000)
            clicked = True
            break
        except Exception as exc:
            last_err = exc
            continue

    if not clicked:
        raise RuntimeError(
            f"Could not click facility card '{card_name}'. Last error: {last_err}"
        )

    # Confirm navigation actually happened — either the URL changed, or a
    # date-tab-shaped element appeared. A silent no-op click here is the
    # root cause of a 'Could not find date tab' error later.
    try:
        await page.wait_for_function(
            """(args) => {
                if (window.location.href !== args.initialUrl) return true;
                // Look for any element whose text looks like 'MON APR 14' or 'APR 14'
                const els = document.querySelectorAll('*');
                for (const el of els) {
                    if (el.children.length > 0) continue;
                    const t = (el.textContent || '').trim();
                    if (/^[A-Z]{3}\\s+[A-Z]{3}\\s+\\d+$/.test(t)) return true;
                    if (/^[A-Z]{3}\\s+\\d+$/.test(t)) return true;
                }
                return false;
            }""",
            arg={"initialUrl": initial_url},
            timeout=6000,
        )
    except PlaywrightTimeout:
        shot = SCREENSHOTS_DIR / f"facility_nav_failed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        try:
            await page.screenshot(path=shot)
        except Exception:
            pass
        raise RuntimeError(
            f"Clicked facility card '{card_name}' but page did not navigate.\n"
            f"URL still: {page.url}\nScreenshot: {shot}"
        )

    await page.wait_for_load_state("networkidle")


async def click_date_tab(
    page, date: datetime, timeout: int = 8000, wait_networkidle: bool = True
):
    """Click the date tab corresponding to the given date.

    Fast path: find `button[data-year=Y][data-month=M][data-day=D]` in a single
    page.evaluate and call .click() in JS directly. This bypasses Playwright's
    actionability auto-waiting (which was costing ~3 s on the fire path).

    Slow path (fallback): Playwright click by text. Rarely needed.

    Set `wait_networkidle=False` on the critical firing path — the caller's
    tight poll for BOOK NOW is the right readiness signal, not networkidle.
    """
    year = date.year
    month = date.month  # 1-indexed, matches the site's data-month
    day = date.day

    # ── Fast path: JS-direct click by data attributes ────────────────────────
    # Only used on the critical snipe path (wait_networkidle=False) where the
    # ~3 s Playwright actionability wait is too expensive. For list/inspect
    # (wait_networkidle=True), skip straight to the reliable slow path —
    # the site's framework sometimes ignores bare .click() events.
    if not wait_networkidle:
        clicked = await page.evaluate(
            """
            ({year, month, day}) => {
                const sel = `button[data-year="${year}"][data-month="${month}"][data-day="${day}"]`;
                const candidates = Array.from(document.querySelectorAll(sel));
                const visible = candidates.find(el => {
                    const s = el.getAttribute('style') || '';
                    if (s.includes('display:none') || s.includes('display: none')) return false;
                    const cs = window.getComputedStyle(el);
                    return cs.display !== 'none' && cs.visibility !== 'hidden';
                });
                const target = visible || candidates[0];
                if (!target) return false;
                target.click();
                return true;
            }
            """,
            {"year": year, "month": month, "day": day},
        )
        if clicked:
            return

    # ── Slow path: Playwright-native click (dispatches full mouse events) ────
    # Try data-attribute selector first (most reliable), then text fallbacks.
    sel = f'button[data-year="{year}"][data-month="{month}"][data-day="{day}"]'
    try:
        btn = page.locator(sel).first
        if await btn.count() > 0:
            await btn.scroll_into_view_if_needed(timeout=3000)
            await btn.click(timeout=timeout)
            if wait_networkidle:
                await page.wait_for_load_state("networkidle")
            return
    except PlaywrightTimeout:
        pass

    long_label = format_date_tab(date)        # "TUE APR 14"
    short_label = format_date_tab_short(date)  # "APR 14"

    last_exc = None
    for label, exact in [(long_label, True), (short_label, False)]:
        try:
            tab = page.get_by_text(label, exact=exact).first
            await tab.scroll_into_view_if_needed(timeout=3000)
            await tab.click(timeout=timeout)
            if wait_networkidle:
                await page.wait_for_load_state("networkidle")
            return
        except PlaywrightTimeout as exc:
            last_exc = exc

    raise PlaywrightTimeout(
        f"Could not find date tab for {year}-{month:02d}-{day:02d} "
        f"(tried data attrs and text '{long_label}'/'{short_label}')"
    ) from last_exc


async def find_book_button(page, user_time: str):
    """
    Return a Playwright locator for the Book Now button of the time slot
    matching `user_time`, or None if not found.

    Each slot is a `div.card[data-slot-number]` whose Book Now button has
    `data-apt-id` and a `data-slot-text` attribute like "2:00 - 2:50 PM".
    We match on the button attribute directly so "1:00" doesn't accidentally
    match "11:00".
    """
    t = datetime.strptime(user_time.strip().upper(), "%I:%M %p")
    hour_12 = t.hour % 12 or 12
    minute = t.minute
    ampm = "AM" if t.hour < 12 else "PM"
    prefix = f"{hour_12}:{minute:02d}"

    marked = await page.evaluate(
        """
        ({prefix, ampm}) => {
            document.querySelectorAll('[data-book-target="1"]').forEach(
                el => el.removeAttribute('data-book-target')
            );
            const buttons = document.querySelectorAll('button[data-apt-id][data-slot-text]');
            for (const btn of buttons) {
                const dst = (btn.getAttribute('data-slot-text') || '').trim();
                if (!dst.startsWith(prefix + ' -')) continue;
                if (!dst.toUpperCase().endsWith(ampm)) continue;
                btn.setAttribute('data-book-target', '1');
                return true;
            }
            return false;
        }
        """,
        {"prefix": prefix, "ampm": ampm},
    )

    if not marked:
        return None
    return page.locator('[data-book-target="1"]').first


async def js_click_book_button(page, user_time: str) -> bool:
    """
    Click the Book Now button in JS directly — zero Playwright auto-waiting.
    Returns True if a matching button was found and clicked.
    """
    t = datetime.strptime(user_time.strip().upper(), "%I:%M %p")
    hour_12 = t.hour % 12 or 12
    minute = t.minute
    ampm = "AM" if t.hour < 12 else "PM"
    prefix = f"{hour_12}:{minute:02d}"

    return await page.evaluate(
        """
        ({prefix, ampm}) => {
            const buttons = document.querySelectorAll('button[data-apt-id][data-slot-text]');
            for (const btn of buttons) {
                const dst = (btn.getAttribute('data-slot-text') || '').trim();
                if (!dst.startsWith(prefix + ' -')) continue;
                if (!dst.toUpperCase().endsWith(ampm)) continue;
                btn.click();
                return true;
            }
            return false;
        }
        """,
        {"prefix": prefix, "ampm": ampm},
    )


async def poll_for_book_button(page, user_time: str, timeout_ms: int = 2500):
    """
    Tight-poll `find_book_button` until it returns a locator or the timeout
    elapses. Much faster than wait_for_slots() + find_book_button() in
    sequence: we catch the button within ~25 ms of it appearing and do no
    redundant waits.
    """
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        btn = await find_book_button(page, user_time)
        if btn:
            return btn
        await asyncio.sleep(0.025)
    return None


async def poll_and_js_click_book(page, user_time: str, timeout_ms: int = 2500) -> bool:
    """
    Tight-poll the page and, the moment the target BOOK NOW button exists,
    click it via JS in the same round trip. This is the fastest path: no
    Playwright click auto-waiting at all.
    """
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if await js_click_book_button(page, user_time):
            return True
        await asyncio.sleep(0.02)
    return False


async def wait_for_slots(page, timeout: int = 8000):
    """
    Wait until booking slot content is visible on the page. Slots render via
    an async fetch after the date/court tab click, so we must wait before
    searching for buttons.

    First waits for the loading spinner (#loadingspinner) to disappear, then
    confirms that at least one slot-related keyword is visible.
    """
    try:
        await page.locator("#loadingspinner").wait_for(
            state="hidden", timeout=timeout,
        )
    except PlaywrightTimeout:
        pass
    try:
        await page.locator("text=BOOK NOW").or_(
            page.locator("text=No spots available")
        ).or_(
            page.locator("text=Unavailable")
        ).first.wait_for(state="visible", timeout=timeout)
    except PlaywrightTimeout:
        pass


async def get_court_tabs(page) -> list[str]:
    """
    Return the text labels of any court sub-tabs visible on the page
    (e.g. ['SCRC - 2', 'SCRC - 3', 'SCRC - 4']).
    Date tabs (matching 'MON APR 14' pattern) are excluded.
    Returns an empty list when no court tabs exist (e.g. pickleball).
    """
    try:
        tabs = page.locator("[role=tab]")
        count = await tabs.count()
        court_tabs = []
        for i in range(count):
            text = (await tabs.nth(i).inner_text()).strip().replace("\n", " ")
            # Skip date-format tabs like "TUE APR 14"
            if re.match(r'[A-Z]{3}\s+[A-Z]{3}\s+\d+', text):
                continue
            if text:
                court_tabs.append(text)
        return court_tabs
    except Exception:
        return []


# ── Book: keep-alive loop ─────────────────────────────────────────────────────

async def keep_alive(
    p, page, until: datetime, headless: bool, lead: float = 35.0, label: str = "Opens"
):
    """
    Reload the booking home page every ~2 minutes to prevent session timeout.
    If the session dies anyway, auto-login again (Duo push) and keep waiting.
    Stops `lead` seconds before `until` (default 35 s, so the caller can pre-position).
    `label` names the event being waited for in the progress lines.
    """
    while True:
        now = datetime.now(LA_TZ)
        remaining = (until - now).total_seconds()

        if remaining <= lead:
            break

        sleep_secs = min(120.0, remaining - lead)
        h = int(remaining // 3600)
        m = int((remaining % 3600) // 60)
        print(f"  [{now.strftime('%H:%M:%S')}] {label} in {h}h {m}m. "
              f"Next keep-alive in {int(sleep_secs)}s...")
        await asyncio.sleep(sleep_secs)

        await page.goto(BOOKING_URL)
        await page.wait_for_load_state("networkidle")

        if not await check_logged_in(page):
            print("  Session expired during wait — logging in again.")
            try:
                await relogin_context(p, page.context, headless)
            except LoginError as e:
                raise RuntimeError(f"Session expired during wait and re-login failed: {e}") from e
            await page.goto(BOOKING_URL)
            await page.wait_for_load_state("networkidle")
            if not await check_logged_in(page):
                raise RuntimeError("Re-login succeeded but the booking site still shows you signed out.")


async def verify_date_tab(page, sport: str, target_date: datetime):
    """Open the facility page and click the target date tab, retrying for up
    to TAB_VERIFY_TIMEOUT_SECS in case the site's midnight rollover lags.
    Raises PlaywrightTimeout if the tab never shows up."""
    deadline = time.monotonic() + TAB_VERIFY_TIMEOUT_SECS
    while True:
        await navigate_to_facility(page, sport)
        try:
            await click_date_tab(page, target_date)
            return
        except PlaywrightTimeout:
            if time.monotonic() >= deadline:
                raise
            print("  Tab not there yet — retrying in 30s...")
            await asyncio.sleep(30)


# ── Book command ──────────────────────────────────────────────────────────────

async def cmd_book(
    sport: str | None,
    date_str: str | None,
    time_str: str | None,
    court_str: str | None,
    headless: bool,
    dry_run: bool,
):
    SCREENSHOTS_DIR.mkdir(exist_ok=True)

    sport, date_str, time_str, court_pref = resolve_booking_args(sport, date_str, time_str, court_str)

    # Parse target datetime (LA time)
    target_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=LA_TZ)
    slot_time = datetime.strptime(time_str.upper(), "%I:%M %p")
    target_dt = target_date.replace(
        hour=slot_time.hour, minute=slot_time.minute, second=0, microsecond=0
    )
    open_time = target_dt - timedelta(hours=BOOKING_ADVANCE_HOURS)
    tab_time = date_tab_visible_at(date_str)
    now = datetime.now(LA_TZ)

    if target_dt <= now:
        print(f"Error: {target_dt.strftime('%a %Y-%m-%d %I:%M %p')} has already started.")
        sys.exit(1)

    label = "[DRY RUN] " if dry_run else ""
    print(f"\n=== UCLA Rec Booking Sniper {label}===")
    print(f"Sport:         {sport}")
    if court_pref:
        print(f"Court:         {court_pref}")
    print(f"Target slot:   {target_dt.strftime('%a %Y-%m-%d %I:%M %p %Z')}")
    print(f"Date tab:      {tab_time.strftime('%a %Y-%m-%d %I:%M %p %Z')}")
    print(f"Booking opens: {open_time.strftime('%a %Y-%m-%d %I:%M %p %Z')}")
    print(f"Current time:  {now.strftime('%a %Y-%m-%d %I:%M %p %Z')}")

    day_name = target_date.strftime("%A")
    tab_pending = tab_time > now
    if open_time < now:
        print("\nNote: Booking window is already open — attempting to book immediately.")
    elif tab_pending:
        print(f"\n{day_name} tab not available yet — waiting until "
              f"{tab_time.strftime('%a %I:%M %p')} to load it. Keeping session alive until then.")
    else:
        secs = (open_time - now).total_seconds()
        print(f"\nBooking opens in {int(secs // 3600)}h {int((secs % 3600) // 60)}m. "
              "Keeping session alive until then.")

    async with async_playwright() as p:
        browser, context, page = await open_session(p, headless)
        print("\nSession valid.")

        if tab_pending:
            # Wait for the date tab to appear, then confirm it's really there
            # so a problem surfaces now rather than at open time.
            await keep_alive(
                p, page, tab_time + timedelta(seconds=TAB_APPEAR_GRACE_SECS), headless,
                lead=0, label=f"{day_name} tab appears",
            )
            print(f"\n{day_name} tab should be available now — checking...")
            try:
                await verify_date_tab(page, sport, target_date)
            except PlaywrightTimeout as exc:
                shot = SCREENSHOTS_DIR / f"tab_missing_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                await page.screenshot(path=shot)
                print(f"\nError: {day_name} tab never appeared: {exc}")
                print(f"Screenshot saved: {shot}")
                await browser.close()
                sys.exit(1)
            secs = (open_time - datetime.now(LA_TZ)).total_seconds()
            print(f"{day_name} tab found. Booking opens in "
                  f"{int(secs // 3600)}h {int((secs % 3600) // 60)}m — keeping session alive.")

        # Keep session alive until 35 s before the booking window opens
        await keep_alive(p, page, open_time, headless)

        # ── Navigate to facility ──────────────────────────────────────────────
        print("\nNavigating to facility page...")
        await navigate_to_facility(page, sport)

        already_open = open_time <= datetime.now(LA_TZ)
        pre_date = target_date - timedelta(days=1)

        if already_open:
            # Booking window is open now — go straight to the target date tab.
            print("Booking window already open. Navigating directly to target date...")
        else:
            # ── Pre-position ──────────────────────────────────────────────────
            # Sit on the day-before tab so the target date tab is not yet loaded.
            try:
                print(f"Pre-positioning on {format_date_tab(pre_date)} tab...")
                await click_date_tab(page, pre_date)
            except PlaywrightTimeout:
                print("Adjacent date tab not available; will navigate directly at open time.")

            # Wait until T-1 s
            now = datetime.now(LA_TZ)
            remaining = (open_time - now).total_seconds()
            if remaining > 1.0:
                print(f"Holding for {remaining:.1f}s...")
                await asyncio.sleep(remaining - 1.0)

            # ── Sniper ────────────────────────────────────────────────────────
            # Tight loop until open_time, then click the target date tab.
            while datetime.now(LA_TZ) < open_time:
                await asyncio.sleep(0.02)

        fire_ts = datetime.now(LA_TZ).strftime("%H:%M:%S.%f")
        print(f"\n[{fire_ts}] Firing — clicking {format_date_tab(target_date)} tab...")
        t_fire = time.monotonic()
        try:
            # Skip networkidle on the critical click — we poll for BOOK NOW directly.
            await click_date_tab(page, target_date, wait_networkidle=False)
        except PlaywrightTimeout:
            shot = SCREENSHOTS_DIR / f"fire_failed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            try:
                await page.screenshot(path=shot)
                print(f"Diagnostic screenshot: {shot}  (url: {page.url})")
            except Exception:
                pass
            raise
        t_clicked = time.monotonic()
        print(f"  [+{(t_clicked - t_fire)*1000:6.0f} ms] date tab clicked")

        if dry_run:
            await wait_for_slots(page)
            shot = SCREENSHOTS_DIR / f"dryrun_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            await page.screenshot(path=shot)
            print(f"[DRY RUN] Would click BOOK NOW for {time_str}.")
            print(f"Screenshot saved: {shot}")
            if not headless:
                await asyncio.sleep(10)
            await browser.close()
            return

        # ── Click BOOK NOW — tight-poll, cycle court tabs, retry ─────────────
        max_retries = 3
        booked = False

        async def try_book_on_current_view(attempt_label: str) -> bool:
            """Try the current view plus court sub-tabs. Return True if booked.

            If court_pref is set (e.g. 'SCRC - 3'), only try that court.
            Otherwise try the current view first, then cycle all courts.
            """
            if court_pref:
                # Click the preferred court tab first, then poll for BOOK NOW.
                ct_clicked = await page.evaluate(
                    """(label) => {
                        const tabs = document.querySelectorAll('[role=tab]');
                        for (const t of tabs) {
                            if ((t.innerText || '').trim() === label) {
                                t.click();
                                return true;
                            }
                        }
                        return false;
                    }""",
                    court_pref,
                )
                if ct_clicked:
                    t_poll_start = time.monotonic()
                    ok = await poll_and_js_click_book(page, time_str, timeout_ms=2500)
                    t_poll_end = time.monotonic()
                    print(f"  [+{(t_poll_end - t_fire)*1000:6.0f} ms] "
                          f"poll {court_pref} "
                          f"({'clicked' if ok else 'not found'} in {(t_poll_end - t_poll_start)*1000:.0f} ms)")
                    if ok:
                        ts = datetime.now(LA_TZ).strftime("%H:%M:%S.%f")
                        print(f"[{ts}] Clicked BOOK NOW on {court_pref} ({attempt_label}) via JS")
                        await page.wait_for_load_state("networkidle")
                        return True
                else:
                    print(f"  Warning: could not click court tab '{court_pref}'")
                return False

            # No court preference — try current view, then cycle all courts.
            t_poll_start = time.monotonic()
            ok = await poll_and_js_click_book(page, time_str, timeout_ms=6000)
            t_poll_end = time.monotonic()
            print(f"  [+{(t_poll_end - t_fire)*1000:6.0f} ms] "
                  f"poll_and_js_click_book "
                  f"({'clicked' if ok else 'not found'} in {(t_poll_end - t_poll_start)*1000:.0f} ms)")
            if ok:
                ts = datetime.now(LA_TZ).strftime("%H:%M:%S.%f")
                print(f"[{ts}] Clicked BOOK NOW ({attempt_label}) via JS")
                await page.wait_for_load_state("networkidle")
                return True

            court_tabs = await get_court_tabs(page)
            for ct in court_tabs:
                print(f"  Trying court tab: {ct}")
                # JS-click the court tab too, for consistency + speed
                ct_clicked = await page.evaluate(
                    """(label) => {
                        const tabs = document.querySelectorAll('[role=tab]');
                        for (const t of tabs) {
                            if ((t.innerText || '').trim() === label) {
                                t.click();
                                return true;
                            }
                        }
                        return false;
                    }""",
                    ct,
                )
                if not ct_clicked:
                    continue
                if await poll_and_js_click_book(page, time_str, timeout_ms=800):
                    ts = datetime.now(LA_TZ).strftime("%H:%M:%S.%f")
                    print(f"[{ts}] Clicked BOOK NOW on {ct} ({attempt_label}) via JS")
                    await page.wait_for_load_state("networkidle")
                    return True
            return False

        for attempt in range(1, max_retries + 1):
            booked = await try_book_on_current_view(f"attempt {attempt}")
            if booked:
                break

            print(f"Attempt {attempt}: BOOK NOW not found for '{time_str}'. Re-navigating...")
            # Re-navigate cleanly to the facility page to avoid stale/wrong page state
            await navigate_to_facility(page, sport)
            await click_date_tab(page, target_date, wait_networkidle=False)

        if not booked:
            shot = SCREENSHOTS_DIR / f"failed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            await page.screenshot(path=shot)
            print(f"\nFailed to find BOOK NOW after {max_retries} attempts.")
            print(f"Screenshot saved: {shot}")
            await browser.close()
            sys.exit(1)

        # ── Confirmation ──────────────────────────────────────────────────────
        # Navigate back to the bookings home so the screenshot shows the
        # Upcoming list with the new reservation, like the cancel confirmation.
        try:
            await page.goto(BOOKING_URL)
            await page.wait_for_load_state("networkidle")
            # Scroll down so the Upcoming section is in frame
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.4)
        except Exception:
            pass  # Fall through and screenshot whatever state we're in
        shot = SCREENSHOTS_DIR / f"booking_confirmation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        await page.screenshot(path=shot, full_page=True)
        print(f"\nBooking complete! Confirmation screenshot: {shot}")

        if not headless:
            print("Browser stays open for 30 s. Press Ctrl+C to close early.")
            await asyncio.sleep(30)

        await browser.close()


# ── Inspect command ──────────────────────────────────────────────────────────

async def cmd_inspect(sport: str | None, date_str: str | None, headless: bool = True):
    """Navigate to a facility page and dump its structure — helps find
    faster selectors for tabs, time slot rows, and BOOK NOW buttons."""
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    sport = resolve_sport(sport)

    # Only use a date if one was explicitly provided. Unlike `list`/`book`,
    # inspect does not prompt — omitting --date inspects the default (today's) tab.
    target_date = None
    if date_str:
        try:
            date_str = check_tab_visible(parse_date_shortcut(date_str))
            target_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=LA_TZ)
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)

    async with async_playwright() as p:
        browser, context, page = await open_session(p, headless)

        print(f"Navigating to {sport} facility page...")
        await navigate_to_facility(page, sport)

        if target_date is not None:
            try:
                await click_date_tab(page, target_date)
            except PlaywrightTimeout as exc:
                print(f"Warning: could not click date tab for {date_str}: {exc}")

        # Wait for slot cards to render before evaluating — otherwise the
        # query runs against a still-loading DOM and finds nothing.
        await wait_for_slots(page)

        print(f"\nURL: {page.url}\n")

        dump = await page.evaluate("""
            () => {
                const summarise = (el) => {
                    const attrs = {};
                    for (const a of el.attributes || []) attrs[a.name] = a.value;
                    return {
                        tag: el.tagName,
                        id: el.id || null,
                        cls: el.className || null,
                        role: el.getAttribute ? el.getAttribute('role') : null,
                        text: (el.innerText || '').trim().slice(0, 80),
                        attrs,
                    };
                };

                // All [role=tab] elements (date + court tabs)
                const tabs = Array.from(document.querySelectorAll('[role=tab]'))
                    .map(summarise);

                // All elements whose trimmed innerText is 'Book Now' (any case)
                const isBookNow = (el) =>
                    (el.innerText || '').trim().toUpperCase() === 'BOOK NOW';
                const bookButtons = [];
                for (const el of document.querySelectorAll('button, a, span, div')) {
                    if (isBookNow(el)) bookButtons.push(summarise(el));
                }

                // For the first Book Now button, walk up 8 ancestors to show
                // the row structure it lives inside
                let rowChain = [];
                const firstBook = Array.from(document.querySelectorAll('button, a, span, div'))
                    .find(isBookNow);
                if (firstBook) {
                    let n = firstBook;
                    for (let d = 0; d < 8; d++) {
                        n = n.parentElement;
                        if (!n) break;
                        rowChain.push({
                            depth: d + 1,
                            ...summarise(n),
                            text: (n.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 120),
                        });
                    }
                }

                // All unique "time-range-looking" text blocks (slot rows)
                const slotTexts = new Set();
                for (const el of document.querySelectorAll('li, tr, div')) {
                    const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                    if (t && t.length < 200 && /\\d{1,2}:\\d{2}.*[AP]M/.test(t) && t.includes('-')) {
                        slotTexts.add(t.slice(0, 120));
                    }
                }

                return {
                    tabs,
                    bookButtons,
                    rowChain,
                    slotTexts: Array.from(slotTexts).slice(0, 20),
                };
            }
        """)

        print(f"── {len(dump['tabs'])} [role=tab] elements ──")
        for t in dump["tabs"]:
            print(f"  <{t['tag'].lower()} class='{t['cls']}' id='{t['id']}'>"
                  f"  text={t['text']!r}")

        print(f"\n── {len(dump['bookButtons'])} 'Book Now' elements ──")
        for b in dump["bookButtons"][:10]:
            keys = [k for k in b["attrs"] if k not in ("class", "id")]
            extra = " ".join(f"{k}={b['attrs'][k]!r}" for k in keys[:4])
            print(f"  <{b['tag'].lower()} class='{b['cls']}' {extra}>")

        print("\n── ancestors of first Book Now ──")
        for n in dump["rowChain"]:
            print(f"  depth={n['depth']} <{n['tag'].lower()} "
                  f"class='{(n['cls'] or '')[:40]}' role={n['role']!r}>")
            print(f"    text={n['text']!r}")

        print(f"\n── {len(dump['slotTexts'])} slot-row text samples ──")
        for s in dump["slotTexts"][:10]:
            print(f"  {s!r}")

        shot = SCREENSHOTS_DIR / f"inspect_{sport}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        await page.screenshot(path=shot, full_page=True)
        html_path = SCREENSHOTS_DIR / f"inspect_{sport}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        html_path.write_text(await page.content())
        print(f"\nFull-page screenshot: {shot}")
        print(f"Full HTML dump:       {html_path}")

        await browser.close()


# ── List command ─────────────────────────────────────────────────────────────

async def extract_slots(page) -> list[dict]:
    """Return all slot entries on the current view as
    [{ 'time': str, 'bookable': bool, 'status': str, 'spot_count': int }, ...]
    in document order.

    `status` is derived from spot availability (not button state):
      - 'available'    — spot_count > 0 (even if BOOK NOW is absent due to
                         the one-booking-per-sport-per-day rule)
      - 'full'         — 0 spots available
      - 'not yet open' — booking window hasn't opened yet
    """
    await wait_for_slots(page)
    return await page.evaluate(
        """
        () => {
            const rows = document.querySelectorAll('div.card[data-slot-number]');
            const out = [];
            const seen = new Set();
            for (const row of rows) {
                const text = (row.innerText || '').trim().replace(/\\s+/g, ' ');
                if (!text) continue;
                const m = text.match(/\\d{1,2}(?::\\d{2})?\\s*-\\s*\\d{1,2}(?::\\d{2})?\\s*(?:AM|PM)/i);
                if (!m) continue;
                const time = m[0].replace(/\\s+/g, ' ');

                // Spot count — "N spot(s) available" vs "No spots available"
                let spotCount = 0;
                const countMatch = text.match(/(\\d+)\\s+spots?\\s+available/i);
                if (countMatch) spotCount = parseInt(countMatch[1], 10);

                const bookable = /book\\s*now/i.test(text);
                const isNotYetOpen = /opens at/i.test(text);

                let status;
                if (isNotYetOpen)    status = 'not yet open';
                else if (spotCount > 0) status = 'available';
                else                 status = 'full';

                const key = time + '|' + status + '|' + spotCount;
                if (seen.has(key)) continue;
                seen.add(key);
                out.push({ time, bookable, status, spot_count: spotCount });
            }
            return out;
        }
        """
    )


async def click_court_tab(page, label: str) -> bool:
    return await page.evaluate(
        """(label) => {
            const tabs = document.querySelectorAll('[role=tab]');
            for (const t of tabs) {
                if ((t.innerText || '').trim() === label) {
                    t.click();
                    return true;
                }
            }
            return false;
        }""",
        label,
    )


async def cmd_list(sport: str | None, date_str: str | None, headless: bool):
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    sport = resolve_sport(sport)
    date_str = resolve_date(date_str, check=check_tab_visible)
    target_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=LA_TZ)

    async with async_playwright() as p:
        browser, context, page = await open_session(p, headless)

        # Collect user's upcoming bookings from the home page so we can mark
        # slots the user has already booked in the listing below.
        all_user_bookings = await get_user_bookings_structured(page)
        user_bookings_here = [
            b for b in all_user_bookings
            if b["sport"] == sport and b["date"] == date_str
        ]

        print(f"\nNavigating to {sport} facility page...")
        await navigate_to_facility(page, sport)

        # The site defaults to today's tab, so skip the click for today.
        today = datetime.now(LA_TZ).date()
        if target_date.date() != today:
            try:
                await click_date_tab(page, target_date)
            except PlaywrightTimeout as exc:
                print(f"\nError: {exc}")
                await browser.close()
                sys.exit(1)

        header = f"=== {FACILITY_CARD_NAMES[sport]} — {target_date.strftime('%a, %b %d %Y').replace(' 0', ' ')} ==="
        print(f"\n{header}")

        court_tabs = await get_court_tabs(page)

        if not court_tabs:
            # Pickleball — single view
            slots = await extract_slots(page)
            _print_slots_pickleball(slots, user_bookings_here)
        else:
            # Tennis — collect all courts, then reorganize by time
            by_court: dict[str, list[dict]] = {}
            for ct in court_tabs:
                if not await click_court_tab(page, ct):
                    continue
                await asyncio.sleep(0.3)
                by_court[ct] = await extract_slots(page)
            _print_slots_tennis(by_court, user_bookings_here)

        await browser.close()


def _print_slots_pickleball(slots: list[dict], user_bookings: list[dict]) -> None:
    if not slots:
        print("  (no slots found)")
        return

    if user_bookings:
        print("  (You already have a booking for pickleball on this day"
              " — new bookings will be rejected.)\n")

    booked_starts = {b["start_minute"] for b in user_bookings}
    your_bookings, available, full, not_yet_open = [], [], [], []

    for s in slots:
        start = parse_slot_start_minute(s["time"])
        if start is not None and start in booked_starts:
            your_bookings.append(s)
        elif s["status"] == "available":
            available.append(s)
        elif s["status"] == "not yet open":
            not_yet_open.append(s)
        else:
            full.append(s)

    if your_bookings:
        print("  Your booking:")
        for s in your_bookings:
            print(f"    {s['time']}")

    if available:
        print("  Available:")
        for s in available:
            n = s["spot_count"]
            word = "court" if n == 1 else "courts"
            print(f"    {s['time']}  ({n} {word})")
    else:
        print("  Available: (none)")

    if full:
        print("  Full:")
        for s in full:
            print(f"    {s['time']}")

    if not_yet_open:
        print("  Not yet open:")
        for s in not_yet_open:
            print(f"    {s['time']}")


def _print_slots_tennis(
    by_court: dict[str, list[dict]], user_bookings: list[dict]
) -> None:
    """Print tennis slots grouped by time with categories:
    your booking, available (with court count + names), full, not yet open."""
    if not by_court:
        print("  (no courts found)")
        return

    if user_bookings:
        print("  (You already have a booking for tennis on this day"
              " — new bookings will be rejected.)\n")

    user_court_by_start: dict[int, str] = {}
    for b in user_bookings:
        if b.get("court"):
            user_court_by_start[b["start_minute"]] = b["court"]

    times: dict[str, dict] = {}
    for court, slots in by_court.items():
        for s in slots:
            label = s["time"]
            start = parse_slot_start_minute(label)
            entry = times.setdefault(label, {
                "start": start if start is not None else 9999,
                "available": [],
                "booked_by_user": [],
                "full": [],
                "not_yet_open": [],
            })
            if start is not None and user_court_by_start.get(start) == court:
                entry["booked_by_user"].append(court)
            elif s["status"] == "available":
                entry["available"].append(court)
            elif s["status"] == "not yet open":
                entry["not_yet_open"].append(court)
            else:
                entry["full"].append(court)

    sorted_items = sorted(times.items(), key=lambda kv: kv[1]["start"])

    your_list: list[tuple[str, list[str]]] = []
    avail_list: list[tuple[str, list[str]]] = []
    full_list: list[str] = []
    not_open_list: list[str] = []

    for label, info in sorted_items:
        if info["booked_by_user"]:
            your_list.append((label, info["booked_by_user"]))
        elif info["available"]:
            avail_list.append((label, info["available"]))
        elif info["not_yet_open"] and not info["full"]:
            not_open_list.append(label)
        else:
            full_list.append(label)

    if your_list:
        print("  Your booking:")
        for label, courts in your_list:
            print(f"    {label}  ({', '.join(courts)})")

    if avail_list:
        print("  Available:")
        for label, courts in avail_list:
            n = len(courts)
            word = "court" if n == 1 else "courts"
            print(f"    {label}  ({n} {word}: {', '.join(courts)})")
    else:
        print("  Available: (none)")

    if full_list:
        print("  Full:")
        for label in full_list:
            print(f"    {label}")

    if not_open_list:
        print("  Not yet open:")
        for label in not_open_list:
            print(f"    {label}")


# ── Cancel command ───────────────────────────────────────────────────────────

def parse_booking_label(raw: str) -> str:
    """
    Extract court name, date, and time from a raw booking card innerText.
    Input example:
      'SCRC Tennis Courts - SCRC - 2 more_vert Open booking actions dropdown
       person NICHOLAS CHEN today Tue, Apr 14 2026 schedule 12:00 - 12:50 PM'
    Output: 'SCRC Tennis Courts  |  Tue, Apr 14 2026  |  12:00 - 12:50 PM'
    """
    facility_m = re.match(r'^([\w\s]+Courts)', raw)
    facility = facility_m.group(1).strip() if facility_m else ""

    date_m = re.search(r'today\s+(\w{3},\s+\w{3}\s+\d{1,2}\s+\d{4})', raw)
    date_s = date_m.group(1) if date_m else ""

    time_m = re.search(r'(\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\s*(?:AM|PM))', raw)
    time_s = time_m.group(1).strip() if time_m else ""

    parts = [x for x in [facility, date_s, time_s] if x]
    return "  |  ".join(parts) if parts else raw[:80]


def parse_booking_info(raw: str) -> dict | None:
    """Parse raw booking card text into structured fields, or None on failure.

    Returns: {"sport", "court" (None for pickleball), "date" (YYYY-MM-DD), "start_minute"}.
    """
    if "Tennis Courts" in raw:
        sport = "tennis"
    elif "Pickleball Courts" in raw:
        sport = "pickleball"
    else:
        return None

    court = None
    if sport == "tennis":
        m = re.search(r"Tennis Courts\s*-\s*(SCRC\s*-\s*\d+)", raw)
        if m:
            court = m.group(1).strip()

    date_iso = None
    date_m = re.search(r"\w{3},\s+(\w{3})\s+(\d{1,2})\s+(\d{4})", raw)
    if date_m:
        try:
            dt = datetime.strptime(
                f"{date_m.group(1)} {date_m.group(2)} {date_m.group(3)}", "%b %d %Y"
            )
            date_iso = dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    start_minute = None
    time_label = None
    time_m = re.search(r"(\d{1,2}):(\d{2})\s*-\s*\d{1,2}:\d{2}\s*(AM|PM)", raw)
    if time_m:
        hour = int(time_m.group(1))
        minute = int(time_m.group(2))
        ampm = time_m.group(3)
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        start_minute = hour * 60 + minute
        time_label = time_m.group(0)

    if date_iso is None or start_minute is None:
        return None

    return {
        "sport": sport,
        "court": court,
        "date": date_iso,
        "start_minute": start_minute,
        "time_label": time_label,
    }


async def get_user_bookings_structured(page) -> list[dict]:
    """Return a list of the user's upcoming bookings as structured records.

    Must be called while `page` is on the booking home (BOOKING_URL).
    """
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(0.4)

    action_btns = page.locator("button").filter(
        has_text=re.compile(r"Open booking actions dropdown", re.IGNORECASE)
    )
    count = await action_btns.count()

    bookings: list[dict] = []
    seen: set[tuple] = set()
    for i in range(count):
        btn = action_btns.nth(i)
        raw = await btn.evaluate("""
            el => {
                let card = el;
                for (let depth = 0; depth < 10; depth++) {
                    card = card.parentElement;
                    if (!card) return '';
                    const text = (card.innerText || '').trim();
                    if (text.match(/\\d{4}/) && text.match(/\\d{1,2}:\\d{2}/)) {
                        return text.replace(/\\s+/g, ' ');
                    }
                }
                return '';
            }
        """)
        info = parse_booking_info(raw or "")
        if not info:
            continue
        key = (info["sport"], info["court"], info["date"], info["start_minute"])
        if key in seen:
            continue
        seen.add(key)
        bookings.append(info)

    return bookings


async def collect_upcoming_bookings(page):
    """
    Return a list of dicts, one per upcoming booking:
      {
        "label":          str,      # cleaned court / date / time summary
        "action_btn":     Locator,  # the '⋮' dropdown button on that card
        "participant_id": str|None, # data-booking-participant-id from the card
      }

    Anchors on the action-dropdown button (one per card) to avoid duplicates
    from parent/child elements matching the same text heuristic. Also extracts
    the participant-id from the card's (hidden) cancel-booking anchor so we can
    uniquely target that link later without relying on visible-first ordering.
    """
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(0.4)

    # Each booking card has a three-dots button whose inner text (from a hidden
    # accessibility span) reads "Open booking actions dropdown".
    action_btns = page.locator("button").filter(
        has_text=re.compile(r"Open booking actions dropdown", re.IGNORECASE)
    )
    count = await action_btns.count()

    bookings = []
    seen_labels: set[str] = set()
    for i in range(count):
        btn = action_btns.nth(i)
        info = await btn.evaluate("""
            el => {
                let card = el;
                for (let depth = 0; depth < 10; depth++) {
                    card = card.parentElement;
                    if (!card) return { text: '', participantId: null };
                    const text = (card.innerText || '').trim();
                    if (text.match(/\\d{4}/) && text.match(/\\d{1,2}:\\d{2}/)) {
                        const link = card.querySelector('a.cancel-booking-btn');
                        return {
                            text: text.replace(/\\s+/g, ' '),
                            participantId: link ? link.getAttribute('data-booking-participant-id') : null,
                        };
                    }
                }
                return { text: '', participantId: null };
            }
        """)
        label = parse_booking_label(info.get("text", ""))
        if label in seen_labels:
            continue  # skip duplicate card from nested DOM match
        seen_labels.add(label)
        bookings.append({
            "label": label,
            "action_btn": btn,
            "participant_id": info.get("participantId"),
        })

    return bookings


async def reveal_cancel_option(action_btn):
    """Click the action-dropdown button on a booking card."""
    await action_btn.scroll_into_view_if_needed(timeout=3000)
    await action_btn.click()


async def cmd_status(headless: bool = True):
    """Print the user's upcoming bookings."""
    async with async_playwright() as p:
        browser, context, page = await open_session(p, headless)

        bookings = await get_user_bookings_structured(page)
        await browser.close()

    if not bookings:
        print("\nNo upcoming bookings.")
        return

    # Sort by date, then start time
    bookings.sort(key=lambda b: (b["date"], b["start_minute"]))

    print(f"\n=== Your upcoming bookings ({len(bookings)}) ===")
    for b in bookings:
        facility = FACILITY_CARD_NAMES.get(b["sport"], b["sport"].title())
        court = f" ({b['court']})" if b.get("court") else ""
        date_obj = datetime.strptime(b["date"], "%Y-%m-%d")
        date_display = date_obj.strftime("%a, %b %d %Y").replace(" 0", " ")
        time_display = b.get("time_label") or ""
        print(f"  {facility}{court}  |  {date_display}  |  {time_display}")


async def cmd_cancel(headless: bool = False):
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser, context, page = await open_session(p, headless)

        # ── Find bookings ─────────────────────────────────────────────────────
        bookings = await collect_upcoming_bookings(page)

        if not bookings:
            print("No upcoming bookings found.")
            await browser.close()
            return

        print(f"\nFound {len(bookings)} upcoming booking(s):\n")
        for i, b in enumerate(bookings, 1):
            print(f"  [{i}] {b['label']}")

        print()
        raw = input("Enter the number of the booking to cancel (or 'q' to quit): ").strip()
        if raw.lower() == "q":
            await browser.close()
            return

        try:
            choice = int(raw)
            if not (1 <= choice <= len(bookings)):
                raise ValueError
        except ValueError:
            print(f"Invalid choice '{raw}'. Exiting.")
            await browser.close()
            sys.exit(1)

        selected = bookings[choice - 1]
        print(f"\nCancelling: {selected['label']}")

        # ── Open the action menu ──────────────────────────────────────────────
        await reveal_cancel_option(selected["action_btn"])
        await asyncio.sleep(0.3)

        # Every booking card has its own (initially hidden) cancel-booking-btn
        # anchor. Target the exact one for this card by its unique
        # data-booking-participant-id — otherwise `.first` picks a hidden link
        # from some other card. Fall back to `:visible` if the id is missing.
        pid = selected.get("participant_id")
        if pid:
            # The site renders the dropdown twice (mobile + desktop variants),
            # so the participant-id selector matches two identical anchors.
            # Pick the visible one; if Playwright still sees both as visible,
            # `.first` is a safe tiebreaker since they point at the same booking.
            cancel_link = page.locator(
                f'a.cancel-booking-btn[data-booking-participant-id="{pid}"]:visible'
            ).first
        else:
            cancel_link = page.locator("a.cancel-booking-btn:visible").first

        await cancel_link.wait_for(state="visible", timeout=5000)
        await cancel_link.click()
        await page.wait_for_load_state("networkidle")

        # ── Confirm cancellation modal ────────────────────────────────────────
        # Modal has "BACK" and "YES, CANCEL" buttons (screenshots 12-13)
        confirm_btn = page.get_by_role("button", name="YES, CANCEL").or_(
            page.get_by_text("YES, CANCEL", exact=False)
        ).first
        await confirm_btn.wait_for(state="visible", timeout=5000)
        await confirm_btn.click()

        # Wait for the cancel modal to disappear — this is a reliable signal that
        # the server has processed the cancellation (more robust than networkidle
        # alone, which can resolve before the POST completes in headless mode).
        try:
            await page.locator("#modalCancelBooking-title").wait_for(
                state="hidden", timeout=10000
            )
        except PlaywrightTimeout:
            pass
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)  # brief pause to let the page settle in all modes

        shot = SCREENSHOTS_DIR / f"cancel_confirmation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        await page.screenshot(path=shot)
        print(f"Booking cancelled. Screenshot saved: {shot}")

        if not headless:
            await asyncio.sleep(5)
        await browser.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="UCLA Rec booking sniper — fires at the exact moment a reservation opens",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  uv run bookingbot.py login
  uv run bookingbot.py list                               # interactive sport/date
  uv run bookingbot.py l pb tmr                           # positional + aliases
  uv run bookingbot.py list --sport tennis --date saturday
  uv run bookingbot.py book                               # fully interactive
  uv run bookingbot.py b t sat 10 3                       # tennis, Sat 10 AM, court 3
  uv run bookingbot.py book --sport pickleball --date saturday --time 10AM
  uv run bookingbot.py book --sport tennis --date +3 --time 2PM --headed
  uv run bookingbot.py book --sport pickleball --date tomorrow --time "10:30 AM" --dry-run
        """,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    login_p = subparsers.add_parser(
        "login",
        help="Log in now (types credentials from env/.env; approve the Duo push) and save the session",
    )
    login_p.add_argument(
        "--manual", action="store_true",
        help="Fallback: open a visible browser and complete the whole login by hand",
    )
    login_p.add_argument(
        "--headed", action="store_true",
        help="Run the automated login in a visible browser (default: headless)",
    )

    # Shared help text for the sport/date/time/court arguments.
    sport_help = "Sport: tennis (t, ten) or pickleball (p, pb, pickle)"
    date_help = ("Date: YYYY-MM-DD, today, tomorrow (tmr), weekday (sat, sa), "
                 "or +N days")
    time_help = ("Start time: 10, 2, 10:30, 2PM, '10:30 AM' "
                 "(AM/PM optional: 8-11 = AM, 12-7 = PM)")
    court_help = "Tennis court number (2-6) or 'any'. Ignored for pickleball."

    book_p = subparsers.add_parser(
        "book", aliases=["b"],
        help="Wait for the booking window to open and snipe the target slot",
        description="Positional form: book [SPORT] [DATE] [TIME] [COURT], "
                    "e.g. 'book t sat 10 3'. Anything omitted is prompted for.",
    )
    book_p.set_defaults(command="book")
    book_p.add_argument("sport_pos", nargs="?", metavar="SPORT", help=sport_help)
    book_p.add_argument("date_pos", nargs="?", metavar="DATE", help=date_help)
    book_p.add_argument("time_pos", nargs="?", metavar="TIME", help=time_help)
    book_p.add_argument("court_pos", nargs="?", metavar="COURT", help=court_help)
    book_p.add_argument("--sport", help=f"{sport_help} (prompted if omitted)")
    book_p.add_argument("--date", metavar="DATE", help=f"{date_help} (prompted if omitted)")
    book_p.add_argument("--time", metavar="TIME", help=f"{time_help} (prompted if omitted)")
    book_p.add_argument(
        "--court", metavar="COURT",
        help=f"{court_help} Prompted for tennis if omitted.",
    )
    book_p.add_argument(
        "--headed", action="store_true",
        help="Run the browser headed so you can watch (default: headless)",
    )
    book_p.add_argument(
        "--dry-run", action="store_true",
        help="Navigate and verify the slot is visible, but do not click BOOK NOW",
    )

    list_p = subparsers.add_parser(
        "list", aliases=["l"],
        help="List all time slots on a facility page for a given date",
        description="Positional form: list [SPORT] [DATE], e.g. 'list pb tmr'.",
    )
    list_p.set_defaults(command="list")
    list_p.add_argument("sport_pos", nargs="?", metavar="SPORT", help=sport_help)
    list_p.add_argument("date_pos", nargs="?", metavar="DATE", help=date_help)
    list_p.add_argument("--sport", help=f"{sport_help} (prompted if omitted)")
    list_p.add_argument("--date", metavar="DATE", help=f"{date_help} (prompted if omitted)")
    list_p.add_argument(
        "--headed", action="store_true",
        help="Run the browser headed so you can watch (default: headless)",
    )

    status_p = subparsers.add_parser(
        "status", aliases=["s"],
        help="Show all upcoming bookings",
    )
    status_p.set_defaults(command="status")
    status_p.add_argument(
        "--headed", action="store_true",
        help="Run the browser headed so you can watch (default: headless)",
    )

    cancel_p = subparsers.add_parser(
        "cancel", aliases=["c"],
        help="List upcoming bookings and cancel one interactively",
    )
    cancel_p.set_defaults(command="cancel")
    cancel_p.add_argument(
        "--headed", action="store_true",
        help="Run the browser headed so you can watch (default: headless)",
    )

    inspect_p = subparsers.add_parser(
        "inspect",
        help="Dump the DOM structure of a facility page (for developing selectors)",
        description="Positional form: inspect [SPORT] [DATE].",
    )
    inspect_p.add_argument("sport_pos", nargs="?", metavar="SPORT", help=sport_help)
    inspect_p.add_argument("date_pos", nargs="?", metavar="DATE", help=date_help)
    inspect_p.add_argument(
        "--sport", help=f"{sport_help} (prompted if omitted)",
    )
    inspect_p.add_argument(
        "--date", metavar="DATE",
        help=f"Optional date to click before dumping. {date_help}. If omitted, "
             "inspects the default (today's) tab without prompting.",
    )
    inspect_p.add_argument(
        "--headed", action="store_true",
        help="Run the browser headed so you can watch (default: headless)",
    )

    args = parser.parse_args()

    # Fold positional forms into the --flag attributes; giving both is an error.
    for name in ("sport", "date", "time", "court"):
        pos = getattr(args, f"{name}_pos", None)
        if pos is None:
            continue
        if getattr(args, name) is not None:
            parser.error(f"{name} given both positionally ({pos!r}) and as --{name}")
        setattr(args, name, pos)

    if args.command == "login":
        asyncio.run(cmd_login(manual=args.manual, headless=not args.headed))
    elif args.command == "status":
        asyncio.run(cmd_status(headless=not args.headed))
    elif args.command == "cancel":
        asyncio.run(cmd_cancel(headless=not args.headed))
    elif args.command == "inspect":
        asyncio.run(cmd_inspect(sport=args.sport, date_str=args.date, headless=not args.headed))
    elif args.command == "list":
        asyncio.run(cmd_list(
            sport=args.sport,
            date_str=args.date,
            headless=not args.headed,
        ))
    elif args.command == "book":
        asyncio.run(cmd_book(
            sport=args.sport,
            date_str=args.date,
            time_str=args.time,
            court_str=args.court,
            headless=not args.headed,
            dry_run=args.dry_run,
        ))

if __name__ == "__main__":
    main()
