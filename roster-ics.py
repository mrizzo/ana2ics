#!/opt/homebrew/bin/python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["anthropic>=1.11", "pillow"]
# ///
"""Turn a screenshot of an ANA cabin attendant work schedule into an .ics file.

Claude only transcribes the table, column by column, exactly as printed. Everything
that has to be right every time (dates, time zones, overnight flights, grouping days
off, the .ics itself) is plain Python, and the month is printed as a table to check
against the screenshot before you import it.

  flight        -> one event per leg:  "NH 34 ITM-HND", in Japan time
  "1720 L"      -> local time at that airport (e.g. Shanghai), converted
  OFF1 / HOL1   -> consecutive days off become one all-day "OFF (OSA)" event
  G1, GRT, ...  -> ground duties on the same day become one "Duty GRT / WA10" event

Checks before writing: every day of the month is present, every flight has a route
and times, legs run forward in time and last between 20 minutes and 14 hours.
Problems stop the run (--force writes anyway).

Usage:
  roster-ics.py SCREENSHOT.png [-o OUT.ics]     # transcribe with Claude, then convert
  roster-ics.py --from-json SCREENSHOT.json     # convert a saved transcription again

The transcription is saved next to the screenshot as .json, so re-running the
conversion costs nothing. The API key comes from ANTHROPIC_API_KEY or
~/.config/roster-ics/api_key.

Importing replaces nothing: delete the month's old events first. The events carry
stable UIDs, so importing the same month twice updates rather than duplicates.
"""
import argparse
import base64
import calendar
import datetime as dt
import io
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
API_KEY_FILE = Path("~/.config/roster-ics/api_key").expanduser()
MODEL = "claude-opus-5-5"

# Airports whose "L" (local) times aren't Japan time. Japanese airports need no entry.
LOCAL_TZ = {
    "PVG": "Asia/Shanghai", "SHA": "Asia/Shanghai", "PEK": "Asia/Shanghai", "PKX": "Asia/Shanghai",
    "CAN": "Asia/Shanghai", "DLC": "Asia/Shanghai", "TAO": "Asia/Shanghai", "SZX": "Asia/Shanghai",
    "HKG": "Asia/Hong_Kong", "TPE": "Asia/Taipei", "TSA": "Asia/Taipei", "ICN": "Asia/Seoul",
    "GMP": "Asia/Seoul", "MNL": "Asia/Manila", "BKK": "Asia/Bangkok", "SGN": "Asia/Ho_Chi_Minh",
    "HAN": "Asia/Bangkok", "SIN": "Asia/Singapore", "KUL": "Asia/Kuala_Lumpur", "CGK": "Asia/Jakarta",
    "HNL": "Pacific/Honolulu",
}
JAPAN = {
    "ITM", "KIX", "UKB", "HND", "NRT", "CTS", "OKA", "SDJ", "FUK", "NGO", "HKD", "KMQ", "HIJ",
    "KOJ", "KMJ", "OIT", "MYJ", "TAK", "KCZ", "TOY", "AOJ", "AXT", "AKJ", "MMB", "KUH", "OBO",
    "ISG", "MMY", "NGS", "KMI", "UBJ", "TKS", "IZO", "YGJ", "OKJ", "FSZ", "NTQ", "HSG", "SHM",
}
FLIGHT_RE = re.compile(r"^([A-Z]{2})\s*(\d{1,4})$")
OFF_RE = re.compile(r"^(OFF|HOL)\d*$")
TIME_RE = re.compile(r"^(\d{2})(\d{2})\s*(L?)$")

PROMPT = """This is a screenshot of an airline cabin attendant's monthly work schedule: a \
fixed-width table with the columns DATE, ON, JOB, (an unlabeled position column), SHP, \
CONF, FROM, TO, TIME (two times), DAY, OFF, STY.

Transcribe it. Return the month exactly as printed in the header (e.g. "2026/10") and \
every row of the table, top to bottom, one entry per printed row, including rows that \
continue the day above (their DATE is blank) and days off. Copy each cell exactly as \
printed: keep spacing inside a cell like "NH 34" or "NH1273", keep an "L" printed \
right after a time as part of that time ("1720 L"), and use "" for an empty cell. \
Don't interpret, correct, convert or skip anything. Read flight numbers and times \
digit by digit: a wrong digit puts someone on the wrong flight."""


