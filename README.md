# ana2ics

*Schedule Yourself.*

Turns a screenshot of an ANA cabin attendant work schedule (the monthly
"CABIN ATTENDANT WORK SCHEDULE" page) into an `.ics` file to import into a
calendar.

```
uv run ana2ics.py schedule.png
```

prints the month for checking, then writes `2025-02.ics` next to the screenshot
(example output from the made-up month in the tests):

```
02/01       09:00-10:10   NH 21 ITM-HND  (Aircraft 789, report 0800)
02/01       11:00-12:15   NH 22 HND-ITM  (Aircraft 789, off 1245)
02/02       14:00-16:30   NH 975 KIX-PVG  (Aircraft 32E, report 1300)
02/02       17:30-19:45   NH 976 PVG-KIX  (Aircraft 32E, off 2015)
02/03       09:00-17:00   Duty GRT / WA10
02/04-02/06 all day     OFF/HOL (OSA)
```

## How it works

1. **Claude transcribes the table, and only that.** Every row and column exactly
   as printed, as structured JSON, saved next to the screenshot. No dates,
   time zones or calendar logic are left to the model.
2. **Python does everything that has to be exact:**
   - one event per flight, in Japan time;
   - `1720 L` means local time at that airport (e.g. Shanghai, UTC+8) and is
     converted;
   - a leg that lands after midnight ends the next day;
   - ground duties on the same day (`GRT`, `WA10`, `G1`, ...) become one
     `Duty GRT / WA10` event;
   - consecutive days off become one all-day `OFF (OSA)` event, or
     `OFF/HOL (OSA)` if it includes a holiday.
3. **Checks before writing:** every day of the month present, every flight with a
   route and readable times, legs between 20 minutes and 14 hours, and no local
   time at an airport whose time zone isn't known. Any problem stops the run.
   Fix the `.json` by hand and re-run with `--from-json` (no API call), or use
   `--force`.

The printed table is the review step: compare it with the screenshot before
importing. The model reads 43 rows without blinking; it can still blink.

## Importing into Google Calendar

The Google Calendar phone app can't import an `.ics`, and tapping one on an
iPhone hands it to Apple Calendar instead, which is how a month of flights ends
up in the wrong calendar. Import it on the web: Settings › **Import & export** ›
Import, choose the `.ics`, pick the calendar it belongs in, Import.

Import each month once. Importing adds and never replaces: a month imported
twice, or on top of events made some other way, shows up twice.

To paste the `.ics` somewhere instead of saving a file, send it to stdout (the
review table moves to stderr):

```
uv run ana2ics.py schedule.png -o - | pbcopy
```

## Setup

- An Anthropic API key in `ANTHROPIC_API_KEY` or `~/.config/ana2ics/api_key`.
- [uv](https://docs.astral.sh/uv/) installs the dependencies (`anthropic`,
  `pillow`) on first run.
- Tests (no API calls, made-up data): `uv run --with pillow --with anthropic python -m unittest`

## Unknown codes

`CP`, `GRF` (the unlabeled column after JOB) and the letters after the weekday
are copied into the description as printed, not interpreted. A local (`L`)
time at an airport missing from `LOCAL_TZ` in the script stops the run: add the
airport and its time zone there.

## Privacy

Screenshots, transcriptions (`.json`) and `.ics` files hold someone's work
schedule. They're git-ignored; keep them out of the repo.
