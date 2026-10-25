#!/usr/bin/env python3
"""Better Termin — web UI.

Checks all categories regularly in the background, stores each category's
earliest appointment and its history in SQLite, and shows in the browser:

  * the earliest slot when one is available,
  * otherwise how often new slots open (as observed since tracking started),
  * an e-mail alert sign-up per category.

Run:  .venv/bin/python web.py   →  http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import statistics
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

import requests
from flask import Flask, jsonify, redirect, render_template, request

import termin_bot as bot

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "termin.db"
MIN_REFRESH_GAP = 60  # seconds; "Check now" can't hammer the site
MAX_BACKOFF = 15 * 60  # longest pause while the booking site keeps failing
CONFIRM_TTL = timedelta(hours=48)  # unconfirmed sign-ups are deleted after this
RESEND_GAP = timedelta(minutes=10)  # min gap between confirmation mails to one address
SIGNUPS_PER_IP_PER_HOUR = 10

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # requests are small JSON bodies or form posts
# Don't write visitors' IP addresses to the request log (see the privacy policy).
logging.getLogger("werkzeug").setLevel(logging.WARNING)

CSP = "; ".join([
    "default-src 'self'", "script-src 'self'", "style-src 'self'", "img-src 'self' data:",
    "font-src 'self'", "connect-src 'self'", "object-src 'none'", "base-uri 'none'",
    "form-action 'self'", "frame-ancestors 'none'",
])


@app.after_request
def security_headers(resp):
    h = resp.headers
    h["Content-Security-Policy"] = CSP
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    # Not "no-referrer": with it, browsers send "Origin: null" on same-site form posts,
    # which the cross-site check below would block. Other sites still get no Referer,
    # so tokens in /confirm links don't leak.
    h["Referrer-Policy"] = "same-origin"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    if request.is_secure:
        h["Strict-Transport-Security"] = "max-age=31536000"
    if request.path.startswith("/api/") or request.path in ("/confirm", "/unsubscribe"):
        h["Cache-Control"] = "no-store"
    return resp


def allowed_origins() -> set[str]:
    origins = {request.host_url.rstrip("/")}
    if os.environ.get("PUBLIC_URL"):
        origins.add(os.environ["PUBLIC_URL"].rstrip("/"))
    return origins


@app.before_request
def redirect_old_address():
    """Send visitors of the old Tailscale address to the public domain.

    Alert and confirmation e-mails sent before the domain existed link to the
    *.ts.net address; they keep working and land on the same page there.
    """
    public = os.environ.get("PUBLIC_URL", "").rstrip("/")
    if (request.method in ("GET", "HEAD") and request.host.endswith(".ts.net")
            and public and not public.split("://")[-1].endswith(".ts.net")):
        target = public + request.full_path.rstrip("?")
        return redirect(target, 301)


@app.before_request
def block_cross_site_writes():
    """CSRF defense: state-changing requests must come from this site's own pages."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin")
        if origin and origin not in allowed_origins():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Cross-site request blocked."}), 403
            return message("Request blocked", "This request didn't come from this site. "
                           "Please open the link from your e-mail again."), 403


