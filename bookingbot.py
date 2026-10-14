#!/usr/bin/env python3
"""
UCLA Rec Booking Sniper

Commands log in automatically when the saved session is missing or expired:
credentials come from UCLA_USERNAME / UCLA_PASSWORD (env or .env), and you
approve the Duo push on your phone.

Usage (with uv, run 'uv run bookingbot.py' instead of 'python bookingbot.py'):
  python bookingbot.py                                     # menu
  python bookingbot.py login                               # force a fresh login
  python bookingbot.py list                                # interactive sport/date
  python bookingbot.py list pb tmr                         # positional + aliases
  python bookingbot.py list --sport tennis --date saturday
  python bookingbot.py book                                # fully interactive
  python bookingbot.py b t sat 10 3                        # tennis, Sat 10 AM, court 3
  python bookingbot.py b t sy sat 10 3                     # same, at Sycamore
  python bookingbot.py book --sport pickleball --date saturday --time 10AM
  python bookingbot.py book --sport tennis --date +3 --time 2PM --headed
  python bookingbot.py book --sport pickleball --date tomorrow --time "10 AM"
"""

import argparse
import asyncio
import getpass
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from playwright.async_api import (
    async_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeout,
)

# ── Config ────────────────────────────────────────────────────────────────────

SESSION_FILE = Path(__file__).parent / "session.json"
SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
# Optional KEY=VALUE file holding UCLA_USERNAME / UCLA_PASSWORD (gitignored).
ENV_FILE = Path(__file__).parent / ".env"
LOGIN_TIMEOUT_SECS = 180  # how long auto-login waits for SSO + Duo approval
BOOKING_URL = "https://secure.recreation.ucla.edu/booking"
# Every booking, newest first, with a Cancel button on each upcoming one.
BOOKINGS_URL = "https://secure.recreation.ucla.edu/profile/bookings"
LA_TZ = ZoneInfo("America/Los_Angeles")
BOOKING_ADVANCE_HOURS = 72  # slots open this many hours before the court time
# The facility page shows this many date tabs: today plus the next 3 days. A
# date's tab appears at midnight Pacific, VISIBLE_DATE_TABS - 1 days beforehand.
VISIBLE_DATE_TABS = 4
TAB_APPEAR_GRACE_SECS = 10  # wait this long past midnight before looking for a new tab
TAB_VERIFY_TIMEOUT_SECS = 300  # keep retrying a newly-due tab this long before giving up
KEEP_ALIVE_SECS = 120  # while waiting, reload this often so the session doesn't time out
OFFLINE_RETRY_SECS = 30  # after a failed reload (e.g. Wi-Fi dropped), retry this often
# Longest to wait for the page to show a court's slots when sniping. It only
# matters on a slow load; if it runs out, the next check tries again.
SLOT_LOAD_TIMEOUT_MS = 3000
# The site keeps showing "Opens at ..." for a few seconds after a slot really
# opens. Keep checking this long past opening time before giving up.
SNIPE_WINDOW_SECS = 20

