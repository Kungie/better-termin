"""Tests for event detection and stats: .venv/bin/python -m unittest test_web"""

import os
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
        p = mock.patch.object(web, "send_email", lambda to, subj, body, html=None: self.sent.append((to, subj)))
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
            c.execute("INSERT INTO categories (id, name, grp, position) VALUES ('344','RWTH Studenten','RWTH',0)")
            c.execute("INSERT INTO alerts (email, cat_id, token, confirmed_at) VALUES ('a@b.de','344','t','2026-10-18T07:00:00')")
            c.execute("INSERT INTO alerts (email, cat_id, token) VALUES ('unconfirmed@b.de','344','u')")
        web.record(CAT, res())
        web.record(CAT, res("2026-12-01"))
        self.assertEqual([to for to, _ in self.sent], ["a@b.de"])  # unconfirmed get nothing


class AlertApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(web, "DB_PATH", Path(self.tmp.name) / "t.db")
        p.start(); self.addCleanup(p.stop)
        web.init_db()
        with web.db() as c:
            c.execute("INSERT INTO categories (id, name, grp, position) VALUES ('344','RWTH Studenten','RWTH',0)")
        self.mails = []
        p = mock.patch.object(web, "send_email", lambda to, subj, body, html=None: self.mails.append((to, subj, body)))
        p.start(); self.addCleanup(p.stop)
        web._signups.clear()
        p = mock.patch.dict(os.environ, {"DEV_MAIL_LOG": "1"})
        p.start(); self.addCleanup(p.stop)
        self.client = web.app.test_client()

    def signup(self, email="a@b.de", consent=True):
        return self.client.post("/api/alerts", json={"email": email, "category_id": "344", "consent": consent})

    def confirm_token(self):
        return self.mails[-1][2].split("confirm?token=")[1].split()[0]

    def status(self, token):
        return self.client.get(f"/api/alerts?tokens={token}").get_json()

    def test_double_opt_in(self):
        r = self.signup()
        self.assertEqual(r.status_code, 201)
        token = r.get_json()["token"]
        self.assertEqual(self.status(token)[0]["status"], "pending")
        self.assertIn("confirm?token=", self.mails[0][2])

        # opening the link (GET) must not confirm: mail scanners open links
        self.client.get(f"/confirm?token={self.confirm_token()}")
        self.assertEqual(self.status(token)[0]["status"], "pending")

        r = self.client.post("/confirm", data={"token": self.confirm_token()})
        self.assertIn(b"Alert confirmed", r.data)
        self.assertEqual(self.status(token)[0]["status"], "active")

        # the confirmation link works only once
        r = self.client.post("/confirm", data={"token": self.confirm_token()})
        self.assertIn(b"Link not valid", r.data)

    def test_consent_required(self):
        self.assertEqual(self.signup(consent=False).status_code, 400)
        self.assertEqual(self.mails, [])

    def test_repeat_signup_does_not_leak_token_and_throttles_mails(self):
        self.signup()
        r = self.signup()
        self.assertEqual(r.get_json(), {"status": "pending"})
        self.assertEqual(len(self.mails), 1)  # re-send only after RESEND_GAP

    def test_delete_needs_token(self):
        token = self.signup().get_json()["token"]
        self.assertEqual(self.client.delete("/api/alerts", json={"token": "wrong"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/alerts", json={"token": token}).status_code, 200)
        self.assertEqual(self.status(token), [])

    def test_unsubscribe_needs_post(self):
        token = self.signup().get_json()["token"]
        self.client.get(f"/unsubscribe?token={token}")
        self.assertEqual(len(self.status(token)), 1)
        self.client.post("/unsubscribe", data={"token": token})
        self.assertEqual(self.status(token), [])

    def test_unconfirmed_signups_expire(self):
        token = self.signup().get_json()["token"]
        old = (datetime.now() - web.CONFIRM_TTL - timedelta(minutes=1)).isoformat(timespec="seconds")
        with web.db() as c:
            c.execute("UPDATE alerts SET created_at=?", (old,))
        web.delete_unconfirmed()
        self.assertEqual(self.status(token), [])

    def test_rate_limit(self):
        for i in range(web.SIGNUPS_PER_IP_PER_HOUR):
            self.assertEqual(self.signup(f"u{i}@b.de").status_code, 201)
        self.assertEqual(self.signup("one-more@b.de").status_code, 429)

    def test_alerts_disabled_without_mail_delivery(self):
        with mock.patch.dict(os.environ, {"DEV_MAIL_LOG": ""}):
            self.assertEqual(self.signup().status_code, 503)
        self.assertEqual(self.mails, [])

    def test_cross_site_writes_blocked(self):
        r = self.client.post("/api/alerts", headers={"Origin": "https://evil.example"},
                             json={"email": "a@b.de", "category_id": "344", "consent": True})
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/unsubscribe", headers={"Origin": "https://evil.example"}, data={"token": "x"})
        self.assertEqual(r.status_code, 403)
        # "Origin: null" (sandboxed frames, privacy modes) is not trusted either
        r = self.client.post("/confirm", headers={"Origin": "null"}, data={"token": "x"})
        self.assertEqual(r.status_code, 403)
        self.assertIn(b"Request blocked", r.data)
        # same-origin requests pass
        r = self.client.post("/api/alerts", headers={"Origin": "http://localhost"},
                             json={"email": "a@b.de", "category_id": "344", "consent": True})
        self.assertEqual(r.status_code, 201)

    def test_security_headers(self):
        r = self.client.get("/")
        csp = r.headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["Referrer-Policy"], "same-origin")
        self.assertNotIn("<script>", r.get_data(as_text=True))  # CSP forbids inline scripts
        self.assertEqual(self.client.get("/api/alerts").headers["Cache-Control"], "no-store")

    def test_oversized_request_rejected(self):
        r = self.client.post("/api/alerts", data="x" * 20000, content_type="application/json")
        self.assertEqual(r.status_code, 413)

    def test_legal_pages_render(self):
        for path in ("/imprint", "/privacy", "/", "/favicon.ico", "/static/icons/icon.svg"):
            self.assertEqual(self.client.get(path).status_code, 200, path)


class SmtpTests(unittest.TestCase):
    def test_sends_via_starttls(self):
        env = {"SMTP_HOST": "smtp.example", "SMTP_PORT": "587", "SMTP_USER": "me@example", "SMTP_PASSWORD": "pw"}
        with mock.patch.dict(os.environ, env), mock.patch.object(web.smtplib, "SMTP") as smtp:
            web.send_email("to@example", "Subject", "Body")
        server = smtp.return_value
        server.starttls.assert_called_once()
        server.auth.assert_called_once_with("PLAIN", server.auth_plain)
        self.assertEqual((server.user, server.password), ("me@example", "pw"))
        msg = server.send_message.call_args[0][0]
        self.assertEqual((msg["To"], msg["From"], msg["Subject"]), ("to@example", "me@example", "Subject"))

    def test_html_alert_with_embedded_logo(self):
        subject, text, html = web.alert_email("RWTH Studenten", "2026-12-02 10:00", "opened", "tok")
        self.assertIn("Book on the official site", html[0])
        self.assertIn(bot.AUSLAENDERAMT.booking_url, text)
        _, text2, _ = web.alert_email("Abmeldung", "2026-12-02 10:00", "opened", "tok", bot.ROOT_URL + "select2?md=1")
        self.assertIn(bot.ROOT_URL + "select2?md=1", text2)
        self.assertIn("unsubscribe?token=tok", html[0])
        self.assertIn("unsubscribe?token=tok", text)
        env = {"SMTP_HOST": "smtp.example", "SMTP_USER": "me@example", "SMTP_PASSWORD": "pw"}
        with mock.patch.dict(os.environ, env), mock.patch.object(web.smtplib, "SMTP") as smtp:
            web.send_email("to@example", subject, text, html)
        msg = smtp.return_value.send_message.call_args[0][0]
        types = [part.get_content_type() for part in msg.walk()]
        self.assertEqual(types, ["multipart/alternative", "text/plain", "multipart/related", "text/html", "image/png"])
        logo = [p for p in msg.walk() if p.get_content_type() == "image/png"][0]
        self.assertIn(logo["Content-ID"][1:-1], html[0])  # <img src="cid:..."> matches the attachment

    def test_html_escapes_category_names(self):
        _, _, html = web.alert_email("<script>x</script>", "2026-12-02 10:00", "opened", "tok")
        self.assertNotIn("<script>x", html[0])

    def test_not_configured_sends_nothing(self):
        with mock.patch.dict(os.environ, {"SMTP_HOST": "", "DEV_MAIL_LOG": ""}), \
             mock.patch.object(web.smtplib, "SMTP") as smtp:
            web.send_email("to@example", "Subject", "Body")
        smtp.assert_not_called()


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


class RoundTests(unittest.TestCase):
    CATS = [bot.Category(str(i), f"cat {i}", "g") for i in range(3)]

    def setUp(self):
        p = mock.patch.object(bot, "REQUEST_DELAY", 0)
        p.start(); self.addCleanup(p.stop)

    def test_one_session_for_the_whole_round(self):
        session = object()
        with mock.patch.object(bot, "open_flow") as open_flow, \
             mock.patch.object(bot, "check_category", side_effect=lambda c, s: [bot.Result(c, "x")]) as check:
            out = list(bot.check_round(self.CATS, session))
        open_flow.assert_not_called()
        self.assertEqual([c.id for c, _ in out], ["0", "1", "2"])
        self.assertTrue(all(call.args[1] is session for call in check.call_args_list))

    def test_retries_on_a_fresh_session_when_the_shared_one_fails(self):
        fresh = object()
        calls = []
        def check(cat, sess):
            calls.append((cat.id, sess))
            return [bot.Result(cat, "x", error="Fehler")] if sess is not fresh else [bot.Result(cat, "x")]
        with mock.patch.object(bot, "open_flow", return_value=(fresh, self.CATS)), \
             mock.patch.object(bot, "check_category", side_effect=check):
            out = list(bot.check_round(self.CATS[:2], object()))
        self.assertTrue(all(not r.error for _, rs in out for r in rs))
        self.assertEqual([c for c, _ in calls], ["0", "0", "1"])  # retried once, then kept the fresh session

    def test_network_error_opens_a_new_session_for_the_next_category(self):
        import requests
        fresh = object()
        def check(cat, sess):
            if cat.id == "0":
                raise requests.ConnectionError("down")
            return [bot.Result(cat, "x")]
        with mock.patch.object(bot, "open_flow", return_value=(fresh, self.CATS)) as open_flow, \
             mock.patch.object(bot, "check_category", side_effect=check):
            out = list(bot.check_round(self.CATS[:2], object()))
        self.assertIn("down", out[0][1][0].error)
        self.assertIsNone(out[1][1][0].error)
        open_flow.assert_called_once()


class SiteTests(unittest.TestCase):
    SVA = bot.Site("sva3", "Monschau", bot.ROOT_URL, "3", order=3)

    def test_ausländeramt_keeps_its_ids_other_units_are_prefixed(self):
        self.assertEqual(bot.AUSLAENDERAMT.category_id("344"), "344")
        self.assertEqual(self.SVA.category_id("1298"), "sva3-1298")
        self.assertEqual(self.SVA.booking_url, bot.ROOT_URL + "select2?md=3")

    def test_parse_flat_category_list(self):
        page = """<form><input type="hidden" id="cncSelected_str-1254" value="Anliegen Abmeldung ausgewählt">
                  <input name="cnc-1254" type="number"><input name="cnc-1255" type="number"></form>"""
        cats = bot.parse_categories(page, self.SVA)
        self.assertEqual([(c.id, c.cnc, c.name, c.group) for c in cats],
                         [("sva3-1254", "1254", "Abmeldung", "Monschau"), ("sva3-1255", "1255", "1255", "Monschau")])

    def test_skips_test_and_special_permit_units(self):
        root = """<button onclick="window.location.href=&#039;select2?md=1&#039;" name="Zulassungsstelle Würselen">
                  <button onclick="window.location.href=&#039;select2?md=4&#039;" name="Ausnahmegenehmigungen (Taxi/Mietwagen)">
                  <button onclick="window.location.href=&#039;select2?md=5&#039;" name="A36.3_Verwaltung-Testumgebung">"""
        resp = mock.Mock(text=root)
        session = mock.Mock(); session.get.return_value = resp
        with mock.patch.object(bot, "new_session", return_value=session):
            sites = bot.strassenverkehrsamt_sites()
        self.assertEqual([(s.key, s.office) for s in sites], [("sva1", "Zulassungsstelle Würselen")])

    def test_parse_grouped_category_list(self):
        page = """<h3 id="header_concerns_accordion-1">RWTH</h3><div><input name="cnc-344"></div>"""
        [cat] = bot.parse_categories(page)
        self.assertEqual((cat.id, cat.group, cat.site), ("344", "RWTH", bot.AUSLAENDERAMT))


class MigrationAndLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(web, "DB_PATH", Path(self.tmp.name) / "t.db")
        p.start(); self.addCleanup(p.stop)

    def test_old_database_is_migrated_and_alerts_survive(self):
        import sqlite3
        conn = sqlite3.connect(web.DB_PATH)   # schema from before offices existed
        conn.executescript("""
            CREATE TABLE categories (id TEXT PRIMARY KEY, name TEXT, grp TEXT, position INTEGER);
            CREATE TABLE alerts (id INTEGER PRIMARY KEY, email TEXT NOT NULL, cat_id TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE, created_at TEXT, last_notified_at TEXT,
                confirm_token TEXT UNIQUE, confirmed_at TEXT, confirm_sent_at TEXT, UNIQUE (email, cat_id));
            INSERT INTO categories VALUES ('344', 'RWTH Studenten', 'RWTH', 0);
            INSERT INTO alerts (email, cat_id, token, confirmed_at) VALUES ('a@b.de', '344', 't', '2026-10-18T08:00:00');
        """)
        conn.commit(); conn.close()
        web.init_db()
        with web.db() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(categories)")}
            alert = c.execute("SELECT * FROM alerts").fetchone()
        self.assertTrue({"office", "booking_url"} <= cols)
        self.assertEqual((alert["email"], alert["cat_id"], alert["token"]), ("a@b.de", "344", "t"))

    def test_lane_stores_categories_with_their_office(self):
        web.init_db()
        sva = bot.Site("sva2", "Führerscheinstelle", bot.ROOT_URL, "2", order=2)
        cat = bot.Category("sva2-1357", "Umschreibung", "Führerschein", sva, "1357")
        checker = web.Checker(60, lanes={"test": (lambda: [sva], "sva*")})
        with mock.patch.object(bot, "open_flow", return_value=(object(), [cat])), \
             mock.patch.object(bot, "check_round", return_value=iter([(cat, [bot.Result(cat, "x", earliest=bot.Slot("2026-11-02", "08:00"))])])):
            self.assertIsNone(checker.run_lane("test"))
        with web.db() as c:
            row = c.execute("SELECT * FROM categories").fetchone()
            status = c.execute("SELECT earliest FROM status").fetchone()[0]
        self.assertEqual((row["id"], row["office"], row["booking_url"], row["position"]),
                         ("sva2-1357", "Führerscheinstelle", bot.ROOT_URL + "select2?md=2", 2000))
        self.assertEqual(status, "2026-11-02 08:00")
        self.assertEqual(checker.state["test"]["done"], 1)

    def test_a_failing_unit_does_not_stop_the_lane(self):
        web.init_db()
        ok = bot.Site("sva1", "Zulassung", bot.ROOT_URL, "1", order=1)
        bad = bot.Site("sva4", "Ausnahmen", bot.ROOT_URL, "4", order=4)
        cat = bot.Category("sva1-1", "Abmeldung", "Zulassung", ok, "1")
        def open_flow(site):
            if site is bad:
                raise RuntimeError("down")
            return object(), [cat]
        checker = web.Checker(60, lanes={"test": (lambda: [ok, bad], "sva*")})
        with mock.patch.object(bot, "open_flow", side_effect=open_flow), \
             mock.patch.object(bot, "check_round", return_value=iter([(cat, [bot.Result(cat, "x")])])):
            err = checker.run_lane("test")
        self.assertIn("Ausnahmen", err)
        self.assertEqual(checker.state["test"]["done"], 1)

    def test_last_check_not_updated_when_no_site_answers(self):
        web.init_db()
        site = bot.Site("sva1", "Zulassung", bot.ROOT_URL, "1", order=1)
        checker = web.Checker(60, lanes={"test": (lambda: [site], "sva*")})
        with mock.patch.object(bot, "open_flow", side_effect=RuntimeError("504")):
            checker.run_lane("test")
        self.assertIsNone(checker.last_check)


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(web, "DB_PATH", Path(self.tmp.name) / "t.db")
        p.start(); self.addCleanup(p.stop)
        web.init_db()
        with web.db() as c:
            for cid in ("344", "sva1-1", "sva4-1312", "sva4-1313"):
                c.execute("INSERT INTO categories (id, name) VALUES (?, ?)", (cid, cid))
                c.execute("INSERT INTO status (cat_id, earliest) VALUES (?, '2026-11-01 09:00')", (cid,))
                c.execute("INSERT INTO events (cat_id, ts, kind) VALUES (?, '2026-10-20T08:00:00', 'opened')", (cid,))
            c.execute("INSERT INTO alerts (email, cat_id, token) VALUES ('a@b.de', 'sva4-1313', 't')")

    def ids(self, table, col="id"):
        with web.db() as c:
            return sorted(r[0] for r in c.execute(f"SELECT {col} FROM {table}"))

    def test_removes_untracked_categories_but_keeps_alerted_ones_and_other_lanes(self):
        removed = web.prune_categories("sva*", {"sva1-1"})
        self.assertEqual(removed, ["sva4-1312"])
        self.assertEqual(self.ids("categories"), ["344", "sva1-1", "sva4-1313"])
        self.assertEqual(self.ids("status", "cat_id"), ["344", "sva1-1", "sva4-1313"])
        self.assertEqual(self.ids("events", "cat_id"), ["344", "sva1-1", "sva4-1313"])
        self.assertEqual(self.ids("alerts", "cat_id"), ["sva4-1313"])

    def test_numeric_glob_only_touches_ausländeramt_ids(self):
        self.assertEqual(web.prune_categories("[0-9]*", set()), ["344"])
        self.assertIn("sva4-1312", self.ids("categories"))

    def test_lane_with_a_failed_unit_does_not_prune(self):
        sva = bot.Site("sva1", "Zulassung", bot.ROOT_URL, "1", order=1)
        bad = bot.Site("sva2", "Führerschein", bot.ROOT_URL, "2", order=2)
        def open_flow(site):
            if site is bad:
                raise RuntimeError("down")
            return object(), []
        checker = web.Checker(60, lanes={"t": (lambda: [sva, bad], "sva*")})
        with mock.patch.object(bot, "open_flow", side_effect=open_flow), \
             mock.patch.object(bot, "check_round", return_value=iter([])):
            checker.run_lane("t")
        self.assertIn("sva4-1312", self.ids("categories"))


class BackoffTests(unittest.TestCase):
    def test_pause_doubles_while_failing_and_is_capped(self):
        self.assertEqual([web.backoff(60, n) for n in range(6)], [60, 120, 240, 480, 900, 900])

    def test_last_error_does_not_repeat_the_lane_name(self):
        checker = web.Checker(60, lanes={"Ausländeramt": (lambda: [], "[0-9]*"), "Straßenverkehrsamt": (lambda: [], "sva*")})
        checker.state["Ausländeramt"]["error"] = "Ausländeramt: Read timed out"
        checker.state["Straßenverkehrsamt"]["error"] = "504 Server Error"
        self.assertEqual(checker.last_error, "Ausländeramt: Read timed out; Straßenverkehrsamt: 504 Server Error")

    def test_status_reports_the_last_successful_check(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        with mock.patch.object(web, "DB_PATH", Path(tmp.name) / "t.db"):
            web.init_db()
            web.record(CAT, res("2026-11-01"))
            web.app.config["CHECKER"] = web.Checker(60, lanes={"x": (lambda: [], "*")})
            data = web.app.test_client().get("/api/status").get_json()
        self.assertIsNotNone(data["last_ok"])
        self.assertIsNone(data["error"])


class RedirectTests(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()

    def get(self, path, host, public):
        with mock.patch.dict(os.environ, {"PUBLIC_URL": public}):
            return self.client.get(path, headers={"Host": host})

    def test_old_tailscale_links_go_to_the_domain_with_path_and_query(self):
        r = self.get("/unsubscribe?token=abc", "better-termin.tail015e29.ts.net", "https://bettertermin.com")
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r.headers["Location"], "https://bettertermin.com/unsubscribe?token=abc")

    def test_no_redirect_on_the_domain_itself_or_before_switching(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        with mock.patch.object(web, "DB_PATH", Path(tmp.name) / "t.db"):
            web.init_db()
            self.assertEqual(self.get("/privacy", "bettertermin.com", "https://bettertermin.com").status_code, 200)
            self.assertEqual(self.get("/privacy", "x.tail015e29.ts.net", "https://x.tail015e29.ts.net").status_code, 200)

    def test_www_goes_to_the_bare_domain(self):
        r = self.get("/privacy", "www.bettertermin.com", "https://bettertermin.com")
        self.assertEqual(r.headers["Location"], "https://bettertermin.com/privacy")

    def test_client_ip_trusts_cloudflare_only_on_the_public_domain(self):
        with mock.patch.dict(os.environ, {"PUBLIC_URL": "https://bettertermin.com"}):
            with web.app.test_request_context("/", headers={"Host": "bettertermin.com", "CF-Connecting-IP": "1.2.3.4"}):
                self.assertEqual(web.client_ip(), "1.2.3.4")
            with web.app.test_request_context("/", headers={"Host": "x.tail015e29.ts.net", "CF-Connecting-IP": "1.2.3.4"},
                                              environ_base={"REMOTE_ADDR": "5.6.7.8"}):
                self.assertEqual(web.client_ip(), "5.6.7.8")

    def test_form_posts_on_the_old_address_are_not_redirected(self):
        with mock.patch.dict(os.environ, {"PUBLIC_URL": "https://bettertermin.com"}):
            r = self.client.post("/unsubscribe", data={"token": "x"}, headers={"Host": "better-termin.tail015e29.ts.net"})
        self.assertNotEqual(r.status_code, 301)


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