def load_env(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader (KEY=value lines); real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def operator() -> dict:
    """Who runs this site, for the imprint and privacy policy (from .env)."""
    address = os.environ.get("OPERATOR_ADDRESS", "")
    op = {
        "name": os.environ.get("OPERATOR_NAME", ""),
        "address_lines": [l.strip() for l in address.split("|") if l.strip()],
        "email": os.environ.get("OPERATOR_EMAIL", ""),
        "hosting": os.environ.get("HOSTING_PROVIDER", ""),
        "mail": os.environ.get("EMAIL_PROVIDER", ""),
    }
    op["configured"] = bool(op["name"] and op["address_lines"] and op["email"])
    return op


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY, name TEXT, grp TEXT, position INTEGER,
    office TEXT, booking_url TEXT
);
CREATE TABLE IF NOT EXISTS status (
    cat_id TEXT PRIMARY KEY,
    earliest TEXT,            -- 'YYYY-MM-DD HH:MM' or NULL when no slot
    location TEXT,
    checked_at TEXT,
    ok_at TEXT,               -- last successful check; events need a previous one
    error TEXT,
    available_since TEXT      -- when the current run of availability began
);
-- kind: 'opened' (none -> some), 'closed' (some -> none), 'earlier' (date moved earlier)
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, cat_id TEXT, ts TEXT, kind TEXT, earliest TEXT
);
CREATE INDEX IF NOT EXISTS events_cat ON events (cat_id, ts);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL,
    cat_id TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,   -- manage/unsubscribe token (browser + every alert mail)
    created_at TEXT,
    last_notified_at TEXT,
    confirm_token TEXT UNIQUE,    -- double opt-in; only sent by e-mail, cleared on confirm
    confirmed_at TEXT,
    confirm_sent_at TEXT,
    UNIQUE (email, cat_id)
);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        # older databases: add the double opt-in columns
        cols = {r["name"] for r in c.execute("PRAGMA table_info(alerts)")}
        for col in ("confirm_token", "confirmed_at", "confirm_sent_at"):
            if col not in cols:
                c.execute(f"ALTER TABLE alerts ADD COLUMN {col} TEXT")
        # older databases: categories from several offices
        cols = {r["name"] for r in c.execute("PRAGMA table_info(categories)")}
        for col in ("office", "booking_url"):
            if col not in cols:
                c.execute(f"ALTER TABLE categories ADD COLUMN {col} TEXT")
        c.execute("INSERT OR IGNORE INTO meta VALUES ('tracking_since', ?)", (now_iso(),))


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Checking
# --------------------------------------------------------------------------- #


# Each lane loops on its own, so the Straßenverkehrsamt (about 85 categories)
# doesn't slow down the Ausländeramt checks.
# Each lane: (units to check, GLOB matching the ids of its categories).
LANES = {
    "Ausländeramt": (lambda: [bot.AUSLAENDERAMT], "[0-9]*"),
    "Straßenverkehrsamt": (bot.strassenverkehrsamt_sites, "sva*"),
}


class Checker:
    """Checks every category, pausing `interval` seconds between rounds of each lane."""

    def __init__(self, interval: int, lanes: dict | None = None):
        self.interval = interval
        self.lanes = lanes or LANES
        self.lock = threading.Lock()
        self.wakes = {name: threading.Event() for name in self.lanes}
        self.state = {name: {"running": False, "done": 0, "total": 0, "current": None,
                             "next": None, "error": None, "failures": 0} for name in self.lanes}
        self.last_check: str | None = None

    def start(self) -> None:
        for name in self.lanes:
            threading.Thread(target=self._loop, args=(name,), daemon=True).start()

    @property
    def progress(self) -> dict:
        with self.lock:
            running = [lane for lane in self.state.values() if lane["running"]]
            return {
                "running": bool(running),
                "done": sum(lane["done"] for lane in running),
                "total": sum(lane["total"] for lane in running),
                "current": running[-1]["current"] if running else None,
            }

    @property
    def next_check(self) -> str | None:
        upcoming = [lane["next"] for lane in self.state.values() if lane["next"] and not lane["running"]]
        return min(upcoming) if upcoming else None

    @property
    def last_error(self) -> str | None:
        errors = []
        for name, lane in self.state.items():
            if lane["error"]:
                err = lane["error"]
                errors.append(err if err.startswith(f"{name}:") else f"{name}: {err}")
        return "; ".join(errors) or None

    def request_refresh(self) -> bool:
        with self.lock:
            idle = [name for name, lane in self.state.items() if not lane["running"]]
            if not idle:
                return False
            if self.last_check and (datetime.now() - datetime.fromisoformat(self.last_check)).total_seconds() < MIN_REFRESH_GAP:
                return False
        for name in idle:
            self.wakes[name].set()
        return True

    def _loop(self, name: str) -> None:
        lane = self.state[name]
        while True:
            try:
                delete_unconfirmed()
                lane["error"] = self.run_lane(name)
            except (requests.RequestException, RuntimeError) as e:
                lane["error"] = str(e)
            finally:
                with self.lock:
                    lane["running"] = False
            if lane["error"]:
                print(f"Check failed ({name}): {lane['error']}", file=sys.stderr)
            lane["failures"] = lane["failures"] + 1 if lane["error"] else 0
            pause = backoff(self.interval, lane["failures"])
            lane["next"] = (datetime.now() + timedelta(seconds=pause)).isoformat(timespec="seconds")
            self.wakes[name].wait(pause)
            self.wakes[name].clear()

    def run_lane(self, name: str) -> str | None:
        """One round over the lane's units; returns an error message for units that failed."""
        sites_fn, id_glob = self.lanes[name]
        flows, errors = [], []
        for site in sites_fn():
            try:
                flows.append(bot.open_flow(site))
            except (requests.RequestException, RuntimeError) as e:
                errors.append(f"{site.office}: {e}")
        with db() as c:
            for _, categories in flows:
                for pos, cat in enumerate(categories):
                    c.execute(
                        "INSERT INTO categories (id, name, grp, position, office, booking_url) VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, grp=excluded.grp, "
                        "position=excluded.position, office=excluded.office, booking_url=excluded.booking_url",
                        (cat.id, cat.name, cat.group, cat.site.order * 1000 + pos, cat.site.office, cat.site.booking_url),
                    )
        lane = self.state[name]
        with self.lock:
            lane.update(running=True, done=0, total=sum(len(cats) for _, cats in flows), current=None)

        for session, categories in flows:
            for cat, results in bot.check_round(categories, session):
                record(cat, results)
                lane["current"] = cat.name
                lane["done"] += 1

        if flows:  # "last check" means the booking site actually answered
            self.last_check = now_iso()
        if not errors:  # only after a complete round, so a site hiccup never removes anything
            prune_categories(id_glob, {cat.id for _, cats in flows for cat in cats})
        return "; ".join(errors) or None


