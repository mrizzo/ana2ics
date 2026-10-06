"""Tests for the conversion half of ana2ics (no API calls). Run: python3 -m unittest"""
import datetime as dt
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("ana2ics", Path(__file__).with_name("ana2ics.py"))
ana = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ana)

KEYS = "date weekday on job pos shp conf dep_airport arr_airport start end off sty".split()


def roster(text, month="2025/02"):
    """A made-up transcription: one row per line, cells separated by |."""
    rows = [dict(zip(KEYS, (line.split("|") + [""] * 13)[:13])) for line in text.strip().splitlines()]
    return {"month": month, "rows": rows}


# February 2025: 28 days. Day 1 two legs, day 2 Shanghai and back with "L" times, day 3
# ground duties, days 4-6 off (with a HOL), day 7 overnight in Sendai, the rest off.
FEB = "\n".join([
    "02/01|SA A|0800|NH 21||789|X|ITM|HND|0900|1010||",
    "|||NH 22||789|X|HND|ITM|1100|1215|1245|A",
    "02/02|SU a|1300|NH 975||32E|X|KIX|PVG|1400|1530 L||",
    "|||NH 976||32E|X|PVG|KIX|1630 L|1945|2015|m",
    "02/03|MO A|0900|GRT||||OSA||0900|1600||",
    "|||WA10||||OSA||1600|1700|1700|A",
    "02/04|TU||OFF1||||OSA|||||",
    "02/05|WE||HOL1||||OSA|||||",
    "02/06|TH||OFF1||||OSA|||||",
    "02/07|FR A|1840|NH 739|CP|32N|X|ITM|SDJ|1940|2100|2130|SDJ",
] + [f"02/{d:02d}|XX||OFF1||||OSA|||||" for d in range(8, 29)])


class ConvertTest(unittest.TestCase):
    def setUp(self):
        (self.year, self.month), self.evs, self.problems = ana.convert(roster(FEB))

    def find(self, summary):
        return next(e for e in self.evs if e["summary"] == summary)

    def jst(self, ev):
        s, e = ev["start"].astimezone(ana.JST), ev["end"].astimezone(ana.JST)
        return f"{s:%m/%d %H:%M}-{e:%H:%M}"

    def test_clean_month_has_no_problems(self):
        self.assertEqual(self.problems, [])
        self.assertEqual((self.year, self.month), (2025, 2))

    def test_legs_in_japan_time(self):
        self.assertEqual(self.jst(self.find("NH 21 ITM-HND")), "02/01 09:00-10:10")
        self.assertEqual(self.find("NH 21 ITM-HND")["report"], "0800")
        self.assertEqual(self.find("NH 22 HND-ITM")["release"], "1245")

    def test_local_times_convert_from_shanghai(self):
        # 15:30 in Shanghai is 16:30 in Japan; 16:30 there is 17:30 here
        self.assertEqual(self.jst(self.find("NH 975 KIX-PVG")), "02/02 14:00-16:30")
        self.assertEqual(self.jst(self.find("NH 976 PVG-KIX")), "02/02 17:30-19:45")

    def test_ground_duties_merge(self):
        duty = self.find("Duty GRT / WA10")
        self.assertEqual(self.jst(duty), "02/03 09:00-17:00")

    def test_days_off_group_and_mark_holidays(self):
        off = [e for e in self.evs if e["kind"] == "off"]
        self.assertEqual([(e["summary"], e["start"], e["end"]) for e in off], [
            ("OFF/HOL (OSA)", dt.date(2025, 2, 4), dt.date(2025, 2, 7)),
            ("OFF (OSA)", dt.date(2025, 2, 8), dt.date(2025, 3, 1)),
        ])

    def test_overnight_stay(self):
        self.assertEqual(self.find("NH 739 ITM-SDJ")["stay"], "SDJ")
        self.assertIn("stay SDJ", ana.describe(self.find("NH 739 ITM-SDJ")))

    def test_ics_is_valid_and_stable(self):
        text = ana.to_ics(self.evs, self.year, self.month)
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertEqual(text.count("BEGIN:VEVENT"), len(self.evs))
        self.assertIn("UID:20250201-nh-21-itm-hnd@ana2ics", text)
        self.assertIn("DTEND:20250202T073000Z", text)  # NH 975 lands 16:30 JST
        self.assertIn("DTSTART;VALUE=DATE:20250204", text)
        self.assertTrue(all(len(l.encode()) <= 75 for l in text.split("\r\n")))


class ProblemsTest(unittest.TestCase):
    def problems(self, text):
        return ana.convert(roster(text))[2]

    def test_missing_days_are_reported(self):
        p = self.problems(FEB.replace("02/05|WE||HOL1||||OSA|||||\n", ""))
        self.assertIn("02/05: missing from the transcription", p)

    def test_unknown_local_airport(self):
        p = self.problems(FEB.replace("PVG|1400|1530 L", "XYZ|1400|1530 L"))
        self.assertTrue(any("time zone isn't known" in x for x in p))

    def test_bad_time(self):
        p = self.problems(FEB.replace("0900|1010", "0900|1O10"))
        self.assertTrue(any("unreadable time" in x for x in p))

    def test_implausible_leg(self):
        p = self.problems(FEB.replace("0900|1010", "0900|0905"))
        self.assertTrue(any("minutes from departure" in x for x in p))


class CodesTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "codes.tsv"
        # made-up meanings; tab and two-space separators, a comment, a code with no meaning yet
        self.path.write_text("# my codes\nGRT\tground thing\nOFF1  day off\n789\tBig plane\nWA10\t\n\n")
        (_, _), self.evs, _ = ana.convert(roster(FEB))
        self.codes = ana.load_codes(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_load_codes(self):
        self.assertEqual(self.codes, {"GRT": "ground thing", "OFF1": "day off", "789": "Big plane"})
        self.assertEqual(ana.load_codes(Path(self.dir.name) / "missing.tsv"), {})

    def test_descriptions_spell_codes_out(self):
        duty = next(e for e in self.evs if e["kind"] == "duty")
        self.assertEqual(ana.full_description(duty, self.codes), "GRT: ground thing")
        leg = next(e for e in self.evs if e["summary"] == "NH 21 ITM-HND")
        self.assertEqual(ana.full_description(leg, self.codes), "Aircraft 789, report 0800\n\n789: Big plane")
        ics = ana.to_ics(self.evs, 2025, 2, self.codes).replace("\r\n ", "")
        self.assertIn("DESCRIPTION:Aircraft 789\\, report 0800\\n\\n789: Big plane", ics)

    def test_unknown_codes_are_listed(self):
        self.assertEqual(ana.unknown_codes(self.evs, self.codes), ["32E", "32N", "CP", "HOL1", "WA10"])


class FoldTest(unittest.TestCase):
    def test_fold_keeps_utf8_whole(self):
        line = "DESCRIPTION:" + "日本語" * 20
        folded = ana.fold(line)
        self.assertEqual(folded.replace("\r\n ", ""), line)
        self.assertTrue(all(len(p.encode()) <= 75 for p in folded.split("\r\n")))


if __name__ == "__main__":
    unittest.main()
