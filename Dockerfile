FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Berlin
# Timestamps are local time; the site and its users are in Germany.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY termin_bot.py web.py ./
COPY static ./static
COPY templates ./templates
# The named volume mounted here inherits this ownership on first use.
RUN mkdir data && chown app:app data

USER app
# Only reachable through the Tailscale container (shared network namespace).
CMD ["python", "web.py", "--host", "127.0.0.1", "--port", "8000"]