def backoff(interval: int, failures: int) -> int:
    """Pause before the next round: doubles with each failed round in a row, so a
    struggling booking site isn't hit every minute, and resets after a good round."""
    return min(interval * 2 ** failures, max(interval, MAX_BACKOFF))


def prune_categories(id_glob: str, current: set[str]) -> list[str]:
    """Remove a lane's categories the sites no longer list (or we no longer track).

    Categories someone has an alert for are always kept.
    """
    with db() as c:
        stale = [r[0] for r in c.execute(
            "SELECT id FROM categories WHERE id GLOB ? AND id NOT IN (SELECT cat_id FROM alerts)", (id_glob,))
            if r[0] not in current]
        for cat_id in stale:
            for table, col in (("categories", "id"), ("status", "cat_id"), ("events", "cat_id")):
                c.execute(f"DELETE FROM {table} WHERE {col}=?", (cat_id,))
    return stale


def record(cat: bot.Category, results: list[bot.Result]) -> None:
    """Store one category's check result and derive opened/closed/earlier events."""
    ok = [r for r in results if not r.error]
    ts = now_iso()
    with db() as c:
        prev = c.execute("SELECT * FROM status WHERE cat_id=?", (cat.id,)).fetchone()
        if not ok:
            # transient failure: keep the last known earliest date
            err = "; ".join(r.error for r in results if r.error)
            if prev:
                c.execute("UPDATE status SET error=?, checked_at=? WHERE cat_id=?", (err, ts, cat.id))
            else:
                c.execute("INSERT INTO status (cat_id, checked_at, error) VALUES (?,?,?)", (cat.id, ts, err))
            return

        with_slot = [r for r in ok if r.earliest]
        best = min(with_slot, key=lambda r: (r.earliest.date, r.earliest.time)) if with_slot else None
        earliest = f"{best.earliest.date} {best.earliest.time}" if best else None
        location = (best or ok[0]).location

        kind = None
        if prev is not None and prev["ok_at"]:
            before = prev["earliest"]
            if before is None and earliest:
                kind = "opened"
            elif before and earliest is None:
                kind = "closed"
            elif before and earliest and earliest < before:
                kind = "earlier"

        # Only known when we saw it open; on the first observation it's unknown.
        available_since = None
        if earliest and prev is not None and prev["ok_at"]:
            available_since = prev["available_since"] if prev["earliest"] else ts

        c.execute(
            "INSERT INTO status VALUES (?,?,?,?,?,NULL,?) ON CONFLICT(cat_id) DO UPDATE SET "
            "earliest=excluded.earliest, location=excluded.location, checked_at=excluded.checked_at, "
            "ok_at=excluded.ok_at, error=NULL, available_since=excluded.available_since",
            (cat.id, earliest, location, ts, ts, available_since),
        )
        if kind:
            c.execute("INSERT INTO events (cat_id, ts, kind, earliest) VALUES (?,?,?,?)", (cat.id, ts, kind, earliest))

    if kind in ("opened", "earlier"):
        notify_subscribers(cat, earliest, kind)


