# Better Termin

An unofficial tracker for the [Ausländeramt Aachen](https://termine.staedteregion-aachen.de/auslaenderamt/) booking site. It shows the earliest free appointment for every category. For categories with no free slots, it shows how often new slots open. It only reads the site and never books anything.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python web.py
```

Open http://127.0.0.1:8000. The app checks all categories every 5 minutes and stores history in `data/termin.db`.

E-mail alerts use double opt-in: the alert only starts after the link in the confirmation e-mail is clicked, and unconfirmed sign-ups are deleted after 48 hours. Sending e-mails isn't wired up yet; for now `send_email` in `web.py` prints them to the server log.

Before publishing, copy `.env.example` to `.env` and fill in the operator details shown on `/imprint` and `/privacy`. The app loads no third-party resources (fonts are self-hosted) and doesn't log IP addresses.

## Command line

```bash
.venv/bin/python termin_bot.py --once
```

Prints the earliest slot per category. Leave off `--once` to keep checking, and use `--help` for the other options. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to get Telegram notifications.

A GitHub Actions workflow (`.github/workflows/termin-bot.yml`) runs this every 15 minutes. It needs the two Telegram values as repo secrets.

## Tests

```bash
.venv/bin/python -m unittest test_web
```

## License

[MIT](LICENSE). The bundled Noto Sans fonts in `static/fonts/` are under the [SIL Open Font License 1.1](static/fonts/OFL.txt).
