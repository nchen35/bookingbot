# bookingbot — Usage Guide

A command-line sniper for UCLA Rec court reservations. Uses Playwright to drive a real browser, so it logs in the same way you would and clicks the same buttons you would — just at exactly the right moment.

---

## How it works at a high level

- Individual slots become bookable **72 hours before their start time**, and the target date's tab is visible roughly 3 calendar days out (see [`book --date`](#book---date-must-land-within-the-visible-booking-window)). The site is first-come-first-served, so seconds matter for popular slots.
- You log in (`login`). The browser session is saved to `session.json` next to the script.
- For a contested slot, you run `book ...` ahead of time. It sits on the facility page, keeps the session alive, and fires the click at the precise moment the booking window opens.
- For uncontested slots you can also run `book` — it'll notice the window is already open and book immediately.

---

## One-time setup

```bash
uv sync                          # installs playwright + tzdata
uv run playwright install chromium
uv run bookingbot.py login       # opens a browser, complete UCLA login + Duo
```

After `login`, a `session.json` file is written next to the script. Keep that file — it's your saved login and no other command works without it.

---

## Session lifetime

**The session expires after roughly 5 minutes of inactivity.** This is a server-side timeout — there's nothing the script can do to extend it beyond what the site allows.

What this means in practice:

- **Back-to-back commands work fine.** `login` → `status` → `list` → `book` in quick succession all reuse the same session.
- **Long gaps require re-login.** If you `login`, go away for an hour, then `status`, the session will be dead. Just run `login` again.
- **`book` keeps itself alive.** The `book` command reloads the booking page every ~2 minutes while waiting for the window to open. A `book` you start 30 minutes (or even hours) before the target time will survive because it never goes idle.
- **Scheduled / unattended runs:** If you want to snipe a slot at a specific time without babysitting, start `book` shortly after logging in. It will wait — potentially for hours — and fire at the right moment. Don't schedule a task to run `book` hours after your last login, because the session will have expired by then.

**Typical workflow:**

1. `login` — complete SSO + Duo
2. Immediately run `book --sport tennis --date saturday --time 10AM`
3. Walk away. The script keeps the session alive and fires when the window opens.

If you want to check things between login and booking, keep your commands close together:

1. `login`
2. `status` — check what you have
3. `list --sport pickleball --date saturday` — see what's open
4. `book --sport pickleball --date saturday --time 10AM` — go

---

## Commands

### `login`

Opens a visible Chromium window, navigates to the booking site, and waits for you to complete the full UCLA SSO + Duo flow in the browser. It watches the URL and the Sign-In button, and the moment it detects you're logged in on the booking page it saves the session automatically — you don't need to press any keys.

```bash
uv run bookingbot.py login
```

- No flags.
- Timeout: 5 minutes. Close the window or let it time out to abort.
- Run again any time the session expires.

### `status`

Lists your upcoming bookings. Fastest way to check "what do I have on the calendar". Cheap and read-only.

```bash
uv run bookingbot.py status
```

- `--headed` — opens a visible browser.

Sample output:

```
=== Your upcoming bookings (2) ===
  SCRC Tennis Courts (SCRC - 2)  |  Thu, Apr 16 2026  |  12:00 - 12:50 PM
  SCRC Pickleball Courts  |  Sat, Apr 18 2026  |  10:00 - 10:50 AM
```

### `list`

Shows every time slot on the facility page for a given sport and date, partitioned into **Your bookings**, **Available**, **Full**, and **Not yet open**. This is the command you run when you want to answer "is 2PM pickleball open on Saturday?"

```bash
uv run bookingbot.py list                                   # fully interactive
uv run bookingbot.py list --sport tennis --date saturday
uv run bookingbot.py list --sport pickleball --date +3
```

