#!/usr/bin/env python3
"""Städteregion Aachen appointment tracker.

Tracks the earliest free appointment for every category (Anliegen) of the
Ausländeramt (https://termine.staedteregion-aachen.de/auslaenderamt/) and the
Straßenverkehrsamt units (https://termine.staedteregion-aachen.de/), and sends
a notification when an earlier slot opens up.

The bot only reads; it never books an appointment.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT_URL = "https://termine.staedteregion-aachen.de/"   # Straßenverkehrsamt units
BASE_URL = ROOT_URL + "auslaenderamt/"
MANDANT = "1"  # "Ausländer- und Staatsangehörigkeitsbehörde"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
STATE_FILE = Path(__file__).with_name("state.json")
REQUEST_DELAY = 1.0  # seconds between categories, to be polite to the server
TIMEOUT = 60


@dataclass(frozen=True)
class Site:
    """One TEVIS booking unit ("Funktionseinheit"), e.g. the Ausländeramt."""
    key: str      # prefix for category ids; "" keeps the original Ausländeramt ids
    office: str   # shown in the UI
    base_url: str
    md: str       # unit number in the TEVIS URLs (md / mdt)
    order: int = 0

    def category_id(self, cnc: str) -> str:
        return f"{self.key}-{cnc}" if self.key else cnc

    @property
    def booking_url(self) -> str:
        return f"{self.base_url}select2?md={self.md}"


AUSLAENDERAMT = Site("", "Ausländeramt", BASE_URL, MANDANT)


@dataclass
class Category:
    id: str
    name: str
    group: str
    site: Site = AUSLAENDERAMT
    cnc: str = ""   # the unit's own id for this category; defaults to id

    def __post_init__(self):
        if not self.cnc:
            self.cnc = self.id


@dataclass
class Slot:
    date: str  # YYYY-MM-DD
    time: str  # HH:MM


@dataclass
class Result:
    category: Category
    location: str
    loc_id: str = ""
    earliest: Slot | None = None
    error: str | None = None


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse_categories(page: str, site: Site = AUSLAENDERAMT) -> list[Category]:
    """Extract all categories from the step-2 page (select2?md=...).

    Most units group their categories under accordion headers; some list them
    flat, in which case the unit's name is used as the group.
    """
    soup = BeautifulSoup(page, "html.parser")

    def category(inp, group: str) -> Category:
        cnc = inp["name"].removeprefix("cnc-")
        hidden = soup.find("input", id=f"cncSelected_str-{cnc}")
        name = cnc
        if hidden is not None:
            m = re.match(r"Anliegen (.*) ausgewählt", hidden["value"])
            name = m.group(1).strip() if m else hidden["value"]
        return Category(site.category_id(cnc), html.unescape(name), group, site, cnc)

    categories = []
    headers = soup.select('h3[id^="header_concerns_accordion-"]')
    for header in headers:
        panel = header.find_next_sibling()
        if panel is not None:
            group = header.get_text(strip=True)
            categories += [category(inp, group) for inp in panel.select('input[name^="cnc-"]')]
    if not headers:
        categories = [category(inp, site.office) for inp in soup.select('input[name^="cnc-"]')]
    return categories


def parse_locations(page: str) -> list[dict[str, str]]:
    """Extract the location forms from the step-3 page (location?...)."""
    soup = BeautifulSoup(page, "html.parser")
    locations: dict[str, dict[str, str]] = {}
    for form in soup.find_all("form", method="post"):
        loc = form.find("input", attrs={"name": "loc"})
        if loc is None:
            continue
        data = {i["name"]: i.get("value", "") for i in form.find_all("input", attrs={"name": True})}
        locations.setdefault(loc["value"], data)
    return list(locations.values())


def parse_location_name(page: str) -> str:
    m = re.search(r'id="loc_details_hidden"[^>]*value="([^"]*)"', page)
    if not m:
        return ""
    return html.unescape(m.group(1)).replace("<br/>", ", ")


FIRST_SLOT_RE = re.compile(r'<form[^>]*class="suggestion_form".*?</form>', re.S)


def parse_first_slot(page: str) -> Slot | None:
    """Return the earliest bookable slot on the step-4 page (suggest).

    Every bookable time is a <form class="suggestion_form"> with a hidden
    `date` input (YYYYMMDD) and a submit button whose title is the time;
    taken times are disabled buttons. Days are listed in date order, so the
    first form is the earliest slot.
    """
    m = FIRST_SLOT_RE.search(page)
    if not m:
        return None
    d = re.search(r'name="date" value="(\d{8})"', m.group(0))
    t = re.search(r'<button[^>]*type="submit"[^>]*title="([^"]+)"', m.group(0))
    if not d or not t:
        return None
    d = d.group(1)
    return Slot(f"{d[:4]}-{d[4:6]}-{d[6:8]}", t.group(1).strip())


def read_until_first_slot(resp: requests.Response) -> str:
    """Read a streamed suggest page only up to its first bookable slot.

    Pages with many free slots are several MB; we only need the head.
    """
    resp.encoding = resp.encoding or "utf-8"
    page = ""
    try:
        for chunk in resp.iter_content(chunk_size=32768, decode_unicode=True):
            page += chunk
            if FIRST_SLOT_RE.search(page, max(0, len(page) - len(chunk) - 4096)):
                break
    finally:
        resp.close()
    return page


def page_title(page: str) -> str:
    m = re.search(r"<h1>(.*?)</h1>", page, re.S)
    return re.sub(r"<[^>]+>|\s+", " ", m.group(1)).strip() if m else ""


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #


def new_session(base_url: str = BASE_URL) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"})
    s.get(base_url, timeout=TIMEOUT).raise_for_status()
    return s


# Units on the root page that aren't tracked: the test unit, and the special
# permits (Taxi, Güterkraftverkehr, rote Kennzeichen), which always have slots.
SKIPPED_UNITS = ("test", "ausnahmegenehmigung")


def strassenverkehrsamt_sites() -> list[Site]:
    """The Straßenverkehrsamt units listed on the root page, minus SKIPPED_UNITS."""
    r = new_session(ROOT_URL).get(ROOT_URL, timeout=TIMEOUT)
    r.raise_for_status()
    units = re.findall(r"select2\?md=(\d+)&#039;\" name=\"([^\"]+)\"", r.text)
    return [Site(f"sva{md}", html.unescape(name), ROOT_URL, md, order=int(md))
            for md, name in units if not any(skip in name.lower() for skip in SKIPPED_UNITS)]


def all_sites() -> list[Site]:
    return [AUSLAENDERAMT, *strassenverkehrsamt_sites()]


def open_flow(site: Site = AUSLAENDERAMT) -> tuple[requests.Session, list[Category]]:
    """Start a booking-flow session for a unit and read its category list.

    The category page (select2) is up to ~600 KB, so all checks of a unit in a
    round share one session and fetch it once instead of once per category.
    """
    s = new_session(site.base_url)
    r = s.get(site.base_url + "select2", params={"md": site.md}, timeout=TIMEOUT)
    r.raise_for_status()
    categories = parse_categories(r.text, site)
    if not categories:
        raise RuntimeError(f"Category list not found for {site.office} - the site structure may have changed.")
    return s, categories


def fetch_categories() -> list[Category]:
    return [c for site in all_sites() for c in open_flow(site)[1]]


def check_category(category: Category, session: requests.Session | None = None) -> list[Result]:
    """Walk the booking flow for one category; one Result per location.

    Pass the session from open_flow() to reuse it; without one, a new flow is started.
    """
    site = category.site
    s = session or open_flow(site)[0]
    params = {"mdt": site.md, "select_cnc": "1", f"cnc-{category.cnc}": "1"}
    r = s.get(site.base_url + "location", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    location_url = r.url

    locations = parse_locations(r.text)
    if not locations:
        # Some categories may skip the location step and land directly on suggest.
        if "suggest" in r.url:
            return [Result(category, parse_location_name(r.text), earliest=parse_first_slot(r.text))]
        return [Result(category, "", error=f"No location found: {page_title(r.text)}")]

    results = []
    for i, form in enumerate(locations):
        if i > 0:  # back to the location step for the next location
            s.get(location_url, timeout=TIMEOUT).raise_for_status()
        data = {k: v for k, v in form.items() if k in ("loc", "gps_lat", "gps_long")}
        data["select_location"] = "Weiter"
        r = s.post(location_url, data=data, timeout=TIMEOUT, stream=True)
        r.raise_for_status()
        page = read_until_first_slot(r)
        title = page_title(page)
        name = parse_location_name(page) or f"loc {form['loc']}"
        if "Fehler" in title:
            results.append(Result(category, name, form["loc"], error=title))
        else:
            results.append(Result(category, name, form["loc"], parse_first_slot(page)))
    return results


# --------------------------------------------------------------------------- #
# State & notifications
# --------------------------------------------------------------------------- #


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def fmt_date(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d:%d.%m.%Y} ({WEEKDAYS[d.weekday()]})"


def notify(title: str, message: str) -> None:
    print(f"\n🔔 {title}\n{message}\n", flush=True)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": f"{title}\n\n{message}\n\n{BASE_URL}"},
                timeout=30,
            ).raise_for_status()
        except requests.RequestException as e:
            print(f"Could not send Telegram notification: {e}", file=sys.stderr)

    if sys.platform == "darwin" and not os.environ.get("NO_MAC_NOTIFY"):
        script = f"display notification {json.dumps(message[:200])} with title {json.dumps(title)} sound name \"Glass\""
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def diff_and_notify(results: list[Result], state: dict, first_run: bool) -> dict:
    """Compare earliest dates with the previous run and notify on improvements."""
    new_state = dict(state)
    improvements = []
    for res in results:
        key = f"{res.category.id}@{res.loc_id}"
        if res.error:
            continue  # keep the previous value on transient errors
        earliest = res.earliest
        prev = state.get(key, {}).get("earliest")
        current = f"{earliest.date} {earliest.time}" if earliest else None
        new_state[key] = {
            "category": res.category.name,
            "location": res.location,
            "earliest": current,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        if current and (prev is None or current < prev) and not first_run:
            was = f"(was: {prev})" if prev else "(no free slots before)"
            improvements.append(
                f"• {res.category.name}: {fmt_date(earliest.date)} {earliest.time} {was}"
            )
    if improvements:
        notify("Aachen Ausländeramt: earlier appointment!", "\n".join(improvements))
    return new_state


def print_table(results: list[Result]) -> None:
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    print(f"\n=== {now} — earliest appointments ===")
    width = max((len(r.category.name) for r in results), default=20)
    for res in sorted(results, key=lambda r: (r.earliest is None, r.earliest.date if r.earliest else "")):
        if res.error:
            info = f"ERROR: {res.error}"
        elif res.earliest:
            info = f"{fmt_date(res.earliest.date)} {res.earliest.time}"
        else:
            info = "no free slots"
        print(f"  [{res.category.id}] {res.category.name:<{width}}  {info}")
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def check_round(categories: list[Category], session: requests.Session | None):
    """Yield (category, results) for each category, sharing one flow session.

    If the shared session stops working (expired, or the site lost track of it),
    the category is retried once on a fresh session.
    """
    for i, cat in enumerate(categories):
        if i:
            time.sleep(REQUEST_DELAY)
        try:
            if session is None:
                session = open_flow(cat.site)[0]
            results = check_category(cat, session)
            if any(r.error for r in results):
                session = open_flow(cat.site)[0]
                results = check_category(cat, session)
        except (requests.RequestException, RuntimeError) as e:
            session = None
            results = [Result(cat, "", error=str(e))]
        yield cat, results


def run_once(category_filter: set[str] | None) -> list[Result]:
    results: list[Result] = []
    # Progress goes to stderr so --json output on stdout stays clean.
    tty = sys.stderr.isatty()
    for site in all_sites():
        session, categories = open_flow(site)
        if category_filter:
            categories = [c for c in categories if c.id in category_filter]
        for i, (cat, cat_results) in enumerate(check_round(categories, session)):
            line = f"Checked {site.office} [{i + 1}/{len(categories)}] {cat.name}"
            print(f"\r\033[K{line[:100]}" if tty else line, end="" if tty else "\n", file=sys.stderr, flush=True)
            results.extend(cat_results)
    if tty:
        print("\r\033[K", end="", file=sys.stderr, flush=True)
    return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true", help="list categories and their IDs")
    p.add_argument("--once", action="store_true", help="check once and exit")
    p.add_argument("--interval", type=int, default=300, help="check interval in seconds (default 300)")
    p.add_argument("--categories", help="only these category IDs, comma-separated (e.g. 344,346)")
    p.add_argument("--json", action="store_true", help="print results as JSON (with --once)")
    p.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    args = p.parse_args()

    if args.test_notify:
        notify("Better Termin", "Test notification — notifications are working ✅")
        return

    if args.list:
        for c in fetch_categories():
            print(f"{c.id:>10}  {c.site.office[:28]:<28} {c.group[:28]:<28} {c.name}")
        return

    category_filter = {c.strip() for c in args.categories.split(",")} if args.categories else None
    interval = max(args.interval, 60)  # don't hammer the server

    while True:
        state = load_state()
        try:
            results = run_once(category_filter)
        except (requests.RequestException, RuntimeError) as e:
            print(f"Check failed: {e}", file=sys.stderr)
            results = None

        if results is not None:
            if args.json:
                print(json.dumps(
                    [{**asdict(r.category), "location": r.location, "error": r.error,
                      "earliest": asdict(r.earliest) if r.earliest else None} for r in results],
                    ensure_ascii=False, indent=2))
            else:
                print_table(results)
            save_state(diff_and_notify(results, state, first_run=not state))

        if args.once:
            break
        next_check = datetime.fromtimestamp(time.time() + interval).strftime("%H:%M:%S")
        print(f"Next check: {next_check} (Ctrl+C to stop)", file=sys.stderr, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
