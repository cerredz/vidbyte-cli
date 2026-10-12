"""Tests for scripts/meetings.py. Standard library only.

Run from the skill folder:  python -m unittest discover -s tests -v

Every calendar here is synthetic. Every command that calls vidbyte-cli (run and feedback) gets a
recording fake through --cli, and PATH holds only a folder with a fake codex and a decoy vidbyte-cli,
so no test can reach a real vidbyte-cli, spend money, or write to the real project catalog.
"""

import datetime as dt
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "meetings.py")
_spec = importlib.util.spec_from_file_location("meetings", SCRIPT)
meetings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(meetings)

TODAY = "2026-10-26"  # a Monday; the window is 2026-10-12 to 2026-11-08 and US daylight saving ends on 2026-11-01

NEW_YORK = """BEGIN:VTIMEZONE
TZID:America/New_York
BEGIN:DAYLIGHT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
TZNAME:EDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
TZNAME:EST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""

PACIFIC_WINDOWS = """BEGIN:VTIMEZONE
TZID:Pacific Standard Time
BEGIN:STANDARD
DTSTART:16010101T020000
TZOFFSETFROM:-0700
TZOFFSETTO:-0800
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=1SU;BYMONTH=11
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:-0800
TZOFFSETTO:-0700
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=2SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE"""

ME = "ATTENDEE;CN=Me;PARTSTAT=ACCEPTED:mailto:me@example.com"
DANA = "ATTENDEE;CN=Dana Lee;PARTSTAT=ACCEPTED:mailto:dana@example.com"
ORG_DANA = "ORGANIZER;CN=Dana Lee:mailto:dana@example.com"
ORG_ME = "ORGANIZER;CN=Me:mailto:me@example.com"
NY = "TZID=America/New_York"


def team(count, domain="example.com"):
    return "\n".join("ATTENDEE;PARTSTAT=ACCEPTED:mailto:person%d@%s" % (i, domain) for i in range(count))


def event(uid, summary, start, end, *lines):
    body = ["BEGIN:VEVENT", "UID:" + uid, "SUMMARY:" + summary, "DTSTART;%s:%s" % (NY, start) if "T" in start and not start.endswith("Z") else "DTSTART:" + start]
    if end:
        body.append("DTEND;%s:%s" % (NY, end) if "T" in end and not end.endswith("Z") else "DTEND:" + end)
    body += [line for block in lines for line in block.split("\n") if line]
    body.append("END:VEVENT")
    return "\n".join(body)


def calendar(*events, extra_tz="", header="X-WR-CALNAME:me@example.com\nX-WR-TIMEZONE:America/New_York"):
    parts = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//synthetic test//EN", header, NEW_YORK]
    if extra_tz:
        parts.append(extra_tz)
    parts += list(events) + ["END:VCALENDAR"]
    return "\r\n".join(line for part in parts for line in part.split("\n") if line) + "\r\n"


def main_calendar():
    return calendar(
        # Weekly on two days, across the end of daylight saving time.
        event("weekly", "Weekly sync", "20261005T100000", "20261005T110000",
              "RRULE:FREQ=WEEKLY;BYDAY=MO,WE", ORG_DANA, ME, DANA, team(2)),
        # Every other week, with a week start of Sunday.
        event("biweekly", "Biweekly review", "20261006T140000", "20261006T150000",
              "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU;WKST=SU", ORG_ME, ME, DANA, team(3)),
        # COUNT.
        event("count", "Kickoff series", "20261013T090000", "20261013T093000",
              "RRULE:FREQ=DAILY;COUNT=3", ORG_DANA, ME, DANA, team(1)),
        # UNTIL in UTC.
        event("until", "Old standup", "20261001T093000", "20261001T100000",
              "RRULE:FREQ=WEEKLY;BYDAY=TH;UNTIL=20261022T235959Z", ORG_DANA, ME, DANA),
        # EXDATE.
        event("exdate", "Team lunch", "20261002T120000", "20261002T130000",
              "RRULE:FREQ=WEEKLY;BYDAY=FR", "EXDATE;%s:20261023T120000" % NY, ORG_DANA, ME, DANA, team(2)),
        # A 1:1 with one moved instance and one cancelled instance (RECURRENCE-ID overrides).
        event("one-on-one", "1:1 Dana / Me", "20261006T150000", "20261006T153000",
              "RRULE:FREQ=WEEKLY;BYDAY=TU", ORG_DANA, ME, DANA),
        event("one-on-one", "1:1 Dana / Me", "20261028T160000", "20261028T163000",
              "RECURRENCE-ID;%s:20261027T150000" % NY, ORG_DANA, ME, DANA),
        event("one-on-one", "1:1 Dana / Me", "20261103T150000", "20261103T153000",
              "RECURRENCE-ID;%s:20261103T150000" % NY, "STATUS:CANCELLED", ORG_DANA, ME, DANA),
        # Monthly rules.
        event("m-2tu", "Monthly business review", "20260908T130000", "20260908T140000",
              "RRULE:FREQ=MONTHLY;BYDAY=2TU", ORG_DANA, ME, team(12)),
        event("m-lastfri", "Month-end close", "20260925T140000", "20260925T150000",
              "RRULE:FREQ=MONTHLY;BYDAY=-1FR", ORG_DANA, ME, DANA),
        event("m-day15", "Mid-month check", "20260915T110000", "20260915T113000",
              "RRULE:FREQ=MONTHLY;BYMONTHDAY=15", ORG_DANA, ME, DANA),
        event("m-lastweekday", "Last-weekday report", "20260930T160000", "20260930T163000",
              "RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1", ORG_DANA, ME, DANA),
        # Daily on weekdays.
        event("standup", "Daily standup", "20261001T091500", "20261001T093000",
              "RRULE:FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR", ORG_ME, ME, DANA, team(4)),
        # All-day events: one ignored, one time off for two days.
        "BEGIN:VEVENT\nUID:allday\nSUMMARY:Sprint 42\nDTSTART;VALUE=DATE:20261019\nDTEND;VALUE=DATE:20261020\nEND:VEVENT",
        "BEGIN:VEVENT\nUID:ooo\nSUMMARY:Out of office\nDTSTART;VALUE=DATE:20261022\nDTEND;VALUE=DATE:20261024\nEND:VEVENT",
        # A cancelled event and a declined meeting.
        event("cancelled", "Cancelled planning", "20261029T100000", "20261029T110000", "STATUS:CANCELLED", ORG_DANA, ME, DANA),
        event("declined", "All-hands", "20261030T100000", "20261030T110000", ORG_DANA,
              "ATTENDEE;PARTSTAT=DECLINED:mailto:me@example.com", team(12)),
        # Unsupported rules.
        event("byweekno", "Quarterly planning", "20261027T100000", "20261027T120000",
              "RRULE:FREQ=WEEKLY;BYWEEKNO=44", ORG_DANA, ME, DANA, team(2)),
        event("hourly", "Pager check", "20261028T130000", "20261028T131500", "RRULE:FREQ=HOURLY;INTERVAL=4",
              ORG_DANA, ME, DANA),
        # A Windows time zone name, defined by the file's own VTIMEZONE.
        "BEGIN:VEVENT\nUID:partner\nSUMMARY:Partner call\nDTSTART;TZID=Pacific Standard Time:20261027T090000\n"
        "DTEND;TZID=Pacific Standard Time:20261027T093000\nRRULE:FREQ=WEEKLY;BYDAY=TU\n" + ORG_ME + "\n" + ME + "\n"
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:partner@partner.test\nEND:VEVENT",
        # A floating focus hold (no TZID), a UTC meeting, an RDATE, a transparent event.
        "BEGIN:VEVENT\nUID:focus\nSUMMARY:Focus time\nDTSTART:20261029T130000\nDTEND:20261029T150000\nEND:VEVENT",
        event("utc", "UTC sync", "20261029T170000Z", "20261029T173000Z", ORG_DANA, ME, DANA, team(1)),
        event("rdate", "Ad-hoc retro", "20261014T150000", "20261014T160000", "RDATE;%s:20261104T150000" % NY,
              ORG_DANA, ME, DANA, team(1)),
        event("transparent", "FYI webinar", "20261030T140000", "20261030T150000", "TRANSP:TRANSPARENT", ORG_DANA, ME, team(30)),
        # An override whose series is not in the file.
        event("orphan", "Moved customer call", "20261105T110000", "20261105T120000", "RECURRENCE-ID;%s:20261104T110000" % NY,
              ORG_ME, ME, "ATTENDEE;PARTSTAT=ACCEPTED:mailto:buyer@customer.test"),
        # Redaction: a title with a phone number, PIN, address, and link; a description with a Meet link only.
        event("redact", "Call +1 415 555 0100 PIN: 123456 with bob@example.org https://zoom.us/j/99999", "20261102T130000",
              "20261102T133000", "DESCRIPTION:Join with Google Meet: https://meet.google.com/abc-defg-hij\\nJoin by phone: "
              "+1 650-555-0199 PIN: 987654#", ORG_DANA, ME, DANA, team(1)),
        # A real agenda, a room that must not count as a person, and a large meeting.
        event("agenda", "Roadmap planning", "20261103T100000", "20261103T120000",
              "DESCRIPTION:Agenda: review the roadmap draft\\, agree on the three priorities for next quarter\\, and assign owners.",
              ORG_DANA, ME, DANA, team(9), "ATTENDEE;CUTYPE=RESOURCE;PARTSTAT=ACCEPTED:mailto:room-4@resource.calendar.google.com"),
        extra_tz=PACIFIC_WINDOWS,
    )


def run_script(args, env=None, cwd=None):
    proc = subprocess.run([sys.executable, SCRIPT] + args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=cwd)
    return proc.returncode, proc.stdout, proc.stderr


def gather(tmp, ics_text, extra=None, name="cal.ics"):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(ics_text)
    out = os.path.join(tmp, "out")
    args = ["gather", "--ics", path, "--tz", "America/New_York", "--today", TODAY, "--out-dir", out,
            "--keep", "customer calls", "--keep", "1:1s"] + (extra or [])
    code, stdout, stderr = run_script(args)
    data = md = inputs = None
    if code == 0:
        with open(os.path.join(out, "meetings.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        with open(os.path.join(out, "meetings.md"), encoding="utf-8") as handle:
            md = handle.read()
        with open(os.path.join(out, "inputs.json"), encoding="utf-8") as handle:
            inputs = json.load(handle)
    return code, stdout, stderr, data, md, inputs, out


def by_title(data):
    return {s["title"]: s for s in data["series"]}


class IcsParsingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tmm-test-")
        cls.code, cls.stdout, cls.stderr, cls.data, cls.md, cls.inputs, cls.out = gather(cls.tmp, main_calendar())
        if cls.code != 0:
            raise AssertionError("gather failed: %s %s" % (cls.stdout, cls.stderr))
        cls.series = by_title(cls.data)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def dates(self, title):
        return [d["start"] for d in self.series[title]["dates"]]

    def test_weekly_two_days_keeps_local_time_across_dst(self):
        self.assertEqual(self.dates("Weekly sync"), [
            "2026-10-12 10:00", "2026-10-14 10:00", "2026-10-19 10:00", "2026-10-21 10:00",
            "2026-10-26 10:00", "2026-10-28 10:00", "2026-11-02 10:00", "2026-11-04 10:00"])
        tag = self.series["Weekly sync"]["tag"]
        starts = {b["date"]: b["start"] for b in self.data["busy"] if b["tag"] == tag}
        self.assertEqual(starts["2026-10-26"], "2026-10-26T14:00:00Z")  # EDT, UTC-4
        self.assertEqual(starts["2026-11-02"], "2026-11-02T15:00:00Z")  # EST, UTC-5
        self.assertEqual(self.series["Weekly sync"]["repeats"], "weekly on Mon, Wed")

    def test_interval_with_wkst(self):
        self.assertEqual(self.dates("Biweekly review"), ["2026-10-20 14:00", "2026-11-03 14:00"])
        self.assertTrue(self.series["Biweekly review"]["organized_by_you"])
        self.assertEqual(self.series["Biweekly review"]["repeats"], "every 2 weeks on Tue")

    def test_count(self):
        self.assertEqual(self.dates("Kickoff series"), ["2026-10-13 09:00", "2026-10-14 09:00", "2026-10-15 09:00"])

    def test_until(self):
        self.assertEqual(self.dates("Old standup"), ["2026-10-15 09:30", "2026-10-22 09:30"])

    def test_exdate(self):
        self.assertEqual(self.dates("Team lunch"), ["2026-10-16 12:00", "2026-10-30 12:00", "2026-11-06 12:00"])

    def test_recurrence_id_moves_and_cancels(self):
        self.assertEqual(self.dates("1:1 Dana / Me"), ["2026-10-13 15:00", "2026-10-20 15:00", "2026-10-28 16:00"])
        item = self.series["1:1 Dana / Me"]
        self.assertTrue(item["one_on_one"])
        self.assertEqual(item["participants"], 2)
        self.assertEqual(item["organizer_name"], "Dana Lee")

    def test_monthly_rules(self):
        self.assertEqual(self.dates("Monthly business review"), ["2026-10-13 13:00"])
        self.assertEqual(self.dates("Month-end close"), ["2026-10-30 14:00"])
        self.assertEqual(self.dates("Mid-month check"), ["2026-10-15 11:00"])
        self.assertEqual(self.dates("Last-weekday report"), ["2026-10-30 16:00"])
        self.assertEqual(self.series["Monthly business review"]["repeats"], "monthly on the 2nd Tue")
        self.assertEqual(self.series["Month-end close"]["repeats"], "monthly on the last Fri")
        self.assertEqual(self.series["Last-weekday report"]["repeats"], "monthly on the last weekday")
        self.assertTrue(self.series["Monthly business review"]["more_than_8"])

    def test_daily_on_weekdays(self):
        dates = self.dates("Daily standup")
        self.assertEqual(len(dates), 20)
        self.assertTrue(all(dt.date.fromisoformat(d[:10]).weekday() < 5 for d in dates))
        self.assertEqual(self.series["Daily standup"]["repeats"], "every weekday")

    def test_all_day_and_time_off(self):
        self.assertNotIn("Sprint 42", self.series)
        self.assertNotIn("Out of office", self.series)
        self.assertEqual(self.data["not_counted"]["days_off"], ["2026-10-22", "2026-10-23"])
        work_days = {d["date"] for d in self.data["work_days"]}
        self.assertNotIn("2026-10-22", work_days)
        self.assertIn("2026-10-21", work_days)

    def test_cancelled_and_declined(self):
        self.assertNotIn("Cancelled planning", self.series)
        self.assertNotIn("All-hands", self.series)
        self.assertGreaterEqual(self.data["not_counted"]["cancelled"], 2)  # the event and the cancelled 1:1 date
        self.assertEqual(self.data["not_counted"]["declined"], 1)

    def test_unsupported_rules_are_reported_not_guessed(self):
        reasons = {r["title"]: r["reason"] for r in self.data["unsupported_rules"]}
        self.assertIn("BYWEEKNO", reasons["Quarterly planning"])
        self.assertIn("FREQ=HOURLY", reasons["Pager check"])
        self.assertEqual(self.dates("Quarterly planning"), ["2026-10-27 10:00"])
        self.assertEqual(self.dates("Pager check"), ["2026-10-28 13:00"])
        self.assertIn("Repeat rule not expanded", self.md)

    def test_windows_tzid_from_vtimezone(self):
        tag = self.series["Partner call"]["tag"]
        starts = {b["date"]: b["start"] for b in self.data["busy"] if b["tag"] == tag}
        self.assertEqual(starts["2026-10-27"], "2026-10-27T16:00:00Z")  # 09:00 PDT
        self.assertEqual(starts["2026-11-03"], "2026-11-03T17:00:00Z")  # 09:00 PST
        self.assertEqual(self.series["Partner call"]["external_domains"], ["partner.test"])

    def test_floating_focus_hold_is_free_and_utc_is_converted(self):
        self.assertNotIn("Focus time", self.series)
        self.assertEqual(self.data["not_counted"]["focus_holds"], 1)
        self.assertEqual(self.dates("UTC sync"), ["2026-10-29 13:00"])

    def test_rdate_and_orphan_override(self):
        self.assertEqual(self.dates("Ad-hoc retro"), ["2026-10-14 15:00", "2026-11-04 15:00"])
        self.assertEqual(self.dates("Moved customer call"), ["2026-11-05 11:00"])

    def test_transparent_not_counted(self):
        self.assertNotIn("FYI webinar", self.series)
        self.assertEqual(self.data["not_counted"]["free"], 1)

    def test_rooms_do_not_count_and_agenda_is_detected(self):
        item = self.series["Roadmap planning"]
        self.assertEqual(item["participants"], 11)  # me, Dana, 9 others; the room is left out
        self.assertTrue(item["agenda"])
        redacted = [s for s in self.data["series"] if s["title"].startswith("Call ")][0]
        self.assertFalse(redacted["agenda"])  # a Meet link and dial-in only

    def test_redaction_everywhere(self):
        everything = self.md + json.dumps(self.data) + json.dumps(self.inputs) + self.stdout
        for leak in ("415 555", "123456", "987654", "bob@example.org", "zoom.us", "meet.google", "http",
                     "dana@example.com", "me@example.com", "person1@", "room-4"):
            self.assertNotIn(leak, everything, leak)
        self.assertIn("[phone]", self.md)
        self.assertIn("[code]", self.md)
        self.assertIn("[email]", self.md)
        self.assertIn("[link]", self.md)
        self.assertNotIn("Dana Lee", self.md)  # organizer names stay in meetings.json only

    def test_me_detected_and_masked(self):
        self.assertIn("m***@example.com", self.stdout)
        self.assertIn("X-WR-CALNAME", self.data["you_identified_by"])

    def test_inputs(self):
        self.assertEqual(self.inputs["forbidden"], ["declining or leaving meetings without telling the organizer"])
        self.assertEqual(self.inputs["project"], "too-many-meetings")
        self.assertIn("customer calls; 1:1s", self.inputs["constraint"][0])
        self.assertTrue(self.inputs["goal"].startswith("Get back 10 focus hours a week."))
        self.assertLessEqual(len(self.inputs["goal"]), 600)
        self.assertTrue(all(v.startswith("[M") and len(v) <= 400 for v in self.inputs["context"]))
        self.assertTrue(all(len(v) <= 400 for v in self.inputs["outcomes"]))
        self.assertLessEqual(len(self.inputs["context"]), 12)

    def test_weeks_and_tags(self):
        self.assertEqual([w["ahead"] for w in self.data["weeks"]], [False, False, True, True])
        tags = [s["tag"] for s in self.data["series"]]
        self.assertEqual(tags, ["M%d" % i for i in range(1, len(tags) + 1)])
        hours = [s["hours_per_week"] for s in self.data["series"]]
        self.assertEqual(hours, sorted(hours, reverse=True))
        self.assertTrue(self.data["chains"])


class RruleUnitTest(unittest.TestCase):
    def expand(self, start, rule, until_tz=meetings.UTC, stop="2027-12-31"):
        parts = meetings.parse_rrule(rule)
        self.assertIsNone(meetings.check_rrule(parts))
        stop_after = dt.datetime.fromisoformat(stop)
        until_ok = meetings.make_until_ok(parts.get("UNTIL"), until_tz, False)
        return [d.strftime("%Y-%m-%d %H:%M") for d in meetings.expand_rrule(dt.datetime.fromisoformat(start), parts, stop_after, until_ok)]

    def test_count_counts_dtstart(self):
        self.assertEqual(self.expand("2026-10-05T10:00", "FREQ=WEEKLY;BYDAY=MO,WE;COUNT=3"),
                         ["2026-10-05 10:00", "2026-10-07 10:00", "2026-10-12 10:00"])

    def test_until_date(self):
        self.assertEqual(self.expand("2026-10-05T10:00", "FREQ=DAILY;UNTIL=20261007"),
                         ["2026-10-05 10:00", "2026-10-06 10:00", "2026-10-07 10:00"])

    def test_monthly_31st_skips_short_months(self):
        self.assertEqual(self.expand("2026-01-31T09:00", "FREQ=MONTHLY;COUNT=3"),
                         ["2026-01-31 09:00", "2026-03-31 09:00", "2026-05-31 09:00"])

    def test_monthly_negative_monthday_and_interval(self):
        self.assertEqual(self.expand("2026-01-31T09:00", "FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=-1;COUNT=3"),
                         ["2026-01-31 09:00", "2026-03-31 09:00", "2026-05-31 09:00"])

    def test_yearly_plain(self):
        self.assertEqual(self.expand("2024-02-29T09:00", "FREQ=YEARLY;COUNT=2", stop="2029-01-01"), ["2024-02-29 09:00", "2028-02-29 09:00"])

    def test_unsupported_parts(self):
        for rule in ("FREQ=MINUTELY", "FREQ=WEEKLY;BYHOUR=9", "FREQ=YEARLY;BYMONTH=3;BYDAY=2SU", "FREQ=WEEKLY;BYDAY=2MO",
                     "FREQ=DAILY;BYSETPOS=1"):
            self.assertIsNotNone(meetings.check_rrule(meetings.parse_rrule(rule)), rule)

    def test_line_unfolding_and_escapes(self):
        root = meetings.parse_ics("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Long \r\n title\\, with comma\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        ev = root.children[0].children[0]
        self.assertEqual(meetings.unescape(ev.value("SUMMARY")), "Long title, with comma")

    def test_vtimezone_without_tz_database(self):
        comp = meetings.parse_ics("BEGIN:VCALENDAR\n" + NEW_YORK + "\nEND:VCALENDAR").children[0].children[0]
        zone = meetings.VTimezone(comp)
        summer = dt.datetime(2026, 7, 1, 12, 0, tzinfo=zone)
        winter = dt.datetime(2026, 12, 1, 12, 0, tzinfo=zone)
        self.assertEqual(summer.utcoffset(), dt.timedelta(hours=-4))
        self.assertEqual(winter.utcoffset(), dt.timedelta(hours=-5))
        back = dt.datetime(2026, 7, 1, 16, 0, tzinfo=meetings.UTC).astimezone(zone)
        self.assertEqual(back.strftime("%H:%M"), "12:00")


class OtherSourcesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmm-src-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_events_json(self):
        events = {"schema_version": 1, "time_zone": "America/New_York", "events": [
            {"series_id": "s1", "title": "Weekly product sync", "start": "2026-10-27T10:00:00-04:00",
             "end": "2026-10-27T11:00:00-04:00", "organizer": {"name": "Dana Lee", "email": "dana@example.com"},
             "attendees": [{"email": "me@example.com", "self": True, "response": "tentativelyAccepted"},
                           {"email": "dana@example.com", "responseStatus": "accepted"},
                           {"email": "ext@customer.test", "response": "needsAction"}]},
            {"series_id": "s1", "title": "Weekly product sync", "start": {"dateTime": "2026-11-03T10:00:00", "timeZone": "America/New_York"},
             "end": {"dateTime": "2026-11-03T11:00:00", "timeZone": "America/New_York"},
             "organizer": {"name": "Dana Lee", "email": "dana@example.com"},
             "attendees": [{"email": "me@example.com", "self": True, "response": "accepted"},
                           {"email": "dana@example.com"}, {"email": "ext@customer.test"}]},
            {"title": "Board prep", "start": "2026-10-28T14:00:00Z", "end": "2026-10-28T15:00:00Z", "my_response": "declined",
             "attendees": [{"email": "me@example.com", "self": True}, {"email": "a@example.com"}]},
            {"title": "Holiday", "start": "2026-11-05", "end": "2026-11-06", "all_day": True, "show_as": "oof"},
            {"title": "Gone", "start": "2026-10-29T10:00:00Z", "end": "2026-10-29T11:00:00Z", "status": "cancelled"},
            {"title": "Optional webinar", "start": "2026-10-30T15:00:00Z", "end": "2026-10-30T16:00:00Z",
             "transparency": "transparent", "attendees": [{"email": "me@example.com", "self": True}, {"email": "b@example.com"}]},
        ]}
        path = os.path.join(self.tmp, "events.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(events, handle)
        out = os.path.join(self.tmp, "out")
        code, stdout, stderr = run_script(["gather", "--events-json", path, "--today", TODAY, "--out-dir", out])
        self.assertEqual(code, 0, stdout + stderr)
        with open(os.path.join(out, "meetings.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        series = by_title(data)
        self.assertEqual(list(series), ["Weekly product sync"])
        item = series["Weekly product sync"]
        self.assertEqual([d["start"] for d in item["dates"]], ["2026-10-27 10:00", "2026-11-03 10:00"])
        self.assertEqual(item["your_rsvp"], "tentative")  # from the next date
        self.assertEqual(item["participants"], 3)
        self.assertEqual(item["external_domains"], ["customer.test"])
        self.assertEqual(data["not_counted"]["days_off"], ["2026-11-05"])
        self.assertEqual(data["not_counted"]["declined"], 1)
        self.assertEqual(data["not_counted"]["free"], 1)  # Google's "transparency": "transparent"
        self.assertIn("the calendar tool's self flags", data["you_identified_by"])

    def test_gcalcli_with_a_fake(self):
        fake_dir = os.path.join(self.tmp, "bin")
        os.makedirs(fake_dir)
        record = os.path.join(self.tmp, "gcalcli-args.json")
        tsv = ("id\tstart_date\tstart_time\tend_date\tend_time\ttitle\temail\n"
               "abc_20261027T140000Z\t2026-10-27\t10:00\t2026-10-27\t11:00\tProduct sync\tme@example.com\n"
               "abc_20261103T150000Z\t2026-11-03\t10:00\t2026-11-03\t11:00\tProduct sync\tme@example.com\n"
               "xyz\t2026-10-28\t13:00\t2026-10-28\t14:00\tVendor demo\tvendor@vendor.test\n"
               "ooo1\t2026-11-06\t\t2026-11-07\t\tVacation\tme@example.com\n")
        write_fake(fake_dir, "gcalcli", "import json, sys\njson.dump(sys.argv[1:], open(%r, 'w'))\nsys.stdout.write(%r)\n" % (record, tsv))
        env = dict(os.environ, PATH=fake_dir)
        # No --tz: gcalcli prints this computer's local time, and the report shows the same clock times.
        out = os.path.join(self.tmp, "out")
        code, stdout, stderr = run_script(["gather", "--gcalcli", "--gcalcli-calendar", "Work", "--me", "me@example.com",
                                           "--today", TODAY, "--out-dir", out], env=env)
        self.assertEqual(code, 0, stdout + stderr)
        with open(record) as handle:
            argv = json.load(handle)
        self.assertEqual(argv, ["--nocolor", "agenda", "--tsv", "--nodeclined", "--details", "id", "--details", "email",
                                "--calendar", "Work", "2026-10-12", "2026-11-09"])
        with open(os.path.join(out, "meetings.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        series = by_title(data)
        self.assertEqual(len(series["Product sync"]["dates"]), 2)
        self.assertTrue(series["Product sync"]["organized_by_you"])
        self.assertIsNone(series["Product sync"]["participants"])
        self.assertFalse(series["Vendor demo"]["organized_by_you"])
        self.assertEqual(data["not_counted"]["days_off"], ["2026-11-06"])
        self.assertNotIn("vendor@vendor.test", json.dumps(data) + stdout)

    def test_ical_address_is_never_printed(self):
        secret = "private-SECRETTOKEN123"
        for url in ("https://127.0.0.1:9/calendar/ical/%s/basic.ics" % secret,
                    "webcal://127.0.0.1:9/%s/basic.ics" % secret, "http://127.0.0.1:9/%s.ics" % secret):
            path = os.path.join(self.tmp, "url.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(url + "\n")
            code, stdout, stderr = run_script(["gather", "--ics-url-file", path, "--today", TODAY])
            self.assertEqual(code, 1, stdout + stderr)
            self.assertNotIn(secret, stdout + stderr)
            self.assertNotIn("127.0.0.1", stdout + stderr)
        env = dict(os.environ, TMM_TEST_URL="https://127.0.0.1:9/%s" % secret)
        code, stdout, stderr = run_script(["gather", "--ics-url-env", "TMM_TEST_URL", "--today", TODAY], env=env)
        self.assertEqual(code, 1)
        self.assertNotIn(secret, stdout + stderr)

    def test_no_meetings_exits_3(self):
        code, *_ = gather(self.tmp, calendar("BEGIN:VEVENT\nUID:x\nSUMMARY:Holiday\nDTSTART;VALUE=DATE:20261027\nEND:VEVENT"))
        self.assertEqual(code, 3)

    def test_one_source_only(self):
        code, stdout, stderr = run_script(["gather", "--today", TODAY])
        self.assertEqual(code, 2)
        self.assertIn("exactly one source", stderr)


class WhatIfTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tmm-whatif-")
        cls.code, _, _, cls.data, _, _, cls.out = gather(cls.tmp, main_calendar())
        cls.series = by_title(cls.data)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def whatif(self, *args):
        return run_script(["whatif", "--out-dir", self.out] + list(args))

    def test_changes_alone_and_together(self):
        weekly = self.series["Weekly sync"]["tag"]
        standup = self.series["Daily standup"]["tag"]
        roadmap = self.series["Roadmap planning"]["tag"]
        biweekly = self.series["Biweekly review"]["tag"]
        lunch = self.series["Team lunch"]["tag"]
        code, stdout, stderr = self.whatif("--drop", weekly, "--shorten", roadmap + "=25", "--async", standup,
                                           "--move", biweekly + "=fri@16:00", "--every-other", lunch)
        self.assertEqual(code, 0, stdout + stderr)
        with open(os.path.join(self.out, "plan.json"), encoding="utf-8") as handle:
            plan = json.load(handle)
        rows = {c["tag"]: c for c in plan["changes"]}
        self.assertAlmostEqual(rows[weekly]["meeting_hours_freed_per_week"], 2.0, places=1)  # 4 x 60 min over 2 weeks
        self.assertAlmostEqual(rows[roadmap]["meeting_hours_freed_per_week"], 0.79, places=1)  # 120 -> 25 min, once
        self.assertEqual(rows[biweekly]["meeting_hours_freed_per_week"], 0.0)  # moving frees no meeting time
        self.assertGreater(plan["together"]["focus_hours_back_per_week"], 0)
        self.assertEqual(rows[weekly]["write_to"], "Dana Lee")
        self.assertEqual(rows[standup]["write_to"], "the attendees (you organize it)")
        self.assertIn("All 5 changes together", stdout)

    def test_one_change_per_meeting(self):
        tag = self.series["Weekly sync"]["tag"]
        code, _, stderr = self.whatif("--drop", tag, "--async", tag)
        self.assertEqual(code, 2)
        self.assertIn("one change", stderr)

    def test_unknown_tag_and_bad_shorten(self):
        self.assertEqual(self.whatif("--drop", "M999")[0], 2)
        tag = self.series["Weekly sync"]["tag"]
        self.assertEqual(self.whatif("--shorten", tag + "=90")[0], 2)

    def test_flags_keep_list(self):
        tag = self.series["1:1 Dana / Me"]["tag"]
        code, stdout, _ = self.whatif("--drop", tag)
        self.assertEqual(code, 0)
        self.assertIn("Check with the user", stdout)


FAKE_CLI = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_RECORD"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\n")
def flag_values(name):
    return [args[i + 1] for i, a in enumerate(args) if a == name and i + 1 < len(args)]
if "feedback" in args:
    print(json.dumps({"schema_version": 1, "kind": "suggestions.feedback", "data": {"project_key": flag_values("--project")[0]}}))
    sys.exit(0)
if "--dry-run" in args:
    manifest = [{"ref": "ctx-%03d" % (i + 1), "kind": "file" if a == "--files" else a[2:], "source": v, "status": "included"}
                for i, (a, v) in enumerate((a, args[j + 1]) for j, a in enumerate(args)
                                           if a in ("--constraint", "--forbidden", "--outcomes", "--context", "--files"))]
    print(json.dumps({"schema_version": 1, "kind": "suggestions.result", "data": {"status": "no_suggestions",
          "stop_reason": "dry_run", "ideas": [], "charged_cents": None, "context_manifest": manifest, "warnings": []}}))
    sys.exit(0)
sys.stderr.write("Suggestion admission retry key: %s. If admission fails, reuse this key.\n" % flag_values("--idempotency-key")[0])
sys.stderr.write("Vidbyte has verified the admission for this run.\n")
context = flag_values("--context")
idea = {"rank": 1, "id": "idea-001", "title": "Replace the Weekly sync with a written update", "summary": "s",
        "primary_category": "simplification", "readiness": "ready", "effort_estimate": "30 minutes", "horizon": "now",
        "first_action": "Ask the organizer.", "why_now": "w", "suggested_actions": ["a"], "completion_criteria": "c",
        "handoff": {"evidence": []}}
other = dict(idea, rank=2, id="idea-002", title="Stop the second meeting", horizon="later",
             handoff={"evidence": [{"source": "flag:context", "content": context[1] if len(context) > 1 else ""}]})
print(json.dumps({"schema_version": 1, "kind": "suggestions.result", "data": {"status": "complete", "stop_reason": "completed",
      "ideas": [idea, other], "charged_cents": 2, "admission": {"admission_id": "adm_fake", "charged_cents": 2, "units": 1},
      "context_manifest": [], "warnings": []}}))
'''


def write_fake(folder, name, code):
    """A fake executable that shutil.which() finds on PATH, on Windows and elsewhere."""
    script = os.path.join(folder, name + "_fake.py")
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(code)
    if os.name == "nt":
        target = os.path.join(folder, name + ".cmd")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write('@"%s" "%s" %%*\r\n' % (sys.executable, script))
    else:
        target = os.path.join(folder, name)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexec '%s' '%s' \"$@\"\n" % (sys.executable, script))
        os.chmod(target, os.stat(target).st_mode | stat.S_IEXEC)
    return target


DECOY_CLI = r'''
import os, sys
with open(os.environ["DECOY_RECORD"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\n")
sys.exit(99)
'''


class PaidRunGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmm-run-")
        code, _, _, self.data, _, _, self.out = gather(self.tmp, main_calendar())
        self.assertEqual(code, 0)
        # Every test passes the recording fake explicitly with --cli. PATH holds only a folder with a fake
        # codex and a decoy vidbyte-cli, so even a lookup on PATH can never reach the real vidbyte-cli.
        self.fakes = os.path.join(self.tmp, "fakes")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.fakes)
        os.makedirs(self.bin)
        self.record = os.path.join(self.tmp, "calls.jsonl")
        self.decoy_record = os.path.join(self.tmp, "decoy-calls.jsonl")
        self.fake = write_fake(self.fakes, "vidbyte-cli", FAKE_CLI)
        write_fake(self.bin, "vidbyte-cli", DECOY_CLI)
        self.set_codex(logged_in=True)
        self.env = dict(os.environ, PATH=self.bin, FAKE_RECORD=self.record, DECOY_RECORD=self.decoy_record)
        found = shutil.which("vidbyte-cli", path=self.env["PATH"])
        self.assertEqual(os.path.normcase(os.path.dirname(found)), os.path.normcase(self.bin))

    def tearDown(self):
        decoy_called = os.path.exists(self.decoy_record)
        shutil.rmtree(self.tmp, ignore_errors=True)
        if decoy_called and not getattr(self, "decoy_expected", False):
            self.fail("a command looked vidbyte-cli up on PATH instead of using --cli")

    def set_codex(self, logged_in):
        code = "import sys\nprint('Logged in using ChatGPT' if %r else 'Not logged in')\nsys.exit(0 if %r else 1)\n" % (logged_in, logged_in)
        write_fake(self.bin, "codex", code)

    def calls(self):
        if not os.path.exists(self.record):
            return []
        with open(self.record, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def run_cmd(self, *args):
        return run_script(["run", "--out-dir", self.out, "--cli", self.fake] + list(args), env=self.env)

    def test_dry_run_passes_a_relative_file_and_never_a_key(self):
        code, stdout, stderr = self.run_cmd("--dry-run")
        self.assertEqual(code, 0, stdout + stderr)
        call = self.calls()[0]
        argv = call["argv"]
        self.assertEqual(argv[:5], ["--json", "--no-input", "agents", "suggest", "run"])
        self.assertIn("--dry-run", argv)
        self.assertNotIn("--idempotency-key", argv)
        self.assertEqual(argv[argv.index("--files") + 1], "meetings.md")
        self.assertEqual(os.path.normcase(os.path.realpath(call["cwd"])), os.path.normcase(os.path.realpath(self.out)))
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--category"], ["stop_or_defer", "simplification", "delegation"])
        self.assertEqual(argv[argv.index("--horizon") + 1], "any")
        self.assertEqual(argv[argv.index("--count") + 1], "6")
        self.assertEqual(argv[argv.index("--project") + 1], "too-many-meetings")
        self.assertEqual(argv[argv.index("--forbidden") + 1], "declining or leaving meetings without telling the organizer")
        self.assertFalse(any(self.tmp.lower() in a.lower() for a in argv))
        context = [argv[i + 1] for i, a in enumerate(argv) if a == "--context"]
        with open(os.path.join(self.out, "inputs.json"), encoding="utf-8") as handle:
            self.assertEqual(context, json.load(handle)["context"])  # values survive the command line unchanged
        self.assertFalse(os.path.exists(os.path.join(self.out, "idempotency-key.txt")))
        self.assertEqual(json.loads(stdout)["stop_reason"], "dry_run")

    def test_paid_needs_codex_login(self):
        self.set_codex(logged_in=False)
        code, stdout, stderr = self.run_cmd("--paid")
        self.assertEqual(code, 2)
        self.assertIn("codex login", stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.out, "idempotency-key.txt")))

    def test_paid_once_then_refused_then_recovered(self):
        code, stdout, stderr = self.run_cmd("--paid")
        self.assertEqual(code, 0, stdout + stderr)
        with open(os.path.join(self.out, "idempotency-key.txt"), encoding="utf-8") as handle:
            key = handle.read().strip()
        self.assertRegex(key, r"^too-many-meetings:\d{8}:[0-9a-f]{6}$")
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--idempotency-key") + 1], key)
        self.assertNotIn("--dry-run", argv)
        self.assertEqual(json.loads(stdout)["charged_cents"], 2)
        # A second paid run in the same folder is refused before vidbyte-cli is called.
        code, _, stderr = self.run_cmd("--paid")
        self.assertEqual(code, 2)
        self.assertIn("already started", stderr)
        self.assertEqual(len(self.calls()), 1)
        # Recovery is refused while a finished result exists, and allowed with the saved key once it is gone.
        self.assertEqual(self.run_cmd("--paid", "--idempotency-key", key)[0], 2)
        os.remove(os.path.join(self.out, "result.json"))
        code, stdout, stderr = self.run_cmd("--paid", "--idempotency-key", key)
        self.assertEqual(code, 0, stdout + stderr)
        argv = self.calls()[1]["argv"]
        self.assertEqual(argv[argv.index("--idempotency-key") + 1], key)

    def test_neither_or_both_modes_refused(self):
        self.assertEqual(self.run_cmd()[0], 2)
        self.assertEqual(self.run_cmd("--dry-run", "--paid")[0], 2)
        self.assertEqual(self.run_cmd("--dry-run", "--idempotency-key", "abcdefgh1")[0], 2)
        self.assertEqual(self.calls(), [])

    def test_report_and_feedback(self):
        self.assertEqual(self.run_cmd("--paid")[0], 0)
        code, stdout, stderr = run_script(["report", "--out-dir", self.out], env=self.env)
        self.assertEqual(code, 0, stdout + stderr)
        weekly = by_title(self.data)["Weekly sync"]["tag"]
        self.assertIn("## Now", stdout)
        self.assertIn("## Later", stdout)
        self.assertIn("[%s]" % weekly, stdout)  # matched by title
        self.assertIn("[M2]", stdout)  # matched by the tag in the cited context value
        self.assertIn("This run cost 2 cents", stdout)
        code, stdout, stderr = run_script(["feedback", "--out-dir", self.out, "--idea", "idea-002", "--reject",
                                           "--reason", "I need that meeting", "--cli", self.fake], env=self.env)
        self.assertEqual(code, 0, stdout + stderr)
        argv = self.calls()[-1]["argv"]
        self.assertEqual(argv, ["--json", "agents", "suggest", "feedback", "reject", "--project", "too-many-meetings",
                                "--suggestion", "Stop the second meeting", "--reason", "I need that meeting"])

    def test_missing_cli_path_exits_127_before_anything(self):
        missing = os.path.join(self.tmp, "nowhere", "vidbyte-cli")
        code, stdout, _ = run_script(["run", "--out-dir", self.out, "--cli", missing, "--paid"], env=self.env)
        self.assertEqual(code, 127)
        self.assertIn("--cli", stdout)
        self.assertFalse(os.path.exists(os.path.join(self.out, "idempotency-key.txt")))
        self.assertEqual(self.calls(), [])

    def test_default_lookup_uses_path(self):
        # Without --cli the script runs vidbyte-cli from PATH: here, the decoy, which exits 99.
        self.decoy_expected = True
        code, _, _ = run_script(["run", "--out-dir", self.out, "--dry-run"], env=self.env)
        self.assertEqual(code, 99)
        self.assertTrue(os.path.exists(self.decoy_record))
        self.assertEqual(self.calls(), [])

    def test_bad_inputs_refused_before_the_cli(self):
        path = os.path.join(self.out, "inputs.json")
        with open(path, encoding="utf-8") as handle:
            inputs = json.load(handle)
        inputs["context"].append("x" * 500)
        inputs["surprise"] = 1
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(inputs, handle)
        code, stdout, _ = self.run_cmd("--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("surprise", stdout)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