# --------------------------------------------------------------------------- #
# E-mail alerts
# --------------------------------------------------------------------------- #

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def smtp_configured() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))


EMAIL_LOGO = ROOT / "static" / "icons" / "icon-192.png"


def render_email(template: str, **ctx) -> tuple[str, str]:
    """Render an HTML e-mail; returns (html, logo content id)."""
    cid = make_msgid(domain="better-termin")
    html = app.jinja_env.get_template(f"email/{template}").render(
        logo_cid=cid[1:-1], site_url=request_base().rstrip("/"), **ctx)
    return html, cid


def send_email(to: str, subject: str, body: str, html: tuple[str, str] | None = None) -> None:
    """Send via SMTP (e.g. Gmail with an app password), configured in .env.

    `body` is the plain-text version; `html` is (html, logo cid) from render_email.
    The logo is embedded in the message, so no remote image is loaded.
    """
    if not smtp_configured():
        if os.environ.get("DEV_MAIL_LOG") == "1":  # local testing only: print the mail
            print(f"[dev mail] to {to}: {subject}\n{body}", file=sys.stderr, flush=True)
        return
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ["SMTP_USER"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html:
        markup, cid = html
        msg.add_alternative(markup, subtype="html")
        msg.get_payload()[1].add_related(EMAIL_LOGO.read_bytes(), "image", "png", cid=cid)
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587"))
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        with server:
            # AUTH PLAIN only: when it fails, smtplib's fallback to LOGIN makes Gmail
            # drop the connection, which hides the real error (e.g. 535 bad credentials).
            server.ehlo()
            server.user, server.password = os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"]
            server.auth("PLAIN", server.auth_plain)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        code = getattr(e, "smtp_code", "")
        print(f"Sending e-mail failed: {type(e).__name__} {code}".rstrip(), file=sys.stderr)  # no address in logs


def send_confirmation(email: str, cat_name: str, confirm_token: str) -> None:
    url = f"{request_base()}confirm?token={confirm_token}"
    send_email(
        email,
        f"Confirm your Better Termin alert: {cat_name}",
        f"Someone (hopefully you) asked for an e-mail alert on Better Termin for:\n\n  {cat_name}\n\n"
        f"To start the alert, confirm here:\n{url}\n\n"
        f"If this wasn't you, ignore this e-mail. The sign-up is deleted automatically after 48 hours.\n",
        render_email("confirm.html", title="Confirm your alert", preheader=f"Confirm your alert for {cat_name}",
                     category=cat_name, confirm_url=url),
    )


def delete_unconfirmed() -> None:
    cutoff = (datetime.now() - CONFIRM_TTL).isoformat(timespec="seconds")
    with db() as c:
        c.execute("DELETE FROM alerts WHERE confirmed_at IS NULL AND created_at < ?", (cutoff,))


_signups: dict[str, list[float]] = {}  # ip -> request times; memory only, kept for 1 hour
_signups_lock = threading.Lock()


def signup_rate_limited(ip: str) -> bool:
    now = time.time()
    with _signups_lock:
        for key in list(_signups):
            _signups[key] = [t for t in _signups[key] if now - t < 3600]
            if not _signups[key]:
                del _signups[key]
        times = _signups.setdefault(ip, [])
        if len(times) >= SIGNUPS_PER_IP_PER_HOUR:
            return True
        times.append(now)
        return False


def alert_email(cat_name: str, earliest: str, kind: str, token: str,
                booking_url: str = bot.AUSLAENDERAMT.booking_url) -> tuple[str, str, tuple[str, str]]:
    """Subject, plain-text body and HTML of the e-mail sent when a slot opens or moves earlier."""
    date, hhmm = earliest.split()
    what = "a slot opened" if kind == "opened" else "an earlier slot opened"
    subject = f"{cat_name}: {what} — {bot.fmt_date(date)} {hhmm}"
    unsubscribe_url = f"{request_base()}unsubscribe?token={token}"
    body = (
        f"{cat_name}: {what}.\n\nEarliest appointment: {bot.fmt_date(date)} {hhmm}\n\n"
        f"Book it here: {booking_url}\n\n"
        f"Remove this alert: {unsubscribe_url}\n"
    )
    day = datetime.strptime(date, "%Y-%m-%d").date()
    n = (day - datetime.now().date()).days
    html = render_email(
        "alert.html", title="A slot opened" if kind == "opened" else "An earlier slot opened",
        preheader=f"{cat_name}: {bot.fmt_date(date)} {hhmm}", category=cat_name,
        date=day.strftime("%d.%m.%Y"), weekday=bot.WEEKDAYS[day.weekday()], time=hhmm,
        days_until="today" if n <= 0 else "tomorrow" if n == 1 else f"in {n} days",
        booking_url=booking_url, unsubscribe_url=unsubscribe_url,
    )
    return subject, body, html


def notify_subscribers(cat: bot.Category, earliest: str, kind: str) -> None:
    with db() as c:
        subs = c.execute("SELECT * FROM alerts WHERE cat_id=? AND confirmed_at IS NOT NULL", (cat.id,)).fetchall()
        for sub in subs:
            send_email(sub["email"], *alert_email(cat.name, earliest, kind, sub["token"], cat.site.booking_url))
            c.execute("UPDATE alerts SET last_notified_at=? WHERE id=?", (now_iso(), sub["id"]))


def request_base() -> str:
    return app.config.get("PUBLIC_URL", "http://127.0.0.1:8000/")


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #


def category_stats(events: list[sqlite3.Row], since: datetime) -> dict:
    """Opening frequency derived from what we have observed so far."""
    opened = [datetime.fromisoformat(e["ts"]) for e in events if e["kind"] == "opened"]
    earlier = [datetime.fromisoformat(e["ts"]) for e in events if e["kind"] == "earlier"]
    cutoff = datetime.now() - timedelta(days=30)

    gaps = [(b - a).total_seconds() for a, b in zip(opened, opened[1:])]
    # how long a slot stayed available after it opened
    durations, open_at = [], None
    for e in events:
        t = datetime.fromisoformat(e["ts"])
        if e["kind"] == "opened":
            open_at = t
        elif e["kind"] == "closed" and open_at:
            durations.append((t - open_at).total_seconds())
            open_at = None

    hours = Counter(t.hour for t in opened)
    return {
        "openings": len(opened),
        "openings_30d": sum(t >= cutoff for t in opened),
        "earlier_30d": sum(t >= cutoff for t in earlier),
        "last_opened": opened[-1].isoformat() if opened else None,
        # with a single opening, the best estimate is "once since tracking started"
        "avg_interval_s": statistics.mean(gaps) if gaps else (
            (datetime.now() - since).total_seconds() if len(opened) == 1 else None),
        "typical_hour": hours.most_common(1)[0][0] if len(opened) >= 2 else None,
        "avg_open_s": statistics.mean(durations) if durations else None,
    }


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/favicon.ico")
def favicon():
    return app.send_static_file("icons/favicon.ico")


@app.get("/imprint")
def imprint():
    return render_template("imprint.html", op=operator())


@app.get("/privacy")
def privacy():
    return render_template("privacy.html", op=operator())


@app.get("/api/status")
def api_status():
    checker: Checker = app.config["CHECKER"]
    with db() as c:
        since = datetime.fromisoformat(c.execute("SELECT value FROM meta WHERE key='tracking_since'").fetchone()[0])
        rows = c.execute(
            "SELECT c.*, s.earliest, s.location, s.checked_at, s.error, s.available_since "
            "FROM categories c LEFT JOIN status s ON s.cat_id = c.id ORDER BY c.position"
        ).fetchall()
        events = c.execute("SELECT * FROM events ORDER BY ts").fetchall()
        last_ok = c.execute("SELECT MAX(ok_at) FROM status").fetchone()[0]

    by_cat: dict[str, list] = {}
    for e in events:
        by_cat.setdefault(e["cat_id"], []).append(e)

    cats = []
    for r in rows:
        earliest = None
        if r["earliest"]:
            d, t = r["earliest"].split()
            earliest = {"date": d, "time": t}
        cats.append({
            "id": r["id"], "name": r["name"], "group": r["grp"],
            "office": r["office"] or bot.AUSLAENDERAMT.office,
            "booking_url": r["booking_url"] or bot.AUSLAENDERAMT.booking_url,
            "earliest": earliest, "location": r["location"],
            "checked_at": r["checked_at"], "error": r["error"],
            "available_since": r["available_since"],
            "stats": category_stats(by_cat.get(r["id"], []), since),
        })

    return jsonify({
        "tracking_since": since.isoformat(),
        "last_check": checker.last_check,
        "next_check": checker.next_check,
        "interval": checker.interval,
        "progress": checker.progress,
        "error": checker.last_error,
        "last_ok": last_ok,
        "booking_url": bot.BASE_URL + "select2?md=" + bot.MANDANT,
        "alerts_enabled": alerts_enabled(),
        "categories": cats,
    })


@app.post("/api/refresh")
def api_refresh():
    started = app.config["CHECKER"].request_refresh()
    return jsonify({"started": started}), (202 if started else 429)


@app.get("/api/alerts")
def api_alerts_list():
    """Status of the alerts this browser created, looked up by their tokens."""
    tokens = [t for t in (request.args.get("tokens") or "").split(",") if t][:50]
    if not tokens:
        return jsonify([])
    with db() as c:
        rows = c.execute(
            f"SELECT token, cat_id, confirmed_at FROM alerts WHERE token IN ({','.join('?' * len(tokens))})", tokens
        ).fetchall()
    return jsonify([
        {"token": r["token"], "category_id": r["cat_id"], "status": "active" if r["confirmed_at"] else "pending"}
        for r in rows
    ])


def alerts_enabled() -> bool:
    # Without working e-mail delivery, collecting addresses would be pointless.
    return smtp_configured() or os.environ.get("DEV_MAIL_LOG") == "1"


@app.post("/api/alerts")
def api_alerts_create():
    if not alerts_enabled():
        return jsonify({"error": "E-mail alerts aren't available yet."}), 503
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    cat_id = str(data.get("category_id", ""))
    if not EMAIL_RE.match(email) or len(email) > 254:
        return jsonify({"error": "Please enter a valid e-mail address."}), 400
    if data.get("consent") is not True:
        return jsonify({"error": "Please accept the privacy policy to set an alert."}), 400
    if signup_rate_limited(request.remote_addr or ""):
        return jsonify({"error": "Too many sign-ups from this connection. Please try again later."}), 429

    with db() as c:
        cat = c.execute("SELECT name FROM categories WHERE id=?", (cat_id,)).fetchone()
        if not cat:
            return jsonify({"error": "Unknown category."}), 400
        row = c.execute("SELECT * FROM alerts WHERE email=? AND cat_id=?", (email, cat_id)).fetchone()
        if row and row["confirmed_at"]:
            # Don't hand out the token: whoever types an address must not be able to manage its alerts.
            return jsonify({"status": "active"}), 200
        if row:
            sent = row["confirm_sent_at"]
            if not sent or datetime.now() - datetime.fromisoformat(sent) >= RESEND_GAP:
                c.execute("UPDATE alerts SET confirm_sent_at=? WHERE id=?", (now_iso(), row["id"]))
                send_confirmation(email, cat["name"], row["confirm_token"])
            return jsonify({"status": "pending"}), 200

        token, confirm_token = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        c.execute(
            "INSERT INTO alerts (email, cat_id, token, created_at, confirm_token, confirm_sent_at) VALUES (?,?,?,?,?,?)",
            (email, cat_id, token, now_iso(), confirm_token, now_iso()),
        )
    send_confirmation(email, cat["name"], confirm_token)
    return jsonify({"status": "pending", "token": token}), 201


@app.delete("/api/alerts")
def api_alerts_delete():
    token = str((request.get_json(silent=True) or {}).get("token", ""))
    with db() as c:
        n = c.execute("DELETE FROM alerts WHERE token=?", (token,)).rowcount
    return jsonify({"ok": bool(n)}), (200 if n else 404)


def message(title: str, text: str, **form):
    return render_template("message.html", title=title, text=text, **form)


@app.route("/confirm", methods=["GET", "POST"])
def confirm():
    token = request.values.get("token", "")
    if request.method == "GET":
        return message("Confirm your alert", "Click the button to start your e-mail alert.",
                       action="/confirm", token=token, button="Confirm alert")
    cutoff = (datetime.now() - CONFIRM_TTL).isoformat(timespec="seconds")
    with db() as c:
        n = c.execute(
            "UPDATE alerts SET confirmed_at=?, confirm_token=NULL "
            "WHERE confirm_token=? AND confirmed_at IS NULL AND created_at >= ?",
            (now_iso(), token, cutoff),
        ).rowcount
    if n:
        return message("Alert confirmed", "You'll get an e-mail when a slot opens or an earlier slot appears.")
    return message("Link not valid", "This link has expired or was already used. You can set the alert again on the site.")


@app.route("/unsubscribe", methods=["GET", "POST"])
def unsubscribe():
    token = request.values.get("token", "")
    if request.method == "GET":
        return message("Remove alert", "Click the button to stop this e-mail alert and delete its data.",
                       action="/unsubscribe", token=token, button="Remove alert")
    with db() as c:
        n = c.execute("DELETE FROM alerts WHERE token=?", (token,)).rowcount
    if n:
        return message("Alert removed", "You won't get any more e-mails for this alert. Its data has been deleted.")
    return message("Alert not found", "It may already have been removed.")


def main() -> None:
    p = argparse.ArgumentParser(description="Better Termin web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--interval", type=int, default=60, help="pause between rounds in seconds (min 60)")
    args = p.parse_args()

    load_env()
    init_db()
    if not operator()["configured"]:
        print("Warning: OPERATOR_NAME / OPERATOR_ADDRESS / OPERATOR_EMAIL are not set; "
              "fill them in .env before publishing (imprint and privacy policy).", file=sys.stderr)
    checker = Checker(max(args.interval, 60))
    with db() as c:  # survive restarts: show the last check from history
        checker.last_check = c.execute("SELECT MAX(ok_at) FROM status").fetchone()[0]
    app.config["CHECKER"] = checker
    app.config["PUBLIC_URL"] = os.environ.get("PUBLIC_URL", f"http://{args.host}:{args.port}/").rstrip("/") + "/"
    checker.start()
    print(f"UI: http://{args.host}:{args.port}", flush=True)
    from waitress import serve  # production WSGI server; no access log, so no IPs logged
    proxy = {}
    if os.environ.get("TRUST_PROXY") == "1":
        # Behind exactly one local reverse proxy (the Tailscale container): take the
        # client IP, scheme and host from its X-Forwarded-* headers. Waitress drops
        # these headers from anyone else.
        proxy = dict(trusted_proxy="127.0.0.1", trusted_proxy_count=1,
                     trusted_proxy_headers="x-forwarded-for x-forwarded-host x-forwarded-proto")
    serve(app, host=args.host, port=args.port, threads=8, ident="",
          max_request_body_size=app.config["MAX_CONTENT_LENGTH"], **proxy)


if __name__ == "__main__":
    main()