# Text on the facility cards as it appears on the booking page. "tennis" is
# SCRC's tennis courts; "sycamore" is the Sycamore tennis courts.
# Adjust these if the site changes its labels — run with --dry-run to verify navigation.
FACILITY_CARD_NAMES = {
    "pickleball": "SCRC Pickleball Courts",
    "tennis": "SCRC Tennis Courts",
    "sycamore": "Sycamore Tennis Courts",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def run_hint(args: str = "") -> str:
    """How to run this script, for hints in messages: plain Python or uv."""
    cmd = f"bookingbot.py {args}".strip()
    return f"'python {cmd}' (or 'uv run {cmd}')"


def network_error_reason(exc: BaseException) -> str | None:
    """If `exc` means the site couldn't be reached (offline, switched Wi-Fi,
    page load timed out), a short reason like 'net::ERR_INTERNET_DISCONNECTED'.
    None for anything else."""
    if isinstance(exc, PlaywrightTimeout):
        return "timed out"
    if isinstance(exc, PlaywrightError):
        m = re.search(r"net::ERR_\w+", str(exc))
        if m:
            return m.group(0)
    return None


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

# Where to play tennis. Pickleball is only at SCRC.
WHERE_ALIASES = {
    "scrc": "scrc", "sc": "scrc", "sunset": "scrc",
    "sycamore": "sycamore", "syc": "sycamore", "sy": "sycamore",
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


def parse_where(s: str) -> str:
    where = WHERE_ALIASES.get(s.strip().lower())
    if where is None:
        raise ValueError(f"Invalid place '{s}'. Use scrc (sc, sunset) or sycamore (sy, syc).")
    return where


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
            f"Invalid time format: '{s}'. Try '10', '2PM', or '10 AM'."
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
            f"{format_day(d)} isn't on the site yet. Only {format_day(today)} through "
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


def ask_yes_no(prompt: str) -> bool:
    """Ask a yes/no question. Anything but y or yes counts as no."""
    return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes")


def resolve_sport(sport: str | None) -> str:
    if sport:
        try:
            return parse_sport(sport)
        except ValueError as e:
            print(f"  {e}")
    return prompt_until_valid("Sport (t/tennis, pb/pickleball)", parse_sport)


def resolve_facility(sport: str, where: str | None) -> str:
    """Turn a sport plus a place into a FACILITY_CARD_NAMES key. Tennis is at
    SCRC or Sycamore, so it asks where if `where` is missing (Enter = SCRC).
    Pickleball is only at SCRC."""
    if where:
        try:
            where = parse_where(where)
        except ValueError as e:
            print(f"  {e}")
            where = None
    if sport != "tennis":
        if where == "sycamore":
            print("Error: Sycamore only has tennis courts.")
            sys.exit(1)
        return sport
    if where is None:
        where = prompt_until_valid("Where (scrc/sycamore)", parse_where, default="scrc")
    return "sycamore" if where == "sycamore" else "tennis"


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
        "Time (e.g. 10, 2, 2PM)",
        parse_time_shortcut,
    )


# Court tab label prefix and court numbers, for the facilities where you pick a court.
COURTS = {
    "tennis": ("SCRC - ", [2, 3, 4, 5, 6]),
    "sycamore": ("SYCA - ", [1, 2, 3, 4, 5, 6]),
}


def resolve_court(court_str: str | None, facility: str) -> str | None:
    """Return a court tab label like 'SCRC - 3', or None for any court.
    Only meaningful for facilities in COURTS; returns None for pickleball."""
    if facility not in COURTS:
        return None
    prefix, valid = COURTS[facility]
    if court_str is None:
        def _v(raw: str) -> str | None:
            raw = raw.strip().lower()
            if raw in ("", "any"):
                return ""
            try:
                n = int(raw)
            except ValueError:
                raise ValueError(f"Must be a court number ({valid[0]}-{valid[-1]}) or 'any'.")
            if n not in valid:
                raise ValueError(f"Must be {valid[0]}-{valid[-1]} or 'any'.")
            return f"{prefix}{n}"
        result = prompt_until_valid(
            f"Court ({valid[0]}-{valid[-1]} or 'any')",
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
        print(f"Invalid court '{court_str}'. Must be {valid[0]}-{valid[-1]} or 'any'.")
        return resolve_court(None, facility)
    if n not in valid:
        print(f"Invalid court '{court_str}'. Must be {valid[0]}-{valid[-1]}.")
        return resolve_court(None, facility)
    return f"{prefix}{n}"


def resolve_booking_args(
    sport: str | None, where: str | None, date_str: str | None, time_str: str | None,
    court_str: str | None = None,
):
    """Normalize provided args and interactively prompt for any missing ones.
    Returns (facility, date, time, court), facility being a FACILITY_CARD_NAMES key."""
    facility = resolve_facility(resolve_sport(sport), where)
    # Future dates are fine for book — it waits for the tab to appear.
    date_str = resolve_date(date_str, check=check_not_past)
    return facility, date_str, resolve_time(time_str), resolve_court(court_str, facility)


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
        print("UCLA_USERNAME / UCLA_PASSWORD not set (env or .env). Enter them now "
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
                    # Only ever type the password into UCLA's own login page, not
                    # e.g. a Wi-Fi sign-in page that took over the connection.
                    if not (host == "ucla.edu" or host.endswith(".ucla.edu")):
                        raise LoginError(f"Expected the UCLA login page but got {host or page.url}; "
                                         "not entering your password.")
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
                    raise LoginError("UCLA login form is still showing. Was the username or password rejected?")
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
        print(f"Retry, or run {run_hint('login --manual')} to log in by hand.")
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
                print(f"Retry, or run {run_hint('login --manual')} to log in by hand.")
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
        print(f"Run {run_hint()} to open the menu.")
        await browser.close()


# ── Book: navigation helpers ──────────────────────────────────────────────────

async def navigate_to_facility(page, facility: str):
    """Navigate from the booking home to the page for `facility`, a
    FACILITY_CARD_NAMES key."""
    await page.goto(BOOKING_URL)
    await page.wait_for_load_state("networkidle")

    if not await check_logged_in(page):
        raise RuntimeError("Session expired. Re-run the command to log in again.")

    card_name = FACILITY_CARD_NAMES[facility]
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


def slot_text_parts(user_time: str) -> tuple[str, str]:
    """Split a time like "1:00 PM" into what a slot's text starts and ends
    with: ("1:00", "PM") matches "1:00 - 1:50 PM" but not "11:00 - 11:50 AM"."""
    t = datetime.strptime(user_time.strip().upper(), "%I:%M %p")
    return f"{t.hour % 12 or 12}:{t.minute:02d}", "AM" if t.hour < 12 else "PM"


async def js_click_book_button(page, user_time: str) -> str:
    """
    Click the Book Now button in JS directly — zero Playwright auto-waiting.
    Returns "clicked", "missing" (the selected court's slots have loaded but
    this time has no Book Now button), or "loading" (they haven't loaded yet).

    Clicking a date or court tab empties #divBookingSlots right away, then
    fills it with that court's slots inside a div.bookingTab_panel. The site
    sets the panel's id to "panel_<court id>" (the court tab's
    data-facility-id), but only if some slot has a button (Book Now or
    Unavailable); while every slot still says "Opens at", it has no id.
    Requiring the selected court's panel means another court's leftover
    slots never count. Pickleball has no court tabs, so any content counts
    there.

    Each slot's Book Now button has `data-apt-id` and a `data-slot-text`
    attribute like "2:00 - 2:50 PM". Matching on the attribute's start means
    "1:00" doesn't accidentally match "11:00".
    """
    prefix, ampm = slot_text_parts(user_time)
    return await page.evaluate(
        """
        ({prefix, ampm}) => {
            const box = document.querySelector('#divBookingSlots');
            if (!box || box.children.length === 0) return 'loading';
            let scope = box;
            const tab = document.querySelector(
                '#tabBookingFacilities [role=tab][aria-selected=true]');
            const courtId = tab && tab.getAttribute('data-facility-id');
            if (courtId) {
                const panel = box.querySelector(`[id="panel_${courtId}"]`);
                if (panel) scope = panel;
                // Another court's slots are still showing
                else if (box.querySelector('.bookingTab_panel[id^="panel_"]')) return 'loading';
                // No buttons at all: "no available times", or all "Opens at"
                else return 'missing';
            }
            const buttons = scope.querySelectorAll('button[data-apt-id][data-slot-text]');
            for (const btn of buttons) {
                const dst = (btn.getAttribute('data-slot-text') || '').trim();
                if (!dst.startsWith(prefix + ' -')) continue;
                if (!dst.toUpperCase().endsWith(ampm)) continue;
                btn.click();
                return 'clicked';
            }
            return 'missing';
        }
        """,
        {"prefix": prefix, "ampm": ampm},
    )


async def poll_and_js_click_book(
    page, user_time: str, timeout_ms: int = SLOT_LOAD_TIMEOUT_MS
) -> str:
    """
    Tight-poll the page and, the moment the target BOOK NOW button exists,
    click it via JS in the same round trip. This is the fastest path: no
    Playwright click auto-waiting at all.

    Stops as soon as the selected court's slots have loaded, so a court
    without the time is skipped right away. `timeout_ms` only matters when
    they're slow to load (slow Wi-Fi, or the site swamped at opening time).
    Returns "clicked", "missing", or "timeout".
    """
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        status = await js_click_book_button(page, user_time)
        if status == "missing":
            print(f"    slots shown: {await describe_slots(page)}")
        if status != "loading":
            return status
        await asyncio.sleep(0.02)
    return "timeout"


async def describe_slots(page) -> str:
    """One-line summary of the slots currently shown, for diagnosing a miss:
    each Book Now button's time and spots left, or the panel's text if none."""
    return await page.evaluate("""() => {
        const box = document.querySelector('#divBookingSlots');
        if (!box) return '(no slot container)';
        const btns = [...box.querySelectorAll('button[data-apt-id][data-slot-text]')];
        if (btns.length) return btns.map(b =>
            `${b.getAttribute('data-slot-text')} (${b.getAttribute('data-spots-left-text') || '?'})`
        ).join(' | ');
        return '(no Book Now buttons) ' + box.innerText.replace(/\\s+/g, ' ').slice(0, 300);
    }""")


async def slot_urls(page, sample_url: str, court_pref: str | None) -> list[tuple[str | None, str]]:
    """List (court tab label, slots URL) for each court to watch. The page loads
    a court's slots from .../slots/<court id>/<year>/<month>/<day>, and
    `sample_url` is one it requested for the target date, so swapping in each
    court tab's id gives the rest. Pickleball has no court tabs, so it's just
    [(None, sample_url)]. With `court_pref`, only that court."""
    base, rest = sample_url.split("/slots/", 1)
    date_part = rest.split("/", 1)[1]
    courts = await page.evaluate("""() => [...document.querySelectorAll(
        '#tabBookingFacilities [role=tab][data-facility-id]')].map(
        t => [t.innerText.trim(), t.getAttribute('data-facility-id')])""")
    if not courts:
        return [(None, sample_url)]
    urls = [(label, f"{base}/slots/{court_id}/{date_part}")
            for label, court_id in courts if court_pref in (None, label)]
    if not urls:
        raise RuntimeError(f"No court tab '{court_pref}' on the page "
                           f"(found {', '.join(label for label, _ in courts)}).")
    return urls


async def fetch_slot_state(page, url: str, user_time: str) -> str:
    """Fetch one court's slots straight from the server, without touching the
    page, and say where `user_time` stands: "open" (has a Book Now button),
    "pending" (still says "Opens at ..."), "taken" (full, or not offered), or
    "error" (the request failed)."""
    prefix, ampm = slot_text_parts(user_time)
    return await page.evaluate(
        """
        async ({url, prefix, ampm}) => {
            const isTarget = s => {
                s = (s || '').trim();
                return s.startsWith(prefix + ' -') && s.toUpperCase().endsWith(ampm);
            };
            try {
                const res = await fetch(url, {credentials: 'same-origin', cache: 'no-store'});
                if (!res.ok || res.redirected) return 'error';
                const doc = new DOMParser().parseFromString(await res.text(), 'text/html');
                for (const b of doc.querySelectorAll('button[data-apt-id][data-slot-text]'))
                    if (isTarget(b.getAttribute('data-slot-text'))) return 'open';
                for (const card of doc.querySelectorAll('.card')) {
                    const heading = card.querySelector('strong');
                    if (heading && isTarget(heading.textContent))
                        return /Opens at/i.test(card.textContent) ? 'pending' : 'taken';
                }
                return 'taken';
            } catch (e) {
                return 'error';
            }
        }
        """,
        {"url": url, "prefix": prefix, "ampm": ampm},
    )


async def show_court(page, court: str | None, target_date: datetime) -> None:
    """Make the page (re)load `court`'s slots for the date tab already
    selected. Clicking a tab that's already selected still reloads it. With no
    court tabs (pickleball), re-click the target date's tab instead."""
    if court:
        await click_court_tab(page, court)
    else:
        await click_date_tab(page, target_date, wait_networkidle=False)


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


async def click_court_tab(page, label: str) -> bool:
    """JS-click the court tab whose text is `label` (e.g. 'SCRC - 3').
    Returns False if there's no such tab."""
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


# ── Book: keep-alive loop ─────────────────────────────────────────────────────

async def keep_alive(
    p, page, until: datetime, headless: bool, lead: float = 35.0, label: str = "Opens"
) -> bool:
    """
    Reload the booking home page every KEEP_ALIVE_SECS to prevent session timeout.
    If the session dies anyway, auto-login again (Duo push) and keep waiting.
    Stops `lead` seconds before `until` (default 35 s, so the caller can pre-position).
    `label` names the event being waited for in the progress lines.

    If the site can't be reached (Wi-Fi dropped, switched networks), retries
    every OFFLINE_RETRY_SECS, and if it's still unreachable when the wait would
    end, keeps retrying right up to `until`. Returns False if the site was
    still unreachable then, True otherwise.
    """
    offline = False
    while True:
        now = datetime.now(LA_TZ)
        remaining = (until - now).total_seconds()

        if remaining <= (0 if offline else lead):
            return not offline

        if offline:
            sleep_secs = min(OFFLINE_RETRY_SECS, remaining)
        else:
            sleep_secs = min(KEEP_ALIVE_SECS, remaining - lead)
            h = int(remaining // 3600)
            m = int((remaining % 3600) // 60)
            print(f"  [{now.strftime('%H:%M:%S')}] {label} in {h}h {m}m. "
                  f"Next keep-alive in {int(sleep_secs)}s...")
        await asyncio.sleep(sleep_secs)

        try:
            await page.goto(BOOKING_URL)
            await page.wait_for_load_state("networkidle")

            if not await check_logged_in(page):
                print("  Session expired during wait. Logging in again.")
                try:
                    await relogin_context(p, page.context, headless)
                except LoginError as e:
                    raise RuntimeError(f"Session expired during wait and re-login failed: {e}") from e
                await page.goto(BOOKING_URL)
                await page.wait_for_load_state("networkidle")
                if not await check_logged_in(page):
                    raise RuntimeError("Re-login succeeded but the booking site still shows you signed out.")
        except PlaywrightError as exc:
            reason = network_error_reason(exc)
            if reason is None:
                raise
            stamp = datetime.now(LA_TZ).strftime('%H:%M:%S')
            if offline:
                print(f"  [{stamp}] Still can't reach the UCLA site. "
                      f"Retrying in {OFFLINE_RETRY_SECS}s...")
            else:
                print(f"  [{stamp}] Can't reach the UCLA site ({reason}). "
                      f"Retrying every {OFFLINE_RETRY_SECS}s...")
            offline = True
            continue

        if offline:
            print(f"  [{datetime.now(LA_TZ).strftime('%H:%M:%S')}] Back online.")
            offline = False


async def verify_date_tab(page, facility: str, target_date: datetime) -> bool:
    """Open the facility page and click the target date tab, retrying for up
    to TAB_VERIFY_TIMEOUT_SECS in case the site's midnight rollover lags.
    Returns False if the site couldn't be reached to check. Raises
    PlaywrightTimeout if the site loaded but the tab never showed up."""
    deadline = time.monotonic() + TAB_VERIFY_TIMEOUT_SECS
    while True:
        try:
            await navigate_to_facility(page, facility)
        except PlaywrightError as exc:
            if network_error_reason(exc) is None:
                raise
            if time.monotonic() >= deadline:
                return False
            print(f"  Can't reach the UCLA site. Retrying in {OFFLINE_RETRY_SECS}s...")
            await asyncio.sleep(OFFLINE_RETRY_SECS)
            continue
        try:
            await click_date_tab(page, target_date)
            return True
        except PlaywrightTimeout:
            if time.monotonic() >= deadline:
                raise
            print("  Tab not there yet. Retrying in 30s...")
            await asyncio.sleep(30)


# ── Book command ──────────────────────────────────────────────────────────────

async def cmd_book(
    sport: str | None,
    where: str | None,
    date_str: str | None,
    time_str: str | None,
    court_str: str | None,
    headless: bool,
    dry_run: bool,
):
    SCREENSHOTS_DIR.mkdir(exist_ok=True)

    facility, date_str, time_str, court_pref = resolve_booking_args(
        sport, where, date_str, time_str, court_str)

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
    print(f"Where:         {FACILITY_CARD_NAMES[facility]}")
    if court_pref:
        print(f"Court:         {court_pref}")
    print(f"Target slot:   {target_dt.strftime('%a %Y-%m-%d %I:%M %p %Z')}")
    print(f"Date tab:      {tab_time.strftime('%a %Y-%m-%d %I:%M %p %Z')}")
    print(f"Booking opens: {open_time.strftime('%a %Y-%m-%d %I:%M %p %Z')}")
    print(f"Current time:  {now.strftime('%a %Y-%m-%d %I:%M %p %Z')}")

    day_name = target_date.strftime("%A")
    tab_pending = tab_time > now
    if open_time < now:
        print("\nNote: Booking window is already open. Attempting to book immediately.")
    elif tab_pending:
        print(f"\n{day_name} tab not available yet. Waiting until "
              f"{tab_time.strftime('%a %I:%M %p')} to load it, keeping the session alive until then.")
    else:
        secs = (open_time - now).total_seconds()
        print(f"\nBooking opens in {int(secs // 3600)}h {int((secs % 3600) // 60)}m. "
              "Keeping session alive until then.")

    async with async_playwright() as p:
        browser, context, page = await open_session(p, headless)
        print("\nSession valid.")

        if not dry_run:
            same_day = [b for b in await collect_upcoming_bookings(page)
                        if b["facility"] == facility and b["date"] == date_str]
            if same_day:
                print(f"\nYou already have a booking at {FACILITY_CARD_NAMES[facility]} that day:")
                for b in same_day:
                    print(f"  {b['label']}")
                print("UCLA allows one booking per day at each facility, so this one will fail "
                      "unless you cancel that booking first.")
                if not ask_yes_no("Keep going anyway?"):
                    await browser.close()
                    return

        if tab_pending:
            # Wait for the date tab to appear, then confirm it's really there
            # so a problem surfaces now rather than at open time.
            online = await keep_alive(
                p, page, tab_time + timedelta(seconds=TAB_APPEAR_GRACE_SECS), headless,
                lead=0, label=f"{day_name} tab appears",
            )
            if online:
                print(f"\n{day_name} tab should be available now. Checking...")
                try:
                    online = await verify_date_tab(page, facility, target_date)
                except PlaywrightTimeout as exc:
                    shot = SCREENSHOTS_DIR / f"tab_missing_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                    await page.screenshot(path=shot)
                    print(f"\nError: {day_name} tab never appeared: {exc}")
                    print(f"Screenshot saved: {shot}")
                    await browser.close()
                    sys.exit(1)
            if online:
                secs = (open_time - datetime.now(LA_TZ)).total_seconds()
                print(f"{day_name} tab found. Booking opens in "
                      f"{int(secs // 3600)}h {int((secs % 3600) // 60)}m. Keeping session alive.")
            else:
                print(f"\nCan't reach the UCLA site to check the {day_name} tab. "
                      "Will keep trying until booking opens.")

        # Keep session alive until 35 s before the booking window opens
        if not await keep_alive(p, page, open_time, headless):
            print(f"\nError: Still couldn't reach the UCLA site when booking opened "
                  f"({open_time.strftime('%a %I:%M %p')}). Check your connection and try again.")
            await browser.close()
            sys.exit(1)

        # ── Navigate to facility ──────────────────────────────────────────────
        print("\nNavigating to facility page...")
        await navigate_to_facility(page, facility)

        # ── Pre-position ──────────────────────────────────────────────────────
        # Sit on the target date's tab, noting the URL the page loads its slots
        # from, so the slots can be fetched directly at opening time.
        print(f"Loading the {format_date_tab(target_date)} tab...")
        try:
            async with page.expect_request(lambda r: "/slots/" in r.url,
                                           timeout=10_000) as slots_request:
                await click_date_tab(page, target_date)
        except PlaywrightTimeout:
            shot = SCREENSHOTS_DIR / f"fire_failed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            try:
                await page.screenshot(path=shot)
                print(f"Diagnostic screenshot: {shot}  (url: {page.url})")
            except Exception:
                pass
            raise
        urls = await slot_urls(page, (await slots_request.value).url, court_pref)

        if open_time > datetime.now(LA_TZ):
            # Wait until T-1 s, then tight-loop until open_time.
            remaining = (open_time - datetime.now(LA_TZ)).total_seconds()
            if remaining > 1.0:
                print(f"Holding for {remaining:.1f}s...")
                await asyncio.sleep(remaining - 1.0)
            while datetime.now(LA_TZ) < open_time:
                await asyncio.sleep(0.02)

        # ── Snipe ─────────────────────────────────────────────────────────────
        # Fetch the slots one court at a time, rotating, until one shows BOOK
        # NOW for the time, then have the page load that court and click it
        # there. (Fetching every court at once makes the site answer some of
        # them slowly.) The page never leaves the target date, so whatever it
        # shows is for the right day.
        fire_ts = datetime.now(LA_TZ).strftime("%H:%M:%S.%f")
        watching = ", ".join(court for court, _ in urls if court) or "the slots"
        print(f"\n[{fire_ts}] Firing: checking {watching} for {time_str}...")
        t_fire = time.monotonic()

        def elapsed() -> str:
            return f"[+{(time.monotonic() - t_fire)*1000:6.0f} ms]"

        states: dict[str | None, str | None] = {court: None for court, _ in urls}
        booked = False
        turn = 0
        while not booked and time.monotonic() - t_fire < SNIPE_WINDOW_SECS:
            live = [(court, url) for court, url in urls if states[court] != "taken"]
            if not live:
                break
            court, url = live[turn % len(live)]
            turn += 1
            state = await fetch_slot_state(page, url, time_str)
            if state != states[court]:
                print(f"  {elapsed()} {f'{court}: ' if court else ''}{state}")
                states[court] = state
            if state != "open":
                continue

            await show_court(page, court, target_date)
            if dry_run:
                await wait_for_slots(page)
                shot = SCREENSHOTS_DIR / f"dryrun_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                await page.screenshot(path=shot)
                print(f"[DRY RUN] Would click BOOK NOW for {time_str}"
                      f"{f' on {court}' if court else ''}.")
                print(f"Screenshot saved: {shot}")
                if not headless:
                    await asyncio.sleep(10)
                await browser.close()
                return
            status = await poll_and_js_click_book(page, time_str)
            print(f"  {elapsed()} page shows {court or 'slots'}: {status}")
            if status == "clicked":
                ts = datetime.now(LA_TZ).strftime("%H:%M:%S.%f")
                print(f"[{ts}] Clicked BOOK NOW{f' on {court}' if court else ''} via JS")
                await page.wait_for_load_state("networkidle")
                booked = True

        if not booked:
            seen = set(states.values())
            if seen == {"taken"}:
                target = court_pref or ("every court" if urls[0][0] else None)
                reason = (f"{time_str} is taken or not offered"
                          f"{f' on {target}' if target else ''}")
            elif "open" in seen:
                reason = f"{time_str} was open, but the page wouldn't show its BOOK NOW button"
            elif "pending" in seen:
                reason = (f"{time_str} still said 'Opens at' "
                          f"{SNIPE_WINDOW_SECS}s after opening time")
            else:
                reason = "the UCLA site wouldn't return the slots"
            shot = SCREENSHOTS_DIR / f"failed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            await page.screenshot(path=shot)
            print(f"\nCouldn't book: {reason}.")
            print(f"Screenshot saved: {shot}")
            await browser.close()
            sys.exit(1)

        # ── Confirmation ──────────────────────────────────────────────────────
        print("\nBooking complete! Saving confirmation screenshot...")

        shot = await save_bookings_screenshot(page, "booking_confirmation")
        print(f"Confirmation screenshot: {shot}")

        if not headless:
            print("Browser stays open for 30 s. Press Ctrl+C to close early.")
            await asyncio.sleep(30)

        await browser.close()


# ── Inspect command ──────────────────────────────────────────────────────────

async def cmd_inspect(
    sport: str | None, where: str | None, date_str: str | None, headless: bool = True
):
    """Navigate to a facility page and dump its structure — helps find
    faster selectors for tabs, time slot rows, and BOOK NOW buttons."""
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    facility = resolve_facility(resolve_sport(sport), where)

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

        print(f"Navigating to {FACILITY_CARD_NAMES[facility]}...")
        await navigate_to_facility(page, facility)

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

        print(f"-- {len(dump['tabs'])} [role=tab] elements --")
        for t in dump["tabs"]:
            print(f"  <{t['tag'].lower()} class='{t['cls']}' id='{t['id']}'>"
                  f"  text={t['text']!r}")

        print(f"\n-- {len(dump['bookButtons'])} 'Book Now' elements --")
        for b in dump["bookButtons"][:10]:
            keys = [k for k in b["attrs"] if k not in ("class", "id")]
            extra = " ".join(f"{k}={b['attrs'][k]!r}" for k in keys[:4])
            print(f"  <{b['tag'].lower()} class='{b['cls']}' {extra}>")

        print("\n-- ancestors of first Book Now --")
        for n in dump["rowChain"]:
            print(f"  depth={n['depth']} <{n['tag'].lower()} "
                  f"class='{(n['cls'] or '')[:40]}' role={n['role']!r}>")
            print(f"    text={n['text']!r}")

        print(f"\n-- {len(dump['slotTexts'])} slot-row text samples --")
        for s in dump["slotTexts"][:10]:
            print(f"  {s!r}")

        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        shot = SCREENSHOTS_DIR / f"inspect_{facility}_{stamp}.png"
        await page.screenshot(path=shot, full_page=True)
        html_path = SCREENSHOTS_DIR / f"inspect_{facility}_{stamp}.html"
        html_path.write_text(await page.content(), encoding="utf-8")
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
                         the one-booking-per-day rule)
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


async def cmd_list(sport: str | None, where: str | None, date_str: str | None, headless: bool):
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    facility = resolve_facility(resolve_sport(sport), where)
    date_str = resolve_date(date_str, check=check_tab_visible)
    target_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=LA_TZ)

    async with async_playwright() as p:
        browser, context, page = await open_session(p, headless)

        # Collect the user's upcoming bookings so we can mark slots they've
        # already booked in the listing below.
        all_user_bookings = await collect_upcoming_bookings(page)
        user_bookings_here = [
            b for b in all_user_bookings
            if b["facility"] == facility and b["date"] == date_str
        ]

        print(f"\nNavigating to {FACILITY_CARD_NAMES[facility]}...")
        await navigate_to_facility(page, facility)

        # The site defaults to today's tab, so skip the click for today.
        today = datetime.now(LA_TZ).date()
        if target_date.date() != today:
            try:
                await click_date_tab(page, target_date)
            except PlaywrightTimeout as exc:
                print(f"\nError: {exc}")
                await browser.close()
                sys.exit(1)

        header = f"=== {FACILITY_CARD_NAMES[facility]} | {target_date.strftime('%a, %b %d %Y').replace(' 0', ' ')} ==="
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
        print("  (You already have a booking here that day, so new bookings will be rejected.)\n")

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
        print("  (You already have a booking here that day, so new bookings will be rejected.)\n")

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
    not_open_list: list[tuple[str, list[str]]] = []

    for label, info in sorted_items:
        if info["booked_by_user"]:
            your_list.append((label, info["booked_by_user"]))
        elif info["available"]:
            avail_list.append((label, info["available"]))
        elif info["not_yet_open"]:
            # Some courts can show 0 spots before the window opens (facility
            # holds/lessons), so one blocked court must not mark the time full.
            not_open_list.append((label, info["not_yet_open"]))
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
        for label, courts in not_open_list:
            n = len(courts)
            word = "court" if n == 1 else "courts"
            print(f"    {label}  ({n} {word}: {', '.join(courts)})")


# ── Your bookings (status, cancel, list, book) ───────────────────────────────

def parse_booking_card(raw: str) -> dict:
    """Parse a row of the bookings page, for example
      'SCRC Tennis Courts - SCRC - 2 Tue, Apr 14 2026 12:00 - 12:50 PM CANCEL'
    into
      {"facility": "tennis", "court": "SCRC - 2", "date": "2026-04-14", "start_minute": 720,
       "label": "SCRC Tennis Courts (SCRC - 2)  |  Tue, Apr 14 2026  |  12:00 - 12:50 PM"}

    "facility" is the FACILITY_CARD_NAMES key (None for others, like squash),
    and "court" is None for pickleball (its bookings all say 'SCRC - 1').
    Fields that can't be parsed are None, and the label falls back to the raw text.
    """
    facility = next((key for key, name in FACILITY_CARD_NAMES.items() if raw.startswith(name)), None)

    name = court = None
    m = re.match(r"(.+?Courts)(?:\s*-\s*([A-Z]+\s*-\s*\d+))?", raw)
    if m:
        name = m.group(1).strip()
        court = m.group(2) if facility != "pickleball" else None

    date_text = date_iso = None
    m = re.search(r"\w{3},\s+(\w{3})\s+(\d{1,2})\s+(\d{4})", raw)
    if m:
        date_text = m.group(0)
        try:
            date_iso = datetime.strptime(" ".join(m.groups()), "%b %d %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    m = re.search(r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\s*(?:AM|PM)", raw)
    time_label = m.group(0) if m else None
    start_minute = parse_slot_start_minute(time_label) if time_label else None

    if name and court:
        name = f"{name} ({court})"
    parts = [x for x in (name, date_text, time_label) if x]
    return {
        "facility": facility,
        "court": court,
        "date": date_iso,
        "start_minute": start_minute,
        "label": "  |  ".join(parts) if parts else raw[:80],
    }


def booking_start(booking: dict) -> datetime | None:
    """When a parsed booking starts (Pacific time), or None if it didn't parse."""
    if booking["date"] is None or booking["start_minute"] is None:
        return None
    day = datetime.strptime(booking["date"], "%Y-%m-%d").replace(tzinfo=LA_TZ)
    return day + timedelta(minutes=booking["start_minute"])


async def save_bookings_screenshot(page, prefix: str) -> Path:
    """
    Screenshot the full bookings list at BOOKINGS_URL and return the file path.

    The list is sorted by date, not booking order, so the booking of interest
    can be in any row. The window is grown to the page's full height so the
    cookie notice, which is pinned to the bottom of the window, lands on the
    footer instead of covering rows. (A full_page screenshot alone doesn't do
    this: pinned elements stay at the current scroll spot.)
    """
    try:
        await page.goto(BOOKINGS_URL)
        await page.wait_for_load_state("networkidle")
        height = await page.evaluate("document.documentElement.scrollHeight")
        await page.set_viewport_size({"width": 1280, "height": height})
        await asyncio.sleep(1)  # let the page settle before the screenshot
    except Exception:
        pass  # Fall through and screenshot whatever state we're in
    shot = SCREENSHOTS_DIR / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    await page.screenshot(path=shot, full_page=True)
    return shot


async def collect_upcoming_bookings(page) -> list[dict]:
    """
    Return the user's upcoming bookings from BOOKINGS_URL, soonest first, one
    dict per booking: parse_booking_card's fields plus "participant_id", the
    booking's id from its Cancel button (None if it has no Cancel button).

    That page lists every booking, newest first and 10 to a page, so the
    upcoming ones are all on the first page. (The booking home page only
    shows 3.)
    """
    await page.goto(BOOKINGS_URL)
    for attempt in range(2):
        await page.wait_for_load_state("networkidle")
        try:
            rows = await page.evaluate("""() =>
                [...document.querySelectorAll('#grdCourtBookings tbody tr')].map(tr => {
                    const btn = tr.querySelector('button.btn-cancel-grid');
                    return {
                        text: tr.innerText,
                        id: btn ? btn.getAttribute('data-booking-participant-id') : null,
                    };
                })""")
            break
        except PlaywrightError:
            # Loading this page posts back to the server and reloads it once.
            # If the read landed during that reload, read again.
            if attempt:
                raise

    now = datetime.now(LA_TZ)
    bookings = []
    for row in rows:
        booking = parse_booking_card(" ".join(row["text"].split()))
        start = booking_start(booking)
        # A row that didn't parse counts as upcoming if it can still be cancelled.
        if not (start > now if start else row["id"]):
            continue
        booking["participant_id"] = row["id"]
        bookings.append(booking)

    # Soonest first; rows that didn't parse go last.
    bookings.sort(key=lambda b: (b["date"] or "9999", b["start_minute"] or 0))
    return bookings


async def cmd_status(headless: bool = True):
    """Print the user's upcoming bookings."""
    async with async_playwright() as p:
        browser, context, page = await open_session(p, headless)

        bookings = await collect_upcoming_bookings(page)
        await browser.close()

    if not bookings:
        print("\nNo upcoming bookings.")
        return

    print(f"\n=== Your upcoming bookings ({len(bookings)}) ===")
    for b in bookings:
        print(f"  {b['label']}")


async def cmd_cancel(headless: bool = False):
    SCREENSHOTS_DIR.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser, context, page = await open_session(p, headless)

        # ── Find bookings ─────────────────────────────────────────────────────
        bookings = [b for b in await collect_upcoming_bookings(page) if b["participant_id"]]

        if not bookings:
            print("No upcoming bookings found.")
            await browser.close()
            return

        print(f"\nFound {len(bookings)} upcoming booking(s):\n")
        for i, b in enumerate(bookings, 1):
            print(f"  [{i}] {b['label']}")

        print()

        def pick(raw: str) -> int | None:
            if raw.lower() == "q":
                return None
            if raw.isdigit() and 1 <= int(raw) <= len(bookings):
                return int(raw)
            raise ValueError(f"Enter a number from 1 to {len(bookings)}, or q to quit.")

        choice = prompt_until_valid("Enter the number of the booking to cancel (or 'q' to quit)", pick)
        if choice is None:
            await browser.close()
            return

        selected = bookings[choice - 1]
        print(f"\nCancelling: {selected['label']}")

        # ── Confirm in the cancel dialog ──────────────────────────────────────
        # The row's Cancel button copies the booking's id into the dialog, and
        # "Yes, Cancel" cancels whichever id the dialog holds, so check it first.
        pid = selected["participant_id"]
        await page.locator(f'button.btn-cancel-grid[data-booking-participant-id="{pid}"]').click()
        dialog = page.locator("#modalCancelBooking")
        await dialog.wait_for(state="visible", timeout=5000)
        if await page.locator("#hdnBookingParticipantId").input_value() != pid:
            print("Error: The cancel dialog opened for a different booking, so nothing was cancelled.")
            await browser.close()
            sys.exit(1)
        await page.locator("#btnCancelBooking").click()

        try:
            await dialog.wait_for(state="hidden", timeout=10000)
        except PlaywrightTimeout:
            pass
        await page.wait_for_load_state("networkidle")

        shot = await save_bookings_screenshot(page, "cancel_confirmation")

        # Make sure it's really gone before saying so.
        still_there = any(b["participant_id"] == pid for b in await collect_upcoming_bookings(page))
        if still_there:
            print(f"Error: The booking still shows on the bookings page. Check the website. "
                  f"Screenshot saved: {shot}")
            await browser.close()
            sys.exit(1)
        print(f"Booking cancelled. Screenshot saved: {shot}")

        if not headless:
            await asyncio.sleep(5)
        await browser.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def run(coro) -> None:
    """Run a command's coroutine. Timeouts and Ctrl+C print a short message
    and exit instead of dumping a traceback."""
    try:
        asyncio.run(coro)
    except PlaywrightTimeout as exc:
        detail = str(exc).splitlines()[0] if str(exc) else ""
        m = re.search(r"Timeout (\d+)ms exceeded", detail)
        if m:
            # Usually a slow or dropped connection.
            print(f"\nError: Timed out after {int(m.group(1)) // 1000} seconds waiting for "
                  "the UCLA site. Check your network connection and try again.")
        else:
            # One of our own timeouts, e.g. "Could not find date tab ...".
            print(f"\nError: {detail}")
        sys.exit(1)
    except PlaywrightError as exc:
        reason = network_error_reason(exc)
        if reason is None:
            raise
        print(f"\nError: Lost the connection to the UCLA site ({reason}). "
              "Check your network connection and try again.")
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.")
        sys.exit(130)


def run_menu() -> None:
    """No command given: pick one from a menu, run it, then show the menu again."""
    items = [
        ("Book a court", lambda: cmd_book(None, None, None, None, None, headless=True, dry_run=False)),
        ("See what's open", lambda: cmd_list(None, None, None, headless=True)),
        ("See your bookings", lambda: cmd_status(headless=True)),
        ("Cancel a booking", lambda: cmd_cancel(headless=True)),
    ]
    while True:
        print("\n=== UCLA Rec booking bot ===")
        for i, (label, _) in enumerate(items, 1):
            print(f"  {i}. {label}")
        print("  q. Quit")
        try:
            choice = input(f"Choose 1-{len(items)} or q: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return
        if choice == "q":
            return
        if not (choice.isdigit() and 1 <= int(choice) <= len(items)):
            print(f"  '{choice}' isn't an option.")
            continue
        try:
            run(items[int(choice) - 1][1]())
        except SystemExit:
            pass  # the command already said why it stopped
        except Exception:
            traceback.print_exc()  # keep the menu (and its window) open


def main():
    parser = argparse.ArgumentParser(
        description="UCLA Rec booking sniper: books a court the moment its reservation opens. "
                    "Run with no command for a menu.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples (with uv, run 'uv run bookingbot.py' instead of 'python bookingbot.py'):
  python bookingbot.py                                  # menu
  python bookingbot.py login
  python bookingbot.py list                             # interactive sport/date
  python bookingbot.py l pb tmr                         # positional + aliases
  python bookingbot.py list --sport tennis --date saturday
  python bookingbot.py book                             # fully interactive
  python bookingbot.py b t sat 10 3                     # tennis, Sat 10 AM, court 3
  python bookingbot.py b t sy sat 10 3                  # same, at Sycamore
  python bookingbot.py book --sport pickleball --date saturday --time 10AM
  python bookingbot.py book --sport tennis --date +3 --time 2PM --headed
  python bookingbot.py book --sport pickleball --date tomorrow --time "10 AM"
        """,
    )
    subparsers = parser.add_subparsers(dest="command")

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

    # Shared help text for the sport/where/date/time/court arguments.
    sport_help = "Sport: tennis (t, ten) or pickleball (p, pb, pickle)"
    where_help = ("Where to play tennis: scrc (sc, sunset) or sycamore (sy, syc). "
                  "Can be left out, which means SCRC")
    date_help = ("Date: YYYY-MM-DD, today, tomorrow (tmr), weekday (sat, sa), "
                 "or +N days")
    time_help = ("Start time: 10, 2, 2PM, '10 AM' "
                 "(AM/PM optional: 8-11 = AM, 12-7 = PM)")
    court_help = "Tennis court number (2-6 at SCRC, 1-6 at Sycamore) or 'any'. Ignored for pickleball."

    book_p = subparsers.add_parser(
        "book", aliases=["b"],
        help="Wait for the booking window to open and snipe the target slot",
        description="Positional form: book [SPORT] [WHERE] [DATE] [TIME] [COURT], "
                    "e.g. 'book t sat 10 3' or 'book t sy sat 10 3'. WHERE is only for "
                    "tennis and can be left out (SCRC). Anything omitted at the end is "
                    "prompted for.",
    )
    book_p.set_defaults(command="book")
    book_p.add_argument("sport_pos", nargs="?", metavar="SPORT", help=sport_help)
    book_p.add_argument("where_pos", nargs="?", metavar="WHERE", help=where_help)
    book_p.add_argument("date_pos", nargs="?", metavar="DATE", help=date_help)
    book_p.add_argument("time_pos", nargs="?", metavar="TIME", help=time_help)
    book_p.add_argument("court_pos", nargs="?", metavar="COURT", help=court_help)
    book_p.add_argument("--sport", help=f"{sport_help} (prompted if omitted)")
    book_p.add_argument("--where", metavar="WHERE", help=where_help)
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
        help="Go through the booking but stop before clicking BOOK NOW, and save a screenshot",
    )

    list_p = subparsers.add_parser(
        "list", aliases=["l"],
        help="List all time slots on a facility page for a given date",
        description="Positional form: list [SPORT] [WHERE] [DATE], e.g. 'list pb tmr' "
                    "or 'list t sy tmr'.",
    )
    list_p.set_defaults(command="list")
    list_p.add_argument("sport_pos", nargs="?", metavar="SPORT", help=sport_help)
    list_p.add_argument("where_pos", nargs="?", metavar="WHERE", help=where_help)
    list_p.add_argument("date_pos", nargs="?", metavar="DATE", help=date_help)
    list_p.add_argument("--sport", help=f"{sport_help} (prompted if omitted)")
    list_p.add_argument("--where", metavar="WHERE", help=where_help)
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
        description="Positional form: inspect [SPORT] [WHERE] [DATE].",
    )
    inspect_p.add_argument("sport_pos", nargs="?", metavar="SPORT", help=sport_help)
    inspect_p.add_argument("where_pos", nargs="?", metavar="WHERE", help=where_help)
    inspect_p.add_argument("date_pos", nargs="?", metavar="DATE", help=date_help)
    inspect_p.add_argument(
        "--sport", help=f"{sport_help} (prompted if omitted)",
    )
    inspect_p.add_argument("--where", metavar="WHERE", help=where_help)
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
    if args.command is None:
        run_menu()
        return

    # WHERE is optional, so if the word in its spot isn't a place, it's really
    # the date: shift it and the answers after it over by one.
    later = {"book": ["date_pos", "time_pos", "court_pos"],
             "list": ["date_pos"], "inspect": ["date_pos"]}.get(args.command)
    if later and args.where_pos is not None and args.where_pos.strip().lower() not in WHERE_ALIASES:
        if getattr(args, later[-1]) is not None:
            parser.error("too many answers")
        for name, value in zip(later, [args.where_pos] + [getattr(args, n) for n in later[:-1]]):
            setattr(args, name, value)
        args.where_pos = None

    # Fold positional forms into the --flag attributes; giving both is an error.
    for name in ("sport", "where", "date", "time", "court"):
        pos = getattr(args, f"{name}_pos", None)
        if pos is None:
            continue
        if getattr(args, name) is not None:
            parser.error(f"{name} given both positionally ({pos!r}) and as --{name}")
        setattr(args, name, pos)

    # Leaving WHERE out means SCRC. It's only asked for when nothing after it
    # was given either (and inspect never asks).
    if later and args.where is None and (args.date is not None or args.command == "inspect"):
        args.where = "scrc"

    if args.command == "login":
        run(cmd_login(manual=args.manual, headless=not args.headed))
    elif args.command == "status":
        run(cmd_status(headless=not args.headed))
    elif args.command == "cancel":
        run(cmd_cancel(headless=not args.headed))
    elif args.command == "inspect":
        run(cmd_inspect(sport=args.sport, where=args.where, date_str=args.date,
                        headless=not args.headed))
    elif args.command == "list":
        run(cmd_list(
            sport=args.sport,
            where=args.where,
            date_str=args.date,
            headless=not args.headed,
        ))
    elif args.command == "book":
        run(cmd_book(
            sport=args.sport,
            where=args.where,
            date_str=args.date,
            time_str=args.time,
            court_str=args.court,
            headless=not args.headed,
            dry_run=args.dry_run,
        ))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # On Windows a Ctrl+C at a prompt can land just after the prompt
        # already gave up with EOFError, outside the handlers above.
        sys.exit(130)
