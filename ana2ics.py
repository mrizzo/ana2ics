#!/opt/homebrew/bin/python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["anthropic>=1.11", "pillow"]
# ///
"""Turn a screenshot of an ANA cabin attendant work schedule into an .ics file.

Broadcast Yourself? Schedule Yourself.

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
  ana2ics.py SCREENSHOT.png [-o OUT.ics]     # transcribe with Claude, then convert
  ana2ics.py SCREENSHOT.png -o - | pbcopy    # the .ics on stdout (the table goes to stderr)
  ana2ics.py --from-json SCREENSHOT.json     # convert a saved transcription again

The transcription is saved next to the screenshot as .json, so re-running the
conversion costs nothing. The API key comes from ANTHROPIC_API_KEY or
~/.config/ana2ics/api_key.

Import each month once. Importing adds and never replaces, so a month imported twice
(or on top of events made some other way) shows up twice.
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
API_KEY_FILE = Path("~/.config/ana2ics/api_key").expanduser()
# What the duty, leave, aircraft and position codes mean. ANA doesn't publish them, so
# this file is yours: one "CODE<tab>meaning" per line, filled in as you look them up.
CODES_FILE = Path("~/.config/ana2ics/codes.tsv").expanduser()
MODEL = "claude-opus-5-5"

# Airports: the name for descriptions, and the time zone for "L" (local) times.
# "L" is one letter, easy to miss. Miss it and the Shanghai leg gets an hour shorter:
# great for the airline, bad for physics, and a calendar that says she's home early.
_J = "Asia/Tokyo"
AIRPORTS = {
    # Japan
    "ITM": ("Itami", _J), "KIX": ("Kansai", _J), "UKB": ("Kobe", _J), "HND": ("Haneda", _J),
    "NRT": ("Narita", _J), "CTS": ("Sapporo", _J), "OKA": ("Okinawa", _J), "SDJ": ("Sendai", _J),
    "FUK": ("Fukuoka", _J), "NGO": ("Nagoya", _J), "HKD": ("Hakodate", _J), "KMQ": ("Komatsu", _J),
    "HIJ": ("Hiroshima", _J), "KOJ": ("Kagoshima", _J), "KMJ": ("Kumamoto", _J), "OIT": ("Oita", _J),
    "MYJ": ("Matsuyama", _J), "TAK": ("Takamatsu", _J), "KCZ": ("Kochi", _J), "TOY": ("Toyama", _J),
    "AOJ": ("Aomori", _J), "AXT": ("Akita", _J), "AKJ": ("Asahikawa", _J), "MMB": ("Memanbetsu", _J),
    "KUH": ("Kushiro", _J), "OBO": ("Obihiro", _J), "ISG": ("Ishigaki", _J), "MMY": ("Miyako", _J),
    "NGS": ("Nagasaki", _J), "KMI": ("Miyazaki", _J), "UBJ": ("Yamaguchi Ube", _J),
    "TKS": ("Tokushima", _J), "IZO": ("Izumo", _J), "YGJ": ("Yonago", _J), "OKJ": ("Okayama", _J),
    "FSZ": ("Shizuoka", _J), "NTQ": ("Noto", _J), "HSG": ("Saga", _J), "SHM": ("Nanki-Shirahama", _J),
    # Asia
    "PVG": ("Shanghai Pudong", "Asia/Shanghai"), "SHA": ("Shanghai Hongqiao", "Asia/Shanghai"),
    "PEK": ("Beijing", "Asia/Shanghai"), "PKX": ("Beijing Daxing", "Asia/Shanghai"),
    "CAN": ("Guangzhou", "Asia/Shanghai"), "DLC": ("Dalian", "Asia/Shanghai"),
    "TAO": ("Qingdao", "Asia/Shanghai"), "SZX": ("Shenzhen", "Asia/Shanghai"),
    "HKG": ("Hong Kong", "Asia/Hong_Kong"), "TPE": ("Taipei Taoyuan", "Asia/Taipei"),
    "TSA": ("Taipei Songshan", "Asia/Taipei"), "ICN": ("Seoul Incheon", "Asia/Seoul"),
    "GMP": ("Seoul Gimpo", "Asia/Seoul"), "MNL": ("Manila", "Asia/Manila"),
    "BKK": ("Bangkok", "Asia/Bangkok"), "SGN": ("Ho Chi Minh City", "Asia/Ho_Chi_Minh"),
    "HAN": ("Hanoi", "Asia/Ho_Chi_Minh"), "SIN": ("Singapore", "Asia/Singapore"),
    "KUL": ("Kuala Lumpur", "Asia/Kuala_Lumpur"), "CGK": ("Jakarta", "Asia/Jakarta"),
    "DEL": ("Delhi", "Asia/Kolkata"), "BOM": ("Mumbai", "Asia/Kolkata"),
    # further
    "HNL": ("Honolulu", "Pacific/Honolulu"), "SYD": ("Sydney", "Australia/Sydney"),
    "LAX": ("Los Angeles", "America/Los_Angeles"), "SFO": ("San Francisco", "America/Los_Angeles"),
    "SEA": ("Seattle", "America/Los_Angeles"), "YVR": ("Vancouver", "America/Vancouver"),
    "JFK": ("New York JFK", "America/New_York"), "ORD": ("Chicago", "America/Chicago"),
    "IAD": ("Washington Dulles", "America/New_York"), "LHR": ("London Heathrow", "Europe/London"),
    "FRA": ("Frankfurt", "Europe/Berlin"), "MUC": ("Munich", "Europe/Berlin"),
    "CDG": ("Paris", "Europe/Paris"),
}
# crew bases, as printed in FROM on ground duties and days off
BASES = {"OSA": "Osaka", "TYO": "Tokyo"}
ROW_FIELDS = ("date", "weekday", "on", "job", "pos", "shp", "conf", "dep_airport", "arr_airport",
              "start", "end", "day", "off", "sty")
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
right after a time as part of that time ("1720 L", also in ON, DAY and OFF), and use "" \
for an empty cell. \
Don't interpret, correct, convert or skip anything. Read flight numbers and times \
digit by digit: a wrong digit puts someone on the wrong flight."""


