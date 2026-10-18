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
import re
import secrets
import sqlite3
import statistics
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_from_directory

import termin_bot as bot

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "termin.db"
MIN_REFRESH_GAP = 60  # seconds; "Check now" can't hammer the site

# Services shown as links only. The city's Bürgerservice booking site
# (SmartCJM) sits behind a Cloudflare bot challenge, so it can't be tracked.
LINK_ONLY_GROUPS = [
    {
        "name": "Meldeangelegenheiten",
        "theme": "yellow",
        "items": [
            {
                "id": "melde-wohnsitz",
                "name": "Wohnsitz an-/ab-/ummelden",
                "location": "Bürgerservice Stadt Aachen",
                "url": "https://stadt-aachen.saas.smartcjm.com/m/buergerservice/extern/calendar/"
                       "?uid=15940648-b483-46d9-819e-285707f1fc34",
                "note": "Live tracking isn't available: the city's booking site blocks automated checks.",
                "release": "Extra same-day slots are released daily around 07:45 (according to aachen.de).",
            },
        ],
    },
]

app = Flask(__name__, static_folder=None)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY, name TEXT, grp TEXT, position INTEGER
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
    token TEXT NOT NULL UNIQUE,
    created_at TEXT,
    last_notified_at TEXT,
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
        c.execute("INSERT OR IGNORE INTO meta VALUES ('tracking_since', ?)", (now_iso(),))


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Checking
# --------------------------------------------------------------------------- #


class Checker:
    """Runs a full check of all categories every `interval` seconds."""

    def __init__(self, interval: int):
        self.interval = interval
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.progress = {"running": False, "done": 0, "total": 0, "current": None}
        self.last_check: str | None = None
        self.next_check: str | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def request_refresh(self) -> bool:
        with self.lock:
            if self.progress["running"]:
                return False
            if self.last_check and (datetime.now() - datetime.fromisoformat(self.last_check)).total_seconds() < MIN_REFRESH_GAP:
                return False
        self.wake.set()
        return True

    def _loop(self) -> None:
        while True:
            try:
                self.run_once()
                self.last_error = None
            except (requests.RequestException, RuntimeError) as e:
                self.last_error = str(e)
                print(f"Check failed: {e}", file=sys.stderr)
            finally:
                with self.lock:
                    self.progress["running"] = False
            self.next_check = (datetime.now() + timedelta(seconds=self.interval)).isoformat(timespec="seconds")
            self.wake.wait(self.interval)
            self.wake.clear()

    def run_once(self) -> None:
        categories = bot.fetch_categories()
        with db() as c:
            for pos, cat in enumerate(categories):
                c.execute(
                    "INSERT INTO categories VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE "
                    "SET name=excluded.name, grp=excluded.grp, position=excluded.position",
                    (cat.id, cat.name, cat.group, pos),
                )
        with self.lock:
            self.progress = {"running": True, "done": 0, "total": len(categories), "current": None}

        for i, cat in enumerate(categories):
            if i:
                time.sleep(bot.REQUEST_DELAY)
            self.progress["current"] = cat.name
            try:
                results = bot.check_category(cat)
            except requests.RequestException as e:
                results = [bot.Result(cat, "", error=str(e))]
            record(cat, results)
            self.progress["done"] = i + 1

        self.last_check = now_iso()


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


def send_email(to: str, subject: str, body: str) -> None:
    """Placeholder: e-mail delivery will be wired up together with hosting.

    Replace this with an SMTP / provider call (SendGrid, SES, Mailgun, ...).
    """
    print(f"[e-mail not configured yet] {to}: {subject}", file=sys.stderr)


def notify_subscribers(cat: bot.Category, earliest: str, kind: str) -> None:
    date, hhmm = earliest.split()
    what = "a slot opened" if kind == "opened" else "an earlier slot opened"
    subject = f"{cat.name}: {what} — {bot.fmt_date(date)} {hhmm}"
    with db() as c:
        subs = c.execute("SELECT * FROM alerts WHERE cat_id=?", (cat.id,)).fetchall()
        for sub in subs:
            body = (
                f"{cat.name}: {what}.\n\nEarliest appointment: {bot.fmt_date(date)} {hhmm}\n\n"
                f"Book it here: {bot.BASE_URL}\n\n"
                f"Remove this alert: {request_base()}unsubscribe?token={sub['token']}\n"
            )
            send_email(sub["email"], subject, body)
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
    return send_from_directory(ROOT / "static", "index.html")


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
        "booking_url": bot.BASE_URL + "select2?md=" + bot.MANDANT,
        "categories": cats,
        "link_only_groups": LINK_ONLY_GROUPS,
    })


@app.post("/api/refresh")
def api_refresh():
    started = app.config["CHECKER"].request_refresh()
    return jsonify({"started": started}), (202 if started else 429)


@app.get("/api/alerts")
def api_alerts_list():
    email = (request.args.get("email") or "").strip().lower()
    with db() as c:
        rows = c.execute("SELECT cat_id FROM alerts WHERE email=?", (email,)).fetchall()
    return jsonify([r["cat_id"] for r in rows])


@app.post("/api/alerts")
def api_alerts_create():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    cat_id = str(data.get("category_id", ""))
    if not EMAIL_RE.match(email) or len(email) > 254:
        return jsonify({"error": "Please enter a valid e-mail address."}), 400
    with db() as c:
        if not c.execute("SELECT 1 FROM categories WHERE id=?", (cat_id,)).fetchone():
            return jsonify({"error": "Unknown category."}), 400
        c.execute(
            "INSERT OR IGNORE INTO alerts (email, cat_id, token, created_at) VALUES (?,?,?,?)",
            (email, cat_id, secrets.token_urlsafe(24), now_iso()),
        )
    return jsonify({"ok": True}), 201


@app.delete("/api/alerts")
def api_alerts_delete():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    with db() as c:
        c.execute("DELETE FROM alerts WHERE email=? AND cat_id=?", (email, str(data.get("category_id", ""))))
    return jsonify({"ok": True})


@app.get("/unsubscribe")
def unsubscribe():
    with db() as c:
        n = c.execute("DELETE FROM alerts WHERE token=?", (request.args.get("token", ""),)).rowcount
    msg = "Alert removed." if n else "Alert not found (it may already have been removed)."
    return f"<!doctype html><meta charset=utf-8><title>Alert</title><p style='font-family:sans-serif'>{msg}</p>"


def main() -> None:
    p = argparse.ArgumentParser(description="Better Termin web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--interval", type=int, default=300, help="check interval in seconds (min 60)")
    args = p.parse_args()

    init_db()
    checker = Checker(max(args.interval, 60))
    with db() as c:  # survive restarts: show the last check from history
        checker.last_check = c.execute("SELECT MAX(ok_at) FROM status").fetchone()[0]
    app.config["CHECKER"] = checker
    app.config["PUBLIC_URL"] = f"http://{args.host}:{args.port}/"
    checker.start()
    print(f"UI: http://{args.host}:{args.port}", flush=True)
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