- `--sport pickleball|tennis` — which facility to check. Prompted if omitted.
- `--date DATE` — which day to check. Prompted if omitted. See [date shortcuts](#date-shortcuts) below.
- `--headed` — watch the automation in a visible browser.

**Note on available vs bookable:** If you already have a booking for the given sport on the given day, the site won't let you book a second one. The `list` command still shows which courts have spots (so you can, say, tell a friend), but prints a note explaining why you can't book.

**Pickleball output** — 4 courts per time slot:

```
=== SCRC Pickleball Courts — Sat, Apr 18 2026 ===
  (You already have a booking for pickleball on this day — new bookings will be rejected.)

  Your bookings:
    2 - 2:50 PM
  Available:
    10 - 10:50 AM  (4 courts)
    11 - 11:50 AM  (2 courts)
  Full:
    12 - 12:50 PM
    1 - 1:50 PM
  Not yet open:
    3 - 3:50 PM
```

**Tennis output** — five courts (SCRC - 2 through SCRC - 6) aggregated by time:

```
=== SCRC Tennis Courts — Sat, Apr 18 2026 ===
  Available:
    10 - 10:50 AM  (3 courts: SCRC - 2, SCRC - 4, SCRC - 5)
    11 - 11:50 AM  (1 court: SCRC - 3)
  Your bookings:
    12 - 12:50 PM  (SCRC - 2)
  Full:
    1 - 1:50 PM
    2 - 2:50 PM
  Not yet open:
    3 - 3:50 PM
```

Times where you have a booking always show under **Your bookings**, even if other courts at the same time are still open (the assumption being: you already have a spot, you don't need another).

### `book`

The main event: snipes a booking at the exact moment the window opens.

```bash
uv run bookingbot.py book                                    # fully interactive
uv run bookingbot.py book --sport tennis --date saturday --time 10AM
uv run bookingbot.py book --sport tennis --date saturday --time 10AM --court 3
uv run bookingbot.py book --sport pickleball --date +3 --time 2PM --headed
uv run bookingbot.py book --sport tennis --date tomorrow --time "10:30 AM" --dry-run
```

- `--sport pickleball|tennis`
- `--date DATE` — see [date shortcuts](#date-shortcuts)
- `--time TIME` — see [time shortcuts](#time-shortcuts)
- `--court 2|3|4|5|6|any` — **tennis only.** Picks a specific SCRC court (2 through 6), or `any` to take whichever opens first. Prompted interactively for tennis if omitted; ignored for pickleball. ⚠️ Currently broken after the recent UCLA site update — see [Known issues](#known-issues--todos).
- `--headed` — run the browser visible so you can watch the snipe unfold. Default is headless.
- `--dry-run` — do everything short of actually clicking BOOK NOW. Takes a screenshot of the target slot and exits. Use this the first time you try a new slot to confirm navigation works.

How it actually fires:
1. Opens the booking page and validates the saved session.
2. Computes the target open time (target slot minus 72 hours).
3. Keeps the session alive (reloading every ~2 minutes) until 35 seconds before open time.
4. Navigates to the facility page.
5. Pre-positions on the day *before* the target date so the target date tab isn't yet loaded. (If the day-before tab isn't visible — e.g. the window is already open — it skips pre-positioning.)
6. Holds until 1 second before open time, then spins in a tight loop until the exact moment.
7. Clicks the target date tab via direct JS (bypassing Playwright's actionability waits).
8. Polls every ~20ms for the BOOK NOW button of the target time and clicks it in JS the instant it appears.
9. For tennis, if `--court` was set to a specific number, it targets that court first; if `any` (or no preference), it cycles through court tabs until one yields a button.
10. On failure, retries up to 3 times with a fresh facility navigation between attempts.
11. On success, saves a confirmation screenshot to `screenshots/`.

Normal snipe performance on a fast network: target button clicked within ~50–200 ms of the window opening.

### `cancel`

Lists your upcoming bookings and lets you pick one to cancel.

```bash
uv run bookingbot.py cancel
uv run bookingbot.py cancel --headed
```

Interactive: prints a numbered list, prompts for a number (or `q` to quit), walks the cancel-booking modal, takes a confirmation screenshot.

- `--headed` — watch the cancel happen.

### `inspect`

A developer / debugging command: navigates to a facility page and dumps its DOM structure (tabs, BOOK NOW buttons, slot rows) plus a full-page screenshot and the raw HTML. Use this when UCLA changes their site layout and one of the other commands stops finding elements.

```bash
uv run bookingbot.py inspect                                 # prompted for sport
uv run bookingbot.py inspect --sport tennis
uv run bookingbot.py inspect --sport pickleball --date saturday --headed
```

- `--sport pickleball|tennis` — prompted if omitted.
- `--date DATE` — optional. If given, clicks that date tab before dumping. If omitted, inspects whatever date tab the site defaults to (today). Does **not** prompt.
- `--headed` — run visible.

Output is printed to the terminal and also written to `screenshots/inspect_<sport>_<timestamp>.{png,html}`.

---

## Date shortcuts

`book --date`, `list --date`, and `inspect --date` all accept:

| Input | Meaning |
|---|---|
| `2026-04-18` | ISO date |
| `today` | today |
| `tomorrow` | tomorrow |
| `monday`, `mon`, `tuesday`, `tue`, ... | next occurrence of that weekday (if today is that weekday, means *next week*) |
| `+3` | 3 days from today |
| `3` | same as `+3` — bare numbers are treated as day offsets |

All dates are interpreted in **America/Los_Angeles** time, not your local clock.

## Time shortcuts

`book --time` accepts any of:

| Input | Meaning |
|---|---|
| `10AM`, `10am` | 10:00 AM |
| `2PM` | 2:00 PM |
| `10:30 AM` | 10:30 AM (quote it in the shell: `--time "10:30 AM"`) |
| `10:30AM` | same, no space needed |

Times match a slot when the start hour/minute and AM/PM match. For example, `10AM` matches a slot labeled `10 - 10:30 AM` or `10 - 10:50 AM`.

---

## Flags — quick reference

| Command | `--sport` | `--date` | `--time` | `--court` | `--headed` | `--dry-run` |
|---|---|---|---|---|---|---|
| `login`   |        |        |        |        |        |        |
| `status`  |        |        |        |        | yes    |        |
| `list`    | yes    | yes    |        |        | yes    |        |
| `book`    | yes    | yes    | yes    | tennis only | yes | yes |
| `cancel`  |        |        |        |        | yes    |        |
| `inspect` | yes    | yes (no prompt) |   |        | yes    |        |

Commands that accept `--sport` or `--date` will prompt you interactively if you omit them, **except** `inspect --date` which simply uses the site's default date tab when omitted (no prompt).

---

## Quirks and edge cases

### `login` is always headed

The other commands accept `--headed` (default is headless); `login` has no such flag — it always opens a visible browser, because you need to complete the UCLA SSO and Duo prompt by hand. You can't script `login` in a fully headless pipeline.

### Bare numbers in `--date` mean days-from-today

`--date 3` and `--date +3` both mean "three days from today". If you expect `3` to mean "the 3rd of this month", you'll be surprised. The `+` prefix is the safer, more intentional form.

### `--time` with a space needs shell quoting

`--time 10AM` works. `--time 10:30 AM` does not — your shell splits it into two arguments and argparse sees `10:30` and chokes. Either drop the space (`--time 10:30AM`) or quote it (`--time "10:30 AM"`). Any example in the help text that uses the spaced form assumes you've quoted it.

### `list` aggregates tennis courts by time

Tennis has 5 sub-courts (SCRC - 2 through SCRC - 6). `list` shows availability aggregated by time slot (with a "(N courts: SCRC - 2, SCRC - 3, ...)" annotation), not court-by-court. If you want to commit to a specific court at book time, use `book --court 3` (see [`book`](#book)). `list` itself has no `--court` flag.

### `book --date` must land within the visible booking window

The site shows roughly 3 days of calendar dates as tabs — the exact cutoff is by calendar day, not a strict rolling 72 hours. For example, on Sunday just after midnight Pacific time the tabs already include the full Wednesday. If the target date tab isn't visible, the script will fail with a "could not find date tab" error. To snipe the opening moment, pass the date of the slot you want (e.g. `--date saturday`) and run the command *before* that date's tab becomes visible; the script will wait.

### The "pre-positioning on the day-before tab" step can silently fall through

The snipe strategy pre-positions on the tab for the day *before* the target date, so that clicking the target date tab at T-0 is a genuine navigation (not a no-op). If the day-before tab isn't visible when pre-position runs (e.g. because the booking window is already open, or because the site's date range drifts mid-wait), the pre-position step fails quietly and the script proceeds anyway. This is almost always fine but can make the first click at T-0 slower than ideal.

### `session.json` lives next to the script

The session file is hard-coded to the script's directory. If two people share the same `bookingbot.py` checkout on one machine, they'll overwrite each other's sessions. Not a concern for single-user setups.

### `BOOKING_ADVANCE_HOURS = 72` is a hard-coded constant

If UCLA ever changes the booking-advance window to something other than 72 hours, `book` will fire at the wrong time. The value is a Python constant near the top of the file — trivial to edit, but not configurable via CLI.

### One booking per sport per day

The UCLA Rec system only allows one reservation per sport per day. If you already have a pickleball booking for Saturday, you cannot book another pickleball slot on Saturday — the BOOK NOW buttons will be disabled. The `list` command still shows spot availability so you can relay the info to friends; it just adds a note that you can't book.

---

## Known issues / TODOs

### Tennis `--court` selection is currently broken

The UCLA Rec site got a UI refresh; the row/button selectors were updated but the **court-targeting logic in `book` has not yet been re-verified against the new tab layout**. Using `--court 3` (or any specific number) may not actually land on that court. Workaround: use `--court any` (or skip the flag) and let `book` cycle through tabs until it finds a free slot.

### Screenshots can be cut off

`screenshots/*.png` are taken with Playwright's default viewport, not full-page mode. If the page is long (lots of time slots, or your bookings list is long), the part you want may be below the fold. The accompanying `.html` dump (from `inspect`) contains the full DOM, so use that when a screenshot is incomplete.

### `status` and screenshots only show the first 3 upcoming bookings

After the site update, the home page collapses the "Upcoming" list to the first ~3 entries with a **"See more"** button to expand the rest. The bot currently scrapes the collapsed list, so the 4th-and-beyond bookings are invisible to `status` and won't appear in `cancel`'s numbered list or in confirmation screenshots. If you have more than 3 active bookings, manage the older ones in the browser.

### `uv` is what's documented, but any Python env works

Everything in this guide uses `uv` (commands like `uv run bookingbot.py ...`). If you'd rather use plain `python` + `pip` + a venv, that works too — the script is a single `bookingbot.py` with a normal `pyproject.toml`. Just install `playwright` and `tzdata`, run `playwright install chromium`, and invoke `python bookingbot.py ...` directly. `uv` is just the smoothest path on a fresh machine.

---

## Tips

- **Always `--dry-run` a new target first.** The navigation, date-tab click, and slot-matching logic can break subtly when the site changes. A dry run takes a screenshot of the target slot and proves end-to-end navigation before you rely on the snipe.
- **Start `book` right after `login`.** Since the session expires in ~5 minutes of inactivity, the safest pattern is `login` → `book` immediately. The book command handles the wait internally.
- **Use `--headed` the first few times.** Watching the snipe happen once or twice makes the failure modes much easier to reason about.
- **After a failed snipe, check `screenshots/failed_*.png`.** The script writes a screenshot on every failure path, which almost always tells you immediately whether the issue was the facility nav, the date tab, or the BOOK NOW button.
- **Re-run `login` at the first sign of trouble.** Many "can't find" errors are actually silent session expiry — the page redirects to the SSO form, and every selector on the booking page is suddenly gone.