def schema():
    from pydantic import BaseModel, Field

    class Row(BaseModel):
        date: str = Field(description='DATE as printed, e.g. "10/01"; "" on a continuation row')
        weekday: str = Field(description='everything printed between DATE and ON, e.g. "TH A", "SU L", "L TU"')
        on: str = Field(description="ON (report time)")
        job: str = Field(description='JOB, e.g. "NH 34", "NH1273", "OFF1", "HOL1", "G1", "GRT"')
        pos: str = Field(description='the column between JOB and SHP, e.g. "CP", "GRF"')
        shp: str
        conf: str
        dep_airport: str = Field(description="FROM")
        arr_airport: str = Field(description="TO")
        start: str = Field(description='first TIME, with a trailing "L" if printed')
        end: str = Field(description='second TIME, with a trailing "L" if printed')
        day: str = Field(description='DAY, e.g. "12 L"; usually empty')
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
        if airport in AIRPORTS:
            tz = ZoneInfo(AIRPORTS[airport][1])
        else:
            problems.add(where, f"local time at {airport!r}, whose time zone isn't known; add it to AIRPORTS")
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
        # every column as a single-spaced string; transcriptions from before a column was
        # added (DAY) just have it empty
        row = {k: " ".join(str(row.get(k, "")).split()) for k in ROW_FIELDS}
        # the STY cell sometimes rides along in OFF ("2220 m", "2130 SDJ"); split it back out
        m_off = re.match(r"^(\d{4}(?: L)?) (\S+)$", row["off"])
        if m_off and not row["sty"] and m_off[2] != "L":
            row["off"], row["sty"] = m_off[1], m_off[2]
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
    out, off_run, last_leg = [], None, None

    def close_off_run():
        # Days off: the only events nobody minds being all day.
        nonlocal off_run
        if off_run:
            codes = off_run["codes"]
            if off_run["family"] == "off":
                kind = "OFF/HOL" if any(c.startswith("HOL") for c in codes) else "OFF"
            else:
                kind = off_run["family"]  # another all-day code, e.g. LO4: named as printed
            out.append({
                "kind": "off", "family": off_run["family"],
                "summary": f"{kind} ({off_run['base']})" if off_run["base"] else kind,
                "start": off_run["first"], "end": off_run["last"] + dt.timedelta(days=1),
                "description": ", ".join(f"{d:%m/%d} {c}" for d, c in zip(off_run["dates"], codes)),
                "codes": list(dict.fromkeys(codes)),
            })
        off_run = None

    for day, rows in days:
        where = day.strftime("%m/%d")
        only = rows[0] if len(rows) == 1 else None
        all_day = only and only["job"] and only["job"] != "CONT" and not (only["start"] or only["end"]) \
            and not (flight_name(only["job"]) and only["arr_airport"])
        if all_day:
            # days off (OFF1, HOL1...) group together; any other all-day code (LO4) groups
            # with itself
            code, base = only["job"].replace(" ", ""), only["dep_airport"]
            family = "off" if OFF_RE.match(code) else code
            if off_run and off_run["last"] == day - dt.timedelta(days=1) and off_run["base"] == base \
                    and off_run["family"] == family:
                off_run.update(last=day)
                off_run["dates"].append(day)
                off_run["codes"].append(code)
            else:
                close_off_run()
                off_run = {"first": day, "last": day, "base": base, "dates": [day], "codes": [code],
                           "family": family}
            continue
        close_off_run()

        # the day's report time belongs to its first duty, which may be on the ground
        report = rows[0]["on"]
        first_is_flight = bool(flight_name(rows[0]["job"]) and rows[0]["dep_airport"] and rows[0]["arr_airport"])
        release = next((r["off"] for r in reversed(rows) if r["off"]), "")
        stay = next((r["sty"] for r in reversed(rows) if r["sty"] in AIRPORTS), "")
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
                if prev_end and start < prev_end:  # a later leg that departs after midnight (Japanese TV calls it 25:30; calendars call it tomorrow)
                    start += dt.timedelta(days=1)
                    end += dt.timedelta(days=1)
                if end <= start:
                    end += dt.timedelta(days=1)
                minutes = (end - start).total_seconds() / 60
                if not 20 <= minutes <= 14 * 60:
                    problems.add(f"{where} {name}", f"{minutes:.0f} minutes from departure to arrival")
                prev_end = end
                deadhead = row["pos"] == "DH"  # riding as a passenger to or from a trip
                legs.append({
                    "kind": "flight", "summary": f"{name} {row['dep_airport']}-{row['arr_airport']}",
                    "start": start, "end": end, "aircraft": row["shp"], "pos": "" if deadhead else row["pos"],
                    "deadhead": deadhead, "day": row["day"],
                    "codes": [c for c in (row["shp"], "" if deadhead else row["pos"]) if c],
                    "from": row["dep_airport"], "to": row["arr_airport"],
                })
            elif row["start"] and row["end"] and not row["job"]:
                problems.add(where, f"times {row['start']}-{row['end']} with no job; left out")
            elif row["start"] and row["end"]:
                start = clock(row["start"], day, "", problems, f"{where} {row['job']}")
                end = clock(row["end"], day, "", problems, f"{where} {row['job']}")
                if start and end:
                    # a ground duty can carry a second code in the column after JOB ("EMG PTE")
                    label = f"{row['job']} {row['pos']}" if row["pos"] else row["job"]
                    ground.append((label, start, end, row["dep_airport"], [row["job"], row["pos"]]))
            elif row["job"] == "CONT":
                # the trip continues from the day before; its OFF is when that trip ends
                if last_leg and row["off"] and not last_leg.get("release"):
                    last_leg["release"] = row["off"]
            elif row["job"]:
                problems.add(f"{where} {row['job']}", "no times and not a day off; left out")
        if legs:
            if first_is_flight:
                legs[0]["report"] = report
            legs[-1]["release"] = release
            legs[-1]["stay"] = stay
            for leg in legs:
                # the title says what kind of leg it is: worked, deadhead, red-eye, layover after
                if leg["deadhead"]:
                    leg["summary"] = f"Deadhead: {leg['summary']}"
                if leg["end"].astimezone(JST).date() > leg["start"].astimezone(JST).date():
                    leg["summary"] = f"Overnight: {leg['summary']}"
                if leg.get("stay"):
                    leg["summary"] += f" · stay {leg['stay']}"
            last_leg = legs[-1]
        if ground:
            jobs = " / ".join(dict.fromkeys(g[0] for g in ground))
            out.append({
                "kind": "duty", "summary": f"Duty {jobs}",
                "start": min(g[1] for g in ground), "end": max(g[2] for g in ground),
                "location": BASES.get(ground[0][3]) or airport(ground[0][3]),
                "codes": list(dict.fromkeys(c for g in ground for c in g[4] if c)),
            })
        out.extend(legs)
        if not legs and not ground and not any(r["job"] == "CONT" for r in rows):
            problems.add(where, "nothing scheduled and not a day off")
    close_off_run()
    # in time order within each day (a ground duty can come after the day's flights)
    return sorted(out, key=lambda ev: ev["start"] if ev["kind"] != "off"
                  else dt.datetime.combine(ev["start"], dt.time(), JST))


