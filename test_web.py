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
            c.execute("INSERT INTO categories VALUES ('344','RWTH Studenten','RWTH',0)")
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
            c.execute("INSERT INTO categories VALUES ('344','RWTH Studenten','RWTH',0)")
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