def schema():
    from pydantic import BaseModel, Field

    class Row(BaseModel):
        date: str = Field(description='DATE as printed, e.g. "10/01"; "" on a continuation row')
        weekday: str = Field(description='the letters after the date, e.g. "TH A", "FR a", "SA"')
        on: str = Field(description="ON (report time)")
        job: str = Field(description='JOB, e.g. "NH 34", "NH1273", "OFF1", "HOL1", "G1", "GRT"')
        pos: str = Field(description='the column between JOB and SHP, e.g. "CP", "GRF"')
        shp: str
        conf: str
        dep_airport: str = Field(description="FROM")
        arr_airport: str = Field(description="TO")
        start: str = Field(description='first TIME, with a trailing "L" if printed')
        end: str = Field(description='second TIME, with a trailing "L" if printed')
        off: str = Field(description="OFF")
        sty: str = Field(description="STY")

    class Roster(BaseModel):
        month: str = Field(description='as printed in the header, e.g. "2026/10"')
        rows: list[Row]

    return Roster


def load_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key and API_KEY_FILE.exists():
        key = API_KEY_FILE.read_text().splitlines()[0].strip()
    if not key:
        sys.exit(f"No API key: set ANTHROPIC_API_KEY or put it in {API_KEY_FILE}")
    return key


def screenshot_png(path):
    """The screenshot as PNG bytes, with the empty space below the table cut off, so
    the table keeps as much resolution as possible within the API's image size."""
    from PIL import Image, ImageOps

    im = Image.open(path).convert("RGB")
    gray = ImageOps.invert(im.convert("L")).point(lambda v: 255 if v > 40 else 0)
    rows = [y for y in range(im.height) if gray.crop((0, y, im.width, y + 1)).getbbox()]
    # The browser's address bar sits at the very bottom; the table ends at the first
    # big gap after the top third of the screen.
    bottom = im.height
    for a, b in zip(rows, rows[1:]):
        if a > im.height // 3 and b - a > im.height // 8:
            bottom = a + 20
            break
    buf = io.BytesIO()
    im.crop((0, 0, im.width, min(bottom, im.height))).save(buf, "PNG")
    return buf.getvalue()


def transcribe(image_path, model):
    import anthropic

    client = anthropic.Anthropic(api_key=load_api_key())
    response = client.beta.messages.parse(
        model=model,
        max_tokens=16000,
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(screenshot_png(image_path)).decode(),
                }},
                {"type": "text", "text": PROMPT},
            ],
        }],
        output_format=schema(),
    )
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        sys.exit(f"transcription failed: stop_reason={response.stop_reason}")
    u = response.usage
    print(f"transcribed with {model}: {u.input_tokens} in / {u.output_tokens} out", file=sys.stderr)
    return response.parsed_output.model_dump()


# --- conversion: no AI below this line -----------------------------------------


class Problems(list):
    def add(self, where, msg):
        self.append(f"{where}: {msg}")


def clock(text, day, airport, problems, where):
    """A printed time ("1640", "1720 L") on `day` as an aware datetime, or None."""
    m = TIME_RE.match(text.strip())
    if not m:
        problems.add(where, f"unreadable time {text!r}")
        return None
    hh, mm, local = int(m[1]), int(m[2]), bool(m[3])
    if hh > 23 or mm > 59:
        problems.add(where, f"impossible time {text!r}")
        return None
    tz = JST
    if local:
        if airport in LOCAL_TZ:
            tz = ZoneInfo(LOCAL_TZ[airport])
        elif airport not in JAPAN:
            problems.add(where, f"local time at {airport!r}, whose time zone isn't known; add it to LOCAL_TZ")
            return None
    return dt.datetime.combine(day, dt.time(hh, mm), tz)


def duties(roster, problems):
    """Group the transcribed rows into days: [(date, [row, ...]), ...]."""
    m = re.match(r"^\s*(\d{4})\s*/\s*(\d{1,2})\s*$", roster["month"])
    if not m:
        problems.add("header", f"unreadable month {roster['month']!r}")
        return None, []
    year, month = int(m[1]), int(m[2])
    days, current = [], None
    for i, row in enumerate(roster["rows"], 1):
        row = {k: " ".join(str(v).split()) for k, v in row.items()}
        if row["date"]:
            dm = re.match(r"^(\d{1,2})/(\d{1,2})$", row["date"])
            if not dm or int(dm[1]) != month:
                problems.add(f"row {i}", f"unexpected date {row['date']!r}")
                continue
            try:
                current = dt.date(year, month, int(dm[2]))
            except ValueError:
                problems.add(f"row {i}", f"no such date {row['date']!r}")
                continue
            days.append((current, []))
        elif current is None:
            problems.add(f"row {i}", "continuation row before the first date")
            continue
        days[-1][1].append(row)
    seen = [d for d, _ in days]
    expected = [dt.date(year, month, n) for n in range(1, calendar.monthrange(year, month)[1] + 1)]
    for d in sorted(set(expected) - set(seen)):
        problems.add(d.strftime("%m/%d"), "missing from the transcription")
    for d in sorted({d for d in seen if seen.count(d) > 1}):
        problems.add(d.strftime("%m/%d"), "appears more than once")
    if seen != sorted(seen):
        problems.add("rows", "dates out of order")
    return (year, month), days