def airport(code):
    return f"{AIRPORTS[code][0]} ({code})" if code in AIRPORTS else code


def details(ev):
    """A flight's one-line extras: aircraft, unlabeled code, report/off times, stay."""
    parts = ["Deadhead (riding as a passenger)" if ev.get("deadhead") else ""]
    parts.append(f"Aircraft {ev['aircraft']}" if ev["aircraft"] else "")
    if ev.get("pos"):
        parts.append(ev["pos"])  # printed in the unlabeled column after JOB; meaning unknown
    if ev.get("report"):
        parts.append(f"report {ev['report']}")
    if ev.get("release"):
        parts.append(f"off {ev['release']}")
    if ev.get("stay"):
        parts.append(f"stay {airport(ev['stay'])}")
    if ev.get("day"):
        parts.append(f"DAY {ev['day']}")  # meaning unknown; printed on long trips
    return ", ".join(p for p in parts if p)


def describe(ev):
    if ev["kind"] != "flight":
        return ev.get("description", "")
    route = f"{airport(ev['from'])} → {airport(ev['to'])}"
    return "\n".join(p for p in (route, details(ev)) if p)


def load_codes(path):
    """{code: meaning} from a codes file; a code with no meaning yet isn't included."""
    codes = {}
    if path and path.exists():
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            # a tab between code and meaning, or two or more spaces (editors turn tabs into spaces)
            code, *meaning = re.split(r"\t+| {2,}", line.strip(), maxsplit=1)
            if meaning and meaning[0].strip():
                codes[code] = meaning[0].strip()
    return codes


