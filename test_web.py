"""Tests for event detection and stats: .venv/bin/python -m unittest test_web"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import termin_bot as bot
import web

CAT = bot.Category("344", "RWTH Studenten", "RWTH")


def res(date=None, time="09:00", error=None):
    slot = bot.Slot(date, time) if date else None
    return [bot.Result(CAT, "Super C", "50", earliest=slot, error=error)]


class EventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(web, "DB_PATH", Path(self.tmp.name) / "t.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        web.init_db()
        self.sent = []
        p = mock.patch.object(web, "send_email", lambda to, subj, body: self.sent.append((to, subj)))
        p.start()
        self.addCleanup(p.stop)

    def kinds(self):
        with web.db() as c:
            return [r["kind"] for r in c.execute("SELECT kind FROM events ORDER BY id")]

    def test_first_observation_creates_no_event(self):
        web.record(CAT, res("2026-11-01"))
        self.assertEqual(self.kinds(), [])

    def test_opened_closed_earlier(self):
        web.record(CAT, res())                     # baseline: nothing
        web.record(CAT, res("2026-12-01"))         # opened
        web.record(CAT, res("2026-12-01"))         # unchanged
        web.record(CAT, res("2026-11-20"))         # earlier
        web.record(CAT, res("2026-12-05"))         # later: no event
        web.record(CAT, res())                     # closed
        self.assertEqual(self.kinds(), ["opened", "earlier", "closed"])

    def test_available_since_only_when_opening_was_seen(self):
        web.record(CAT, res("2026-12-01"))         # already open when tracking began
        with web.db() as c:
            self.assertIsNone(c.execute("SELECT available_since FROM status").fetchone()[0])
        web.record(CAT, res())
        web.record(CAT, res("2026-12-01"))         # opening observed
        web.record(CAT, res("2026-11-30"))         # still open: keeps the opening time
        with web.db() as c:
            since = c.execute("SELECT available_since FROM status").fetchone()[0]
            opened = c.execute("SELECT ts FROM events WHERE kind='opened'").fetchone()[0]
        self.assertEqual(since, opened)

    def test_error_keeps_previous_state_and_creates_no_event(self):
        web.record(CAT, res("2026-12-01"))
        web.record(CAT, res(error="timeout"))
        web.record(CAT, res("2026-12-01"))
        self.assertEqual(self.kinds(), [])
        with web.db() as c:
            row = c.execute("SELECT * FROM status").fetchone()
        self.assertEqual(row["earliest"], "2026-12-01 09:00")
        self.assertIsNone(row["error"])

    def test_error_before_any_success_does_not_fake_an_opening(self):
        web.record(CAT, res(error="timeout"))
        web.record(CAT, res("2026-12-01"))
        self.assertEqual(self.kinds(), [])

    def test_subscribers_notified_on_open(self):
        with web.db() as c:
            c.execute("INSERT INTO categories VALUES ('344','RWTH Studenten','RWTH',0)")
            c.execute("INSERT INTO alerts (email, cat_id, token) VALUES ('a@b.de','344','t')")
        web.record(CAT, res())
        web.record(CAT, res("2026-12-01"))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "a@b.de")


class StatsTests(unittest.TestCase):
    def test_interval_hour_and_open_duration(self):
        base = datetime.now() - timedelta(days=6)
        ev = []
        for day in (0, 2, 4):
            t = base + timedelta(days=day)
            t = t.replace(hour=7, minute=0, second=0, microsecond=0)
            ev.append({"kind": "opened", "ts": t.isoformat()})
            ev.append({"kind": "closed", "ts": (t + timedelta(hours=2)).isoformat()})
        s = web.category_stats(ev, base - timedelta(days=1))
        self.assertEqual(s["openings"], 3)
        self.assertAlmostEqual(s["avg_interval_s"], 2 * 86400, delta=1)
        self.assertEqual(s["typical_hour"], 7)
        self.assertAlmostEqual(s["avg_open_s"], 7200, delta=1)

    def test_no_events(self):
        s = web.category_stats([], datetime.now())
        self.assertEqual(s["openings"], 0)
        self.assertIsNone(s["avg_interval_s"])
        self.assertIsNone(s["last_opened"])


class ParseTests(unittest.TestCase):
    PAGE = """<h1>Schritt 4</h1>
    <h3 title="Montag, 11.01.2027">Montag, 11.01.2027</h3>
    <td><button type="button" class="suggest_btn" disabled>08:00</button></td>
    <td><form method="post" class="suggestion_form" style="margin-bottom: 0px;">
        <input type="hidden" name="date" value="20270111" />
        <button type="submit" class="suggest_btn btn btn-primary " title="08:20">08:20</button >
    </form></td>
    <td><form method="post" class="suggestion_form"><input type="hidden" name="date" value="20270112" />
        <button type="submit" title="09:00">09:00</button></form></td>"""

    def test_first_slot(self):
        self.assertEqual(bot.parse_first_slot(self.PAGE), bot.Slot("2027-01-11", "08:20"))

    def test_no_slot(self):
        self.assertIsNone(bot.parse_first_slot("<h2>Kein freier Termin verfügbar</h2>"))


if __name__ == "__main__":
    unittest.main()