def flight_name(job):
    m = FLIGHT_RE.match(job.replace(" ", "")) or FLIGHT_RE.match(job)
    return f"{m[1]} {int(m[2])}" if m else None


def events(days, problems):
    """Calendar events from the grouped days: dicts with kind, summary, start, end, ..."""
    out, off_run = [], None

    def close_off_run():
        nonlocal off_run
        if off_run:
            codes = off_run["codes"]
            kind = "OFF/HOL" if any(c.startswith("HOL") for c in codes) else "OFF"
            out.append({
                "kind": "off", "summary": f"{kind} ({off_run['base']})" if off_run["base"] else kind,
                "start": off_run["first"], "end": off_run["last"] + dt.timedelta(days=1),
                "description": ", ".join(f"{d:%m/%d} {c}" for d, c in zip(off_run["dates"], codes)),
            })
        off_run = None

    for day, rows in days:
        where = day.strftime("%m/%d")
        if len(rows) == 1 and OFF_RE.match(rows[0]["job"].replace(" ", "")):
            code, base = rows[0]["job"].replace(" ", ""), rows[0]["dep_airport"]
            if off_run and off_run["last"] == day - dt.timedelta(days=1) and off_run["base"] == base:
                off_run.update(last=day)
                off_run["dates"].append(day)
                off_run["codes"].append(code)
            else:
                close_off_run()
                off_run = {"first": day, "last": day, "base": base, "dates": [day], "codes": [code]}
            continue
        close_off_run()

        # the day's report time belongs to its first duty, which may be on the ground
        report = rows[0]["on"]
        first_is_flight = bool(flight_name(rows[0]["job"]) and rows[0]["dep_airport"] and rows[0]["arr_airport"])
        release = next((r["off"] for r in reversed(rows) if r["off"]), "")
        stay = next((r["sty"] for r in reversed(rows) if r["sty"] in JAPAN or r["sty"] in LOCAL_TZ), "")
        ground = []
        prev_end = None
        legs = []
        for row in rows:
            name = flight_name(row["job"]) if row["dep_airport"] and row["arr_airport"] else None
            if name:
                start = clock(row["start"], day, row["dep_airport"], problems, f"{where} {name}")
                end = clock(row["end"], day, row["arr_airport"], problems, f"{where} {name}")
                if not (start and end):
                    continue
                if prev_end and start < prev_end:  # a later leg that departs after midnight
                    start += dt.timedelta(days=1)
                    end += dt.timedelta(days=1)
                if end <= start:
                    end += dt.timedelta(days=1)
                minutes = (end - start).total_seconds() / 60
                if not 20 <= minutes <= 14 * 60:
                    problems.add(f"{where} {name}", f"{minutes:.0f} minutes from departure to arrival")
                prev_end = end
                legs.append({
                    "kind": "flight", "summary": f"{name} {row['dep_airport']}-{row['arr_airport']}",
                    "start": start, "end": end, "aircraft": row["shp"], "pos": row["pos"],
                })
            elif row["start"] and row["end"] and not row["job"]:
                problems.add(where, f"times {row['start']}-{row['end']} with no job; left out")
            elif row["start"] and row["end"]:
                start = clock(row["start"], day, "", problems, f"{where} {row['job']}")
                end = clock(row["end"], day, "", problems, f"{where} {row['job']}")
                if start and end:
                    ground.append((row["job"], start, end, row["dep_airport"]))
            elif row["job"]:
                problems.add(f"{where} {row['job']}", "no times and not a day off; left out")
        if legs:
            if first_is_flight:
                legs[0]["report"] = report
            legs[-1]["release"] = release
            legs[-1]["stay"] = stay
        if ground:
            jobs = " / ".join(dict.fromkeys(g[0] for g in ground))
            out.append({
                "kind": "duty", "summary": f"Duty {jobs}",
                "start": min(g[1] for g in ground), "end": max(g[2] for g in ground),
                "location": ground[0][3],
            })
        out.extend(legs)
        if not legs and not ground and rows:
            problems.add(where, "nothing scheduled and not a day off")
    close_off_run()
    return out