def glossary(ev, codes):
    """The event's codes spelled out, one per line, for the ones the codes file knows."""
    return "\n".join(f"{c}: {codes[c]}" for c in ev.get("codes", []) if c in codes)


def unknown_codes(evs, codes):
    return sorted({c for ev in evs for c in ev.get("codes", []) if c not in codes})


def full_description(ev, codes):
    return "\n\n".join(p for p in (describe(ev), glossary(ev, codes)) if p)


def ics_text(value):
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    """RFC 5545: lines over 75 octets continue on the next line after a space.

    Octets, not characters: a rule inherited from 1998, when calendars were sent by
    email and Japanese text broke them in creative ways. It still does if you let it."""
    out, data = [], line.encode()
    while len(data) > 75:
        cut = 75 if not out else 74
        while (data[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(data[:cut].decode())
        data = data[cut:]
    out.append(data.decode())
    return "\r\n ".join(out)


def to_ics(evs, year, month, codes=None):
    utc = lambda t: t.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ana2ics//EN", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", "X-WR-TIMEZONE:Asia/Tokyo",
    ]
    for ev in evs:
        if ev["kind"] == "off":
            when = [f"DTSTART;VALUE=DATE:{ev['start']:%Y%m%d}", f"DTEND;VALUE=DATE:{ev['end']:%Y%m%d}"]
            key = f"{ev['start']:%Y%m%d}-{ev['family'].lower()}"
        else:
            when = [f"DTSTART:{utc(ev['start'])}", f"DTEND:{utc(ev['end'])}"]
            key = f"{ev['start'].astimezone(JST):%Y%m%d}-{re.sub(r'[^A-Za-z0-9]+', '-', ev['summary']).strip('-').lower()}"
        lines += ["BEGIN:VEVENT", f"UID:{key}@ana2ics", f"DTSTAMP:{stamp}", *when,
                  f"SUMMARY:{ics_text(ev['summary'])}"]
        text = full_description(ev, codes or {})
        if text:
            lines.append(f"DESCRIPTION:{ics_text(text)}")
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
            extra = f"  ({details(ev)})" if ev["kind"] == "flight" else ""
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
    parser.add_argument("-o", "--output", type=Path, help="default: YYYY-MM.ics next to the input; - for stdout")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--force", action="store_true", help="write the .ics even if checks fail")
    parser.add_argument("--codes", type=Path, default=CODES_FILE, help=f"code meanings, default {CODES_FILE}")
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
    codes = load_codes(args.codes)
    to_stdout = str(args.output) == "-"
    print(table(evs), file=sys.stderr if to_stdout else sys.stdout)
    if problems:
        print(f"\n{len(problems)} problem(s):", *problems, sep="\n  ", file=sys.stderr)
        if not args.force:
            print("\nA team of highly trained monkeys has been dispatched to deal with this situation.\n"
                  "Meanwhile, no .ics was written: fix the transcription (.json) and re-run with\n"
                  "--from-json (no API call, no monkeys), or --force.", file=sys.stderr)
            return 1
    if to_stdout:
        sys.stdout.write(to_ics(evs, year, month, codes))
        sys.stdout.flush()
        out = "stdout"
    else:
        out = args.output or source.with_name(f"{year:04d}-{month:02d}.ics")
        out.write_text(to_ics(evs, year, month, codes), newline="")
    unknown = unknown_codes(evs, codes)
    if unknown:
        print(f"\nCodes with no meaning in {args.codes} yet: {', '.join(unknown)}\n"
              "Look each one up once, add a line (CODE<tab>meaning), and every calendar entry\n"
              "spells it out from then on.", file=sys.stderr)
    print(f"\nwrote {len(evs)} events to {out}.\n"
          "Processing done. In 2006 this took 30 minutes. Still, check it against the screenshot\n"
          "before importing: you are the last line of quality assurance.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
