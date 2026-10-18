#!/usr/bin/env python3
"""Aachen Ausländeramt termin takip botu.

https://termine.staedteregion-aachen.de/auslaenderamt/ sitesindeki tüm
kategorilerin (Anliegen) en yakın boş termin tarihlerini takip eder ve
daha erken bir termin açıldığında bildirim gönderir.

Bot sadece okuma yapar, termin rezerve etmez.
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
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://termine.staedteregion-aachen.de/auslaenderamt/"
MANDANT = "1"  # "Ausländer- und Staatsangehörigkeitsbehörde"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
STATE_FILE = Path(__file__).with_name("state.json")
REQUEST_DELAY = 1.0  # seconds between categories, to be polite to the server
TIMEOUT = 60


@dataclass
class Category:
    id: str
    name: str
    group: str


@dataclass
class Slot:
    date: str  # YYYY-MM-DD
    time: str  # HH:MM


@dataclass
class Result:
    category: Category
    location: str
    loc_id: str = ""
    slots: list[Slot] = field(default_factory=list)
    error: str | None = None

    @property
    def earliest(self) -> Slot | None:
        return min(self.slots, key=lambda s: (s.date, s.time)) if self.slots else None


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse_categories(page: str) -> list[Category]:
    """Extract all categories from the step-2 page (select2?md=1)."""
    soup = BeautifulSoup(page, "html.parser")
    categories = []
    for header in soup.select('h3[id^="header_concerns_accordion-"]'):
        group = header.get_text(strip=True)
        panel = header.find_next_sibling()
        if panel is None:
            continue
        for inp in panel.select('input[name^="cnc-"]'):
            cnc_id = inp["name"].removeprefix("cnc-")
            hidden = soup.find("input", id=f"cncSelected_str-{cnc_id}")
            name = cnc_id
            if hidden is not None:
                m = re.match(r"Anliegen (.*) ausgewählt", hidden["value"])
                name = m.group(1).strip() if m else hidden["value"]
            categories.append(Category(cnc_id, html.unescape(name), group))
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


def parse_slots(page: str) -> list[Slot]:
    """Extract bookable slots from the step-4 page (suggest).

    Every bookable time is a <form class="suggestion_form"> with a hidden
    `date` input (YYYYMMDD) and a submit button whose title is the time.
    Disabled buttons are already taken and are ignored.
    """
    soup = BeautifulSoup(page, "html.parser")
    slots = []
    for form in soup.select("form.suggestion_form"):
        date_input = form.find("input", attrs={"name": "date"})
        button = form.find("button", attrs={"type": "submit"})
        if date_input is None or button is None:
            continue
        d = date_input["value"]
        t = (button.get("title") or button.get_text()).strip()
        slots.append(Slot(f"{d[:4]}-{d[4:6]}-{d[6:8]}", t))
    return slots


def page_title(page: str) -> str:
    m = re.search(r"<h1>(.*?)</h1>", page, re.S)
    return re.sub(r"<[^>]+>|\s+", " ", m.group(1)).strip() if m else ""


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"})
    s.get(BASE_URL, timeout=TIMEOUT).raise_for_status()
    return s


def fetch_categories() -> list[Category]:
    s = new_session()
    r = s.get(BASE_URL + "select2", params={"md": MANDANT}, timeout=TIMEOUT)
    r.raise_for_status()
    categories = parse_categories(r.text)
    if not categories:
        raise RuntimeError("Kategori listesi bulunamadı - site yapısı değişmiş olabilir.")
    return categories


def check_category(category: Category) -> list[Result]:
    """Walk the booking flow for one category; one Result per location."""
    s = new_session()
    s.get(BASE_URL + "select2", params={"md": MANDANT}, timeout=TIMEOUT).raise_for_status()
    params = {"mdt": MANDANT, "select_cnc": "1", f"cnc-{category.id}": "1"}
    r = s.get(BASE_URL + "location", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    location_url = r.url

    locations = parse_locations(r.text)
    if not locations:
        # Some categories may skip the location step and land directly on suggest.
        if "suggest" in r.url:
            return [Result(category, parse_location_name(r.text), slots=parse_slots(r.text))]
        return [Result(category, "", error=f"Standort bulunamadı: {page_title(r.text)}")]

    results = []
    for i, form in enumerate(locations):
        if i > 0:  # each location needs a fresh pass through the flow
            s = new_session()
            s.get(BASE_URL + "select2", params={"md": MANDANT}, timeout=TIMEOUT)
            s.get(location_url, timeout=TIMEOUT)
        data = {k: v for k, v in form.items() if k in ("loc", "gps_lat", "gps_long")}
        data["select_location"] = "Weiter"
        r = s.post(location_url, data=data, timeout=TIMEOUT)
        r.raise_for_status()
        title = page_title(r.text)
        name = parse_location_name(r.text) or f"loc {form['loc']}"
        if "Fehler" in title:
            results.append(Result(category, name, form["loc"], error=title))
        else:
            results.append(Result(category, name, form["loc"], parse_slots(r.text)))
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


WEEKDAYS_TR = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]


def fmt_date(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d:%d.%m.%Y} ({WEEKDAYS_TR[d.weekday()]})"


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
            print(f"Telegram bildirimi gönderilemedi: {e}", file=sys.stderr)

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
            "slot_count": len(res.slots),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        if current and (prev is None or current < prev) and not first_run:
            was = f"(önceki: {prev})" if prev else "(önceden boş termin yoktu)"
            improvements.append(
                f"• {res.category.name}: {fmt_date(earliest.date)} {earliest.time} {was}"
            )
    if improvements:
        notify("Aachen Ausländeramt: daha erken termin!", "\n".join(improvements))
    return new_state


def print_table(results: list[Result]) -> None:
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    print(f"\n=== {now} — en yakın terminler ===")
    width = max((len(r.category.name) for r in results), default=20)
    for res in sorted(results, key=lambda r: (r.earliest is None, r.earliest.date if r.earliest else "")):
        if res.error:
            info = f"HATA: {res.error}"
        elif res.earliest:
            info = f"{fmt_date(res.earliest.date)} {res.earliest.time}  ({len(res.slots)} boş saat)"
        else:
            info = "boş termin yok"
        print(f"  [{res.category.id}] {res.category.name:<{width}}  {info}")
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def run_once(category_filter: set[str] | None) -> list[Result]:
    categories = fetch_categories()
    if category_filter:
        categories = [c for c in categories if c.id in category_filter]
    results: list[Result] = []
    # Progress goes to stderr so --json output on stdout stays clean.
    tty = sys.stderr.isatty()
    for i, cat in enumerate(categories):
        if i:
            time.sleep(REQUEST_DELAY)
        line = f"Kontrol ediliyor [{i + 1}/{len(categories)}] {cat.name}"
        print(f"\r\033[K{line[:100]}" if tty else line, end="" if tty else "\n", file=sys.stderr, flush=True)
        try:
            results.extend(check_category(cat))
        except requests.RequestException as e:
            results.append(Result(cat, "", error=str(e)))
    if tty:
        print("\r\033[K", end="", file=sys.stderr, flush=True)
    return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true", help="kategorileri ve ID'lerini listele")
    p.add_argument("--once", action="store_true", help="tek sefer kontrol et ve çık")
    p.add_argument("--interval", type=int, default=300, help="kontrol aralığı (saniye, varsayılan 300)")
    p.add_argument("--categories", help="sadece bu kategori ID'leri, virgülle (örn. 344,346)")
    p.add_argument("--json", action="store_true", help="sonuçları JSON olarak yazdır (--once ile)")
    p.add_argument("--test-notify", action="store_true", help="test bildirimi gönder ve çık")
    args = p.parse_args()

    if args.test_notify:
        notify("Aachen Termin Bot", "Test bildirimi — bildirimler çalışıyor ✅")
        return

    if args.list:
        for c in fetch_categories():
            print(f"{c.id:>5}  {c.group:<28} {c.name}")
        return

    category_filter = {c.strip() for c in args.categories.split(",")} if args.categories else None
    interval = max(args.interval, 60)  # don't hammer the server

    while True:
        state = load_state()
        try:
            results = run_once(category_filter)
        except (requests.RequestException, RuntimeError) as e:
            print(f"Kontrol başarısız: {e}", file=sys.stderr)
            results = None

        if results is not None:
            if args.json:
                print(json.dumps(
                    [{**asdict(r.category), "location": r.location, "error": r.error,
                      "earliest": asdict(r.earliest) if r.earliest else None,
                      "slot_count": len(r.slots)} for r in results],
                    ensure_ascii=False, indent=2))
            else:
                print_table(results)
            save_state(diff_and_notify(results, state, first_run=not state))

        if args.once:
            break
        next_check = datetime.fromtimestamp(time.time() + interval).strftime("%H:%M:%S")
        print(f"Sonraki kontrol: {next_check} (durdurmak için Ctrl+C)", file=sys.stderr, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDurduruldu.")
