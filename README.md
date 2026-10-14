# bookingbot

bookingbot books tennis courts at SCRC (Sunset Canyon Recreation Center) and Sycamore, and pickleball courts at SCRC, on the [UCLA Rec booking site](https://secure.recreation.ucla.edu/booking). Courts open for booking 72 hours ahead, first come, first served, so popular times can go within seconds. You tell bookingbot the sport, day and time you want, and it books the court the moment it opens.

It runs a web browser in the background, logs in with your UCLA account, and clicks the same buttons you would. When it logs in, you approve the Duo push on your phone.

## Quick start

If you already use git and a terminal. Otherwise, follow the [step-by-step setup](#setup) below.

With [uv](https://docs.astral.sh/uv/):

```
git clone https://github.com/nchen35/bookingbot.git
cd bookingbot
uv sync
uv run playwright install chromium
cp .env.example .env          # optional: fill in UCLA_USERNAME and UCLA_PASSWORD
uv run bookingbot.py status   # logs in (approve the Duo push) and lists your bookings
```

With pip instead, use Python 3.10 or newer (ideally in a virtual environment):

```
python -m pip install playwright tzdata
python -m playwright install chromium
python bookingbot.py status
```

Then book with command-line arguments, for example `bookingbot.py book tennis sat 10 any` (see [Command-line arguments](#command-line-arguments)), or run `bookingbot.py` with no arguments for a menu. `bookingbot.py --help` and `bookingbot.py book --help` list every option.

## Setup

You only need to do this once. If you followed the quick start, skip to [Basic usage](#basic-usage).

### 1. Install Python

Install Python 3.10 or newer from [python.org/downloads](https://www.python.org/downloads/). Skip this if you already have it, or if you'll [use uv](#using-uv-instead).

On Windows, if the installer has a checkbox that says "Add python.exe to PATH", check it.

### 2. Download bookingbot

Go to [github.com/nchen35/bookingbot](https://github.com/nchen35/bookingbot), click the green **Code** button, then **Download ZIP**. Unzip it (on Windows, right-click the file and choose **Extract All**). This gives you a folder called `bookingbot-main`. Put it somewhere easy to find, like your Documents folder.

If you have git, you can run `git clone https://github.com/nchen35/bookingbot.git` instead, which gives you a folder called `bookingbot` and lets you update later with `git pull`.

### 3. Open a terminal in the bookingbot folder

A terminal is a window where you type commands.

- **Windows 11:** open the folder in File Explorer, right-click an empty spot, and choose **Open in Terminal**.
- **Windows 10:** open the folder in File Explorer, click the address bar, type `powershell`, and press Enter.
- **Mac:** open the Terminal app, type `cd ` (with a space at the end), drag the folder onto the Terminal window, and press Enter.

Commands in this guide start with `python`. On a Mac, type `python3` instead. On Windows, if `python` doesn't work, try `py`.

Check that Python works:

```
python --version
```

It should print Python 3.10 or higher, for example `Python 3.14.4`.

### 4. Install what bookingbot needs

Run these two commands, one at a time:

```
python -m pip install playwright tzdata
python -m playwright install chromium
```

The first installs the Python libraries bookingbot uses. The second downloads the browser it controls, which can take a few minutes. If pip warns that a folder is not on PATH, or that a newer version of pip is available, you can ignore it.

#### Using uv instead

If you have [uv](https://docs.astral.sh/uv/), you can use it instead of pip. It keeps bookingbot's libraries separate from the rest of your computer, and downloads the right Python if you don't have it, so you can skip step 1:

```
uv sync
uv run playwright install chromium
```

Then type `uv run bookingbot.py` wherever this guide says `python bookingbot.py`.

### 5. Save your UCLA login (optional)

If you skip this step, bookingbot asks for your UCLA Logon ID and password each time it needs to log in, and doesn't save them. Your password won't show up as you type it. That's normal.

To save them instead, create a file called `.env` in the bookingbot folder.

On Windows:

```
copy .env.example .env
notepad .env
```

On a Mac:

```
cp .env.example .env
open -e .env
```

Replace `user` and `pass` with your Logon ID and password, then save and close the file:

```
UCLA_USERNAME=joebruin
UCLA_PASSWORD=your-password
```

The file stays on your computer, but anyone who can open your files can read it.

### 6. Check that it works

```
python bookingbot.py status
```

The first time, it logs in and asks you to approve the Duo push on your phone. Then it lists your upcoming bookings, or says you have none. If you get that far, you're ready.

## Basic usage

Start bookingbot from a terminal in the bookingbot folder (see [setup step 3](#3-open-a-terminal-in-the-bookingbot-folder)):

```
python bookingbot.py
```

This opens a menu. Type a number and press Enter:

```
=== UCLA Rec booking bot ===
  1. Book a court
  2. See what's open
  3. See your bookings
  4. Cancel a booking
  q. Quit
Choose 1-4 or q:
```

When bookingbot finishes, you're back at the menu. To stop it partway through, press Ctrl+C, which also brings you back to the menu. Choose `q` to quit.

You can also skip the menu and type a command directly, like `python bookingbot.py book`. Each section below shows both ways. To skip the questions too, give the answers as [command-line arguments](#command-line-arguments), like `python bookingbot.py book tennis sat 10 any`.

### Book a court

Choose 1 in the menu, or run:

```
python bookingbot.py book
```

bookingbot asks a few questions. Type each answer and press Enter:

```
Sport (t/tennis, pb/pickleball): tennis
Where (scrc/sycamore) [scrc]:
Date (YYYY-MM-DD, today, tmr, sat, +3): saturday
Time (e.g. 10, 2, 2PM): 10am
Court (2-6 or 'any') [any]:
```

The Where and Court questions only come up for tennis. For Where, press Enter for SCRC or type `sycamore`. For Court, press Enter to take any open court.

Slots open 72 hours before they start, so a Saturday 10 AM court opens on Wednesday at 10 AM. You can start `book` any time before then, even days ahead.

- If the slot is already open, bookingbot books it right away.
- If not, it shows when the slot opens and waits, printing a line every 2 minutes. Leave the window open.

When it's done, it prints `Booking complete!` and saves a screenshot in the `screenshots` folder. To make sure the booking went through, look for UCLA's confirmation email (it usually arrives within a minute) or check your bookings (menu option 3).

UCLA allows one booking per day at each facility: SCRC tennis, SCRC pickleball and Sycamore tennis each count separately. If you already have a booking at that facility that day, bookingbot shows it and asks whether to keep going, since the new booking will fail unless you cancel the old one first.

While it waits:

- Keep your computer awake. If it goes to sleep (for example, when you close a laptop's lid), the booking can fail, so turn off sleep in your power settings. On a Mac, you can also start the command with `caffeinate -i`, as in `caffeinate -i python3 bookingbot.py book`, to stop the Mac from going to sleep when idle.
- It's fine if the internet drops for a while (say you switch Wi-Fi networks). bookingbot keeps retrying every 30 seconds, but it needs to be back online by the time the slot opens.
- Keep your phone nearby. If UCLA logs bookingbot out during a long wait, it logs back in and you need to approve another Duo push.

### See what's open

Choose 2 in the menu, or run:

```
python bookingbot.py list
```

It asks for the sport (and where, for tennis) and the date, then shows every time slot that day:

```
=== SCRC Tennis Courts | Fri, Oct 16 2026 ===
  Available:
    9:00 - 9:50 AM  (3 courts: SCRC - 2, SCRC - 4, SCRC - 5)
    10:00 - 10:50 AM  (1 court: SCRC - 3)
  Full:
    11:00 - 11:50 AM
    12:00 - 12:50 PM
  Not yet open:
    7:00 - 7:50 PM  (5 courts: SCRC - 2, SCRC - 3, SCRC - 4, SCRC - 5, SCRC - 6)
```

Pickleball looks the same, but shows how many courts are open instead of their names. Times you've booked show at the top under "Your booking".

`list` only works for today and the next 3 days, because that's all the UCLA site shows.

### See your bookings

Choose 3 in the menu, or run:

```
python bookingbot.py status
```

```
=== Your upcoming bookings (2) ===
  SCRC Tennis Courts (SCRC - 6)  |  Fri, Oct 16 2026  |  3:00 - 3:50 PM
  SCRC Pickleball Courts  |  Sat, Oct 17 2026  |  10:00 - 10:50 AM
```

### Cancel a booking

Choose 4 in the menu, or run:

```
python bookingbot.py cancel
```

It lists your upcoming bookings with a number next to each. Type the number of the one to cancel, or `q` to quit. It cancels right away without asking again, so check the number first. Afterward it checks your bookings to make sure that one is gone.

## Additional features

### Command-line arguments

Instead of using the menu and answering questions, you can give the command and the answers as command-line arguments, all on one line, in this order:

```
python bookingbot.py book SPORT WHERE DATE TIME COURT
python bookingbot.py list SPORT WHERE DATE
```

WHERE only matters for tennis, and you can leave it out to mean SCRC. If you leave anything off the end, bookingbot asks for it. Short forms work too:

| Command | What it does |
|---|---|
| `python bookingbot.py book tennis saturday 10 3` | Tennis at SCRC, next Saturday, 10 AM, court 3 |
| `python bookingbot.py b t sy sat 10 3` | Tennis at Sycamore, next Saturday, 10 AM, court 3 |
| `python bookingbot.py b t sat 10 any` | Tennis at SCRC, next Saturday, 10 AM, any court |
| `python bookingbot.py b pb tmr 2` | Pickleball, tomorrow, 2 PM |
| `python bookingbot.py l t sy +2` | List Sycamore tennis slots 2 days from now |
| `python bookingbot.py b t` | Tennis, then asks where, the date, the time and the court |

| To give | Type |
|---|---|
| The command | `b` (book), `l` (list), `s` (status), `c` (cancel) |
| Tennis | `t`, `ten`, `tennis` |
| Pickleball | `p`, `pb`, `pickle`, `pickleball` |
| SCRC (for tennis) | `scrc`, `sc`, `sunset` |
| Sycamore (for tennis) | `sycamore`, `syc`, `sy` |
| Today or tomorrow | `today`, `tomorrow`, `tmr`, `tmrw`, `tom` |
| A day of the week | `saturday`, `sat`, `sa` (and the same for other days) |
| A number of days from today | `+3` or `3` |
| An exact date | `2026-10-17` |
| A time | `10`, `2`, `10am`, `2pm` |
| A tennis court | `2` to `6` at SCRC, `1` to `6` at Sycamore, or `any` |

A few details:

- A day name means the next one, so on a Saturday, `sat` means a week from today.
- A plain number for the date counts days from today. `3` means 3 days from now, not the 3rd of the month.
- If you leave off AM or PM, bookingbot guesses: 8 to 11 are AM, and 12 and 1 to 7 are PM. Courts can be booked from 8 AM to 8 PM, so only one of the two makes sense.
- If a time has a space in it, put quotes around it (`"10 AM"`) or leave the space out (`10am`).
- Dates and times are always Pacific time, whatever time zone your computer is set to.

You can also label each answer, in any order, with `--sport`, `--where`, `--date`, `--time` and `--court`. For example: `python bookingbot.py book --sport tennis --where sycamore --date sat --time 10 --court any`. Giving the same answer both ways, like `b t --sport pb`, is an error.

To see every option for a command, add `--help`, as in `python bookingbot.py book --help`.

### Choose a tennis court

The SCRC tennis courts are numbered 2 to 6, and the Sycamore courts 1 to 6. When you book tennis, pick a court number or `any`. The number means a court at the place you picked, so court 3 at Sycamore is SYCA - 3.

- With a number, bookingbot only tries that court. If someone else gets it first, the booking fails. It won't switch to a different court.
- With `any`, it books the first open court it finds.

Pickleball doesn't have a court choice.

### Book more than one court

Each `book` runs on its own with its own browser, so you can go after more than one slot by starting each one in a separate terminal window. If they both need to log in, you'll get a Duo push for each. Remember that UCLA allows one booking per day at each facility.

### Watch the browser

bookingbot normally keeps its browser hidden. Add `--headed` to any command to see the browser and watch what it does, for example `python bookingbot.py book --headed`. This is useful the first time you book, or when something isn't working.

### Log in ahead of time

```
python bookingbot.py login
```

This logs in and saves your session. You usually don't need it, since every command logs in by itself when it has to. It has two options:

- `--headed` shows the login browser.
- `--manual` opens a browser window for you to log in yourself, with 5 minutes to finish. Use this if automatic login keeps failing.

## Troubleshooting

- **Windows says `python` is not recognized, or "Python was not found".** Try `py` instead of `python`. If that doesn't work either, reinstall Python from python.org and check "Add python.exe to PATH".
- **Mac says `command not found: python`.** Use `python3`.
- **`No module named 'playwright'`.** Run [setup step 4](#4-install-what-bookingbot-needs) again. Use the same command (`python`, `python3` or `py`) for installing and for running bookingbot.
- **`Executable doesn't exist`, or a message telling you to run `playwright install`.** Run `python -m playwright install chromium`.
- **`externally-managed-environment` while installing.** Your Python came from Homebrew or a Linux package manager. Install Python from python.org and use that, or [use uv](#using-uv-instead).
- **Login fails, or the Duo push never shows up.** Run `python bookingbot.py login --headed` to watch what happens, or `python bookingbot.py login --manual` to log in yourself.
- **`Expected the UCLA login page but got ...`.** The Wi-Fi network you're on wants you to sign in first, which is common at hotels, airports and cafes. Open any website in your normal browser, finish that sign-in, and try again. bookingbot only types your UCLA password into UCLA's own login page.
- **`Timed out after ... seconds waiting for the UCLA site` or `Lost the connection to the UCLA site`.** Usually your internet connection is slow or dropped. Try again. If it keeps happening on a good connection, UCLA may have changed its site (see [inspect](#inspect)).
- **Errors on every command, even with a good connection.** Delete `session.json` from the bookingbot folder. The next command will log in from scratch.
- **A booking failed.** Look in the `screenshots` folder. bookingbot saves a screenshot whenever something goes wrong.

## Notes

Details most people won't need.

### When slots open

The UCLA site shows 4 days at a time: today and the next 3 days. A new day appears at midnight Pacific time, 3 days ahead. On Tuesday, for example, you see Tuesday through Friday, and Saturday appears Wednesday at 12:00 AM.

Each slot opens exactly 72 hours before it starts, so the newest day usually has slots that are "Not yet open". On Wednesday morning, Saturday 9 AM stays closed until Wednesday 9 AM. Today's list may be short late in the day, because times that have passed are hidden.

`list` only accepts those 4 days. For a later date, it tells you when the date will appear and asks again.

`book` accepts any date from today on. If the date isn't on the site yet, `book` waits until the midnight when it appears, checks that it's really there, and then waits for the slot to open. If the date still isn't there after 5 minutes of retrying, it stops and saves a `tab_missing_...png` screenshot, so you find out hours before the slot opens instead of at the last second. A slot that has already started is rejected.

### How book times the click

1. It logs in, then reloads the page every 2 minutes to stay logged in. If the internet drops, it retries every 30 seconds, right up until the slot opens.
2. 35 seconds before the slot opens, it opens the court page and selects your date.
3. At the moment the slot opens, it starts asking the site for your date's slots, about every 0.1 seconds, without changing what the page shows. For tennis with `any` court, it asks about one court at a time, taking turns. With a court number, it only asks about that court. If the slot was already open when you started, it skips straight to this step.
4. As soon as a court shows BOOK NOW for your time, it switches the page to that court and clicks BOOK NOW there.
5. It stops and saves a `failed_...png` screenshot if your time is taken on every court it's checking, or if it still isn't open 20 seconds after opening time.
6. After clicking BOOK NOW, it saves a `booking_confirmation_...png` screenshot of your bookings page.

Expect the click about 2 to 3 seconds after opening time. See [Why bookings land a couple of seconds after the hour](#why-bookings-land-a-couple-of-seconds-after-the-hour).

### Why bookings land a couple of seconds after the hour

The UCLA site opens each slot about 2 seconds after the hour, not right on it. In a test on October 14, 2026, a 2:00 PM slot still said "Opens at 2:00 PM" at about 2:00:02, and showed BOOK NOW from about 2:00:02.4 on. That test used a computer clock within about 0.15 seconds of internet time. Two things add up to the delay:

- The site's servers run about 1 second slow, and they don't all agree with each other.
- Even by the servers' own clocks, the slot opens roughly a second after the hour.

The court page never updates its list of times on its own. Anyone who loads it right at the hour, by hand or with bookingbot, sees "Opens at" and has to load it again. bookingbot keeps checking about every 0.1 seconds, so it clicks within a few tenths of a second of the slot really opening.

The delay may change if UCLA fixes its clocks. If a slot ever takes more than 20 seconds to open, raise `SNIPE_WINDOW_SECS` in `bookingbot.py`.

### Staying logged in

bookingbot saves your login in `session.json` in the bookingbot folder, so commands you run one after another don't each need a new login. Don't share this file, since it lets anyone use your logged-in session.

UCLA logs you out after about 5 minutes of inactivity. The next command after that logs in again and sends another Duo push. If your UCLA single sign-on is still active, it may get back in without your password or Duo.

While `book` waits, it reloads the page every 2 minutes so it doesn't get logged out. If it gets logged out anyway, it logs in again and sends a Duo push. You have 3 minutes to approve it, or the booking stops.

When a command needs to log in, the login browser is hidden unless you added `--headed`.

Instead of a `.env` file, you can set `UCLA_USERNAME` and `UCLA_PASSWORD` as environment variables. They take priority over `.env`.

### More about list

- If you already have a booking there that day, `list` says so. It still shows what's open, which is handy for telling a friend.
- Tennis times are combined across all the courts. A time shows as Available if any court is open, with the open courts listed.
- A time you've booked shows under "Your booking", even if other courts are still open at that time.
- A "Not yet open" tennis time can list fewer than all the courts. The missing courts are already blocked off (for example, for a lesson): the site either shows them with 0 spots or leaves that time off for them entirely.

### Screenshots

bookingbot saves screenshots in the `screenshots` folder: one after each booking and cancellation, and one whenever something fails. The booking screenshot shows the whole page. The others only show what fits in the browser window.

### Settings in bookingbot.py

If UCLA changes how booking works, you may need to edit these values in `bookingbot.py`:

| Setting | Value | What it is |
|---|---|---|
| `BOOKING_ADVANCE_HOURS` | `72` | How many hours before a slot starts it opens for booking |
| `SNIPE_WINDOW_SECS` | `20` | How long after opening time `book` keeps checking while your time still says "Opens at" |
| `VISIBLE_DATE_TABS` | `4` | How many days the site shows at once (today plus 3) |
| `FIRST_AM_HOUR` | `8` | The earliest court hour, used to guess AM or PM |
| `COURTS` | SCRC 2 to 6, Sycamore 1 to 6 | The tennis court numbers at each place |
| `FACILITY_CARD_NAMES` | `SCRC Pickleball Courts`, `SCRC Tennis Courts`, `Sycamore Tennis Courts` | The names of the facility cards on the booking page |

### inspect

`inspect` is a debugging tool for when UCLA changes its website and bookingbot stops finding things. It shows how the court page is built:

```
python bookingbot.py inspect tennis
python bookingbot.py inspect tennis sycamore
python bookingbot.py inspect pickleball saturday
```

It prints the page's tabs, BOOK NOW buttons and time slots, and saves a full-page screenshot and the page's HTML in the `screenshots` folder. The date is optional (without one, you get today), and like `list`, it only accepts the 4 days the site shows.
