# Better Termin

Tracks the earliest free appointment for **every category** on the [Städteregion Aachen Ausländeramt](https://termine.staedteregion-aachen.de/auslaenderamt/) booking site, and notifies you when an earlier slot opens up in a category (terminal + macOS notification + optional Telegram).

The bot only reads. It **never books an appointment**, so you still book it yourself on the site.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Web UI

```bash
.venv/bin/python web.py
```

Then open **http://127.0.0.1:8000** in your browser. The UI uses the booking site's theme and shows:

- **When a slot is available:** the earliest date and time, how many days away it is, and when it opened (if the app saw it open).
- **When nothing is available:** how often new slots open: the average interval, when one last opened, the usual hour of day, and how long slots stay open. These stats come from the changes the app **has observed since tracking started**, so for the first few days the page shows that it is still collecting data.
- An **e-mail alert** button: saves an e-mail address for a category in `data/termin.db`. ⚠️ E-mail *delivery* is not wired up yet: `send_email` in `web.py` only logs for now. It will be connected to SMTP or a mail service once hosting is set up.

By default, the app checks all categories in the background every 5 minutes. Use `--interval 600` to change that and `--port` to change the port. The "Check now" button runs at most once a minute. History is stored in `data/termin.db` (SQLite), and deleting that file resets the stats.

Tests: `.venv/bin/python -m unittest test_web`

## Command line

List categories and their IDs:

```bash
.venv/bin/python termin_bot.py --list
```

Check all categories once:

```bash
.venv/bin/python termin_bot.py --once
```

Keep tracking (every 5 minutes by default):

```bash
.venv/bin/python termin_bot.py
```

Only some categories, every 10 minutes:

```bash
.venv/bin/python termin_bot.py --categories 344,346 --interval 600
```

Example output:

```
=== 18.10.2026 06:38:22 — earliest appointments ===
  [345] Freizügigkeit/ Daueraufenthaltsbescheinigung   21.10.2026 (Wed) 09:30
  [346] FH Studenten                                   25.11.2026 (Wed) 08:30
  [344] RWTH Studenten                                 no free slots
```

### Options

| Option | Description |
|---|---|
| `--list` | List categories and their IDs |
| `--once` | Check once and exit |
| `--interval N` | Check interval in seconds (default 300, minimum 60) |
| `--categories 344,346` | Only track these category IDs |
| `--json` | Print results as JSON |
| `--test-notify` | Send a test notification |

## Notifications

On every check, the command-line bot saves the earliest date per category to `state.json`. If a later check finds an **earlier** slot in a category, or a slot opens where there was none, it sends a notification. The first run sends nothing and only records the baseline.

- **Terminal:** always.
- **macOS notification:** automatic on macOS. Set `NO_MAC_NOTIFY=1` to turn it off.
- **Telegram (optional):**
  1. Create a bot with [@BotFather](https://t.me/BotFather) on Telegram and get its token.
  2. Send your bot a message, then find `chat.id` at `https://api.telegram.org/bot<TOKEN>/getUpdates`.
  3. Run with these environment variables:

     ```bash
     export TELEGRAM_BOT_TOKEN="..."
     export TELEGRAM_CHAT_ID="..."
     .venv/bin/python termin_bot.py --test-notify
     ```

## Running on GitHub Actions

To keep tracking while your computer is off, [`.github/workflows/termin-bot.yml`](.github/workflows/termin-bot.yml) runs `python termin_bot.py --once` on GitHub's servers **every 15 minutes**. `state.json` is kept in the GitHub Actions cache between runs, so earlier-slot detection keeps working. Notifications arrive via Telegram, and macOS notifications are off there.

Setup:

1. On the repo page, go to **Settings → Secrets and variables → Actions → New repository secret** and add two secrets:
   - `TELEGRAM_BOT_TOKEN`: the token from BotFather
   - `TELEGRAM_CHAT_ID`: the `chat.id` you found above

   Or from the terminal:

   ```bash
   gh secret set TELEGRAM_BOT_TOKEN
   ```

   ```bash
   gh secret set TELEGRAM_CHAT_ID
   ```

2. Make sure workflows are enabled on the **Actions** tab. Scheduled runs start on their own.
3. To run it manually, go to Actions → **Termin Bot** → **Run workflow**, or run:

   ```bash
   gh workflow run termin-bot.yml
   ```

Notes:

- The first run sends nothing and only records the baseline. The same happens after the cache is deleted or goes unused for 7 days.
- GitHub doesn't guarantee scheduled runs. At busy times they can be delayed by a few minutes (sometimes more) or skipped.
- **Minutes quota:** private repos on a free account get 2,000 Actions minutes a month. One run takes about 1.5 minutes and is billed as 2. Running every 15 minutes uses about 5,800 minutes a month, so the quota runs out before mid-month and the bot stops. You can fix this in three ways:
  - Make the repo public. Public repos have unlimited minutes.
  - Run less often. `cron: "0 * * * *"` runs hourly and uses about 1,450 minutes a month.
  - Track only the categories you need by adding `--categories 344,346` to the workflow command. Each run then takes under a minute, and with `cron: "*/30 * * * *"` it uses about 1,450 minutes a month.
- To stop it, go to Actions → Termin Bot → **⋯ → Disable workflow**.

## How it works

The site runs the TEVIS appointment system. For each category, the bot follows the same steps as a browser:

1. `select2?md=1`: read the category list (Anliegen). New categories are picked up automatically.
2. `location?...&cnc-<ID>=1`: select the category and read the location form.
3. POST the location to get the `suggest` page. Each `suggestion_form` on it is a free slot, and taken slots show as `disabled` buttons. Days are listed in date order, so the bot reads the page **only up to the first free slot** and then closes the connection instead of downloading pages that are several MB.

To go easy on the server, the bot waits 1 second between categories, and the check interval is at least 60 seconds.