def describe(ev):
    if ev["kind"] != "flight":
        return ev.get("description", "")
    parts = [f"Aircraft {ev['aircraft']}" if ev["aircraft"] else ""]
    if ev.get("pos"):
        parts.append(ev["pos"])  # printed in the unlabeled column after JOB; meaning unknown
    if ev.get("report"):
        parts.append(f"report {ev['report']}")
    if ev.get("release"):
        parts.append(f"off {ev['release']}")
    if ev.get("stay"):
        parts.append(f"stay {ev['stay']}")
    return ", ".join(p for p in parts if p)


def ics_text(value):
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    """RFC 5545: lines over 75 octets continue on the next line after a space."""
    out, data = [], line.encode()
    while len(data) > 75:
        cut = 75 if not out else 74
        while (data[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(data[:cut].decode())
        data = data[cut:]
    out.append(data.decode())
    return "\r\n ".join(out)


def to_ics(evs, year, month):
    utc = lambda t: t.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//roster-ics//EN", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", "X-WR-TIMEZONE:Asia/Tokyo",
    ]
    for ev in evs:
        if ev["kind"] == "off":
            when = [f"DTSTART;VALUE=DATE:{ev['start']:%Y%m%d}", f"DTEND;VALUE=DATE:{ev['end']:%Y%m%d}"]
            key = f"{ev['start']:%Y%m%d}-off"
        else:
            when = [f"DTSTART:{utc(ev['start'])}", f"DTEND:{utc(ev['end'])}"]
            key = f"{ev['start'].astimezone(JST):%Y%m%d}-{re.sub(r'[^A-Za-z0-9]+', '-', ev['summary']).strip('-').lower()}"
        lines += ["BEGIN:VEVENT", f"UID:{key}@roster-ics", f"DTSTAMP:{stamp}", *when,
                  f"SUMMARY:{ics_text(ev['summary'])}"]
        if describe(ev):
            lines.append(f"DESCRIPTION:{ics_text(describe(ev))}")
        if ev.get("location"):
            lines.append(f"LOCATION:{ics_text(ev['location'])}")
        lines += ["TRANSP:OPAQUE", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def table(evs):
    out = []
    for ev in evs:
        if ev["kind"] == "off":
            last = ev["end"] - dt.timedelta(days=1)
            span = f"{ev['start']:%m/%d}" + (f"-{last:%m/%d}" if last != ev["start"] else "")
            out.append(f"{span:<11} {'all day':<11} {ev['summary']}")
        else:
            s, e = ev["start"].astimezone(JST), ev["end"].astimezone(JST)
            nextday = "+1" if e.date() > s.date() else ""
            extra = f"  ({describe(ev)})" if ev["kind"] == "flight" else ""
            out.append(f"{s:%m/%d}{'':<6} {s:%H:%M}-{e:%H:%M}{nextday:<2} {ev['summary']}{extra}")
    return "\n".join(out)


def convert(roster):
    """(year, month, events, problems) from a transcription."""
    problems = Problems()
    ym, days = duties(roster, problems)
    evs = events(days, problems) if ym else []
    return (ym or (0, 0)), evs, problems


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("screenshot", nargs="?", type=Path)
    parser.add_argument("--from-json", type=Path, help="convert a saved transcription instead")
    parser.add_argument("-o", "--output", type=Path, help="default: YYYY-MM.ics next to the input")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--force", action="store_true", help="write the .ics even if checks fail")
    args = parser.parse_args()
    if bool(args.screenshot) == bool(args.from_json):
        parser.error("give a screenshot or --from-json, not both")

    if args.from_json:
        roster = json.loads(args.from_json.read_text())
        source = args.from_json
    else:
        roster = transcribe(args.screenshot, args.model)
        source = args.screenshot.with_suffix(".json")
        source.write_text(json.dumps(roster, ensure_ascii=False, indent=1) + "\n")
        print(f"saved the transcription to {source}", file=sys.stderr)

    (year, month), evs, problems = convert(roster)
    print(table(evs))
    if problems:
        print(f"\n{len(problems)} problem(s):", *problems, sep="\n  ", file=sys.stderr)
        if not args.force:
            print("\nNo .ics written. Fix the transcription (.json) and use --from-json, or --force.", file=sys.stderr)
            return 1
    out = args.output or source.with_name(f"{year:04d}-{month:02d}.ics")
    out.write_text(to_ics(evs, year, month), newline="")
    print(f"\nwrote {len(evs)} events to {out}. Check them against the screenshot before importing.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
