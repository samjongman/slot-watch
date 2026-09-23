#!/usr/bin/env python3
"""Watch a Calendly booking page and email when new slots open up.

Reads Calendly's public booking API (the same one the booking page itself
calls), compares the available slots against the previous run's snapshot, and
emails via Resend only when a slot appears that was not there before.

Configured entirely through environment variables:
At least one notification channel must be configured; both can be.
  RESEND_API_KEY   email: Resend API key
  ALERT_TO         email: where the alert goes
  ALERT_FROM       email: sender (default onboarding@resend.dev)
  WHATSAPP_PHONE   whatsapp: recipient in international form, e.g. +31612345678
  CALLMEBOT_APIKEY whatsapp: key CallMeBot sends you on activation
  ALERT_LABEL      name shown in the alert
  CALENDLY_PROFILE required - the <profile> in calendly.com/<profile>/<event>
  CALENDLY_EVENT   required - the <event> in that same URL
  WATCH_MONTHS     comma-separated YYYY-MM
  CALENDLY_TZ      timezone the times are shown in
  STATE_FILE       snapshot path
  SEED             "1" to record current slots without emailing
"""
import calendar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROFILE = os.environ.get("CALENDLY_PROFILE", "")
EVENT = os.environ.get("CALENDLY_EVENT", "")
TZ = os.environ.get("CALENDLY_TZ", "Europe/Amsterdam")
MONTHS = [m.strip() for m in os.environ.get("WATCH_MONTHS", "").split(",") if m.strip()]
STATE_FILE = os.environ.get("STATE_FILE", "state/seen_spots.json")
RESEND_KEY = os.environ.get("RESEND_API_KEY", "")
ALERT_TO = os.environ.get("ALERT_TO", "")
ALERT_FROM = os.environ.get("ALERT_FROM", "onboarding@resend.dev")
ALERT_LABEL = os.environ.get("ALERT_LABEL", "Calendly")
WHATSAPP_PHONE = os.environ.get("WHATSAPP_PHONE", "")
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")
SEED = os.environ.get("SEED", "") == "1"

BOOKING_URL = "https://calendly.com/{}/{}".format(PROFILE, EVENT)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

DAYS_NL = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MONTHS_NL = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
             "augustus", "september", "oktober", "november", "december"]


def log(msg):
    print(msg, flush=True)


def get_json(url, attempts=4):
    """GET a JSON endpoint, retrying transient failures with backoff."""
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Accept-Language": "nl,en;q=0.8",
                "Referer": BOOKING_URL,
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError) as e:
            last = e
            if i < attempts - 1:
                time.sleep(2 ** i)
    raise RuntimeError("giving up on {}: {}".format(url, last))


def event_uuid():
    """Resolve the event-type UUID from the slugs, so a Calendly-side change
    to the event does not silently break the watcher."""
    q = urllib.parse.urlencode({"event_type_slug": EVENT, "profile_slug": PROFILE})
    data = get_json("https://calendly.com/api/booking/event_types/lookup?" + q)
    uuid = data.get("uuid")
    if not uuid:
        raise RuntimeError("no uuid in event type lookup - did the link change?")
    return uuid


def fetch_month(uuid, ym):
    """Return available slot start-times for one YYYY-MM.

    Calendly rejects ranges wider than a single month, so months are fetched
    one at a time.
    """
    year, month = (int(x) for x in ym.split("-"))
    last = calendar.monthrange(year, month)[1]
    q = urllib.parse.urlencode({
        "timezone": TZ,
        "diagnostics": "false",
        "range_start": "{}-01".format(ym),
        "range_end": "{}-{:02d}".format(ym, last),
    })
    url = "https://calendly.com/api/booking/event_types/{}/calendar/range?{}".format(uuid, q)
    data = get_json(url)
    if "days" not in data:
        raise RuntimeError("unexpected response for {}: {}".format(ym, json.dumps(data)[:200]))
    spots = []
    for day in data["days"]:
        if day.get("status") != "available":
            continue
        for spot in day.get("spots", []):
            if spot.get("status") == "available" and spot.get("start_time"):
                spots.append(spot["start_time"])
    return spots


def pretty(iso):
    """2026-10-13T08:00:00+02:00 -> 'dinsdag 13 oktober, 08:00'"""
    date, _, rest = iso.partition("T")
    y, m, d = (int(x) for x in date.split("-"))
    wd = DAYS_NL[calendar.weekday(y, m, d)]
    return "{} {} {}, {}".format(wd, d, MONTHS_NL[m - 1], rest[:5])


def link_for(iso):
    date = iso.split("T")[0]
    return "{}?month={}&date={}".format(BOOKING_URL, date[:7], date)


def send_email(new_spots, all_spots):
    n = len(new_spots)
    plural = "ken" if n != 1 else ""
    subject = "{}: {} nieuwe plek{} vrij".format(ALERT_LABEL, n, plural)

    rows = "".join(
        '<li style="margin:6px 0"><strong>{}</strong> &nbsp;'
        '<a href="{}">boek deze</a></li>'.format(pretty(s), link_for(s))
        for s in new_spots
    )
    others = len(all_spots) - n
    tail = ""
    if others > 0:
        tail = ('<p style="color:#666;font-size:13px">Plus {} plek(ken) die al eerder '
                'gemeld waren.</p>'.format(others))

    html = (
        '<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;'
        'font-size:15px;line-height:1.5">'
        '<p>Er {} <strong>{}</strong> nieuwe afspraakplek{} vrijgekomen:</p>'
        '<ul style="padding-left:18px">{}</ul>{}'
        '<p><a href="{}" style="background:#0b57d0;color:#fff;padding:9px 16px;'
        'border-radius:6px;text-decoration:none;display:inline-block">Open de agenda</a></p>'
        '<p style="color:#888;font-size:12px">Tijden in {}. Wie het eerst komt, het eerst '
        'maalt - deze plekken kunnen zo weer weg zijn.</p></div>'
    ).format("is" if n == 1 else "zijn", n, plural, rows, tail, BOOKING_URL, TZ)

    text = "Nieuwe vrije plekken:\n" + "\n".join(
        "  - {}  {}".format(pretty(s), link_for(s)) for s in new_spots)

    payload = json.dumps({
        "from": "slot-watch <{}>".format(ALERT_FROM),
        "to": [ALERT_TO],
        "subject": subject,
        "html": html,
        "text": text,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload, method="POST",
        headers={"Authorization": "Bearer " + RESEND_KEY,
                 "Content-Type": "application/json",
                 "Accept": "application/json",
                 # Cloudflare fronts the API and blocks urllib's default
                 # User-Agent outright (error 1010) before Resend ever sees it.
                 "User-Agent": "slot-watch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            log("email sent ({})".format(r.status))
    except urllib.error.HTTPError as e:
        # Resend explains refusals in the body - without it a 403 is a
        # guessing game between a bad key, an unverified sender and a
        # recipient the free tier will not deliver to.
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError("Resend rejected the send: HTTP {} {}".format(e.code, detail))


def send_whatsapp(new_spots, all_spots):
    n = len(new_spots)
    plural = "ken" if n != 1 else ""
    shown = new_spots[:6]
    lines = ["*{}*: {} nieuwe plek{} vrij".format(ALERT_LABEL, n, plural)]
    lines += ["- " + pretty(s) for s in shown]
    if n > len(shown):
        lines.append("...en nog {} andere".format(n - len(shown)))
    lines.append(BOOKING_URL)
    text = "\n".join(lines)
    # CallMeBot takes the message in the query string, so stay well under the
    # practical URL length ceiling.
    if len(text) > 900:
        text = text[:880] + "..."

    q = urllib.parse.urlencode({
        "phone": WHATSAPP_PHONE,
        "text": text,
        "apikey": CALLMEBOT_APIKEY,
    })
    req = urllib.request.Request("https://api.callmebot.com/whatsapp.php?" + q,
                                 headers={"User-Agent": "slot-watch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError("CallMeBot HTTP {}: {}".format(
            e.code, e.read().decode("utf-8", "replace")[:200]))
    if "ERROR" in body.upper():
        # CallMeBot reports refusals in a 200 body, not in the status code.
        raise RuntimeError("CallMeBot refused: {}".format(body[:200]))
    log("whatsapp sent")


def notify(new_spots, all_spots):
    """Send through every configured channel.

    Returns True if at least one got through - one channel being down must
    not stop the others, and must not lose the alert.
    """
    channels = []
    if RESEND_KEY and ALERT_TO:
        channels.append(("email", send_email))
    if WHATSAPP_PHONE and CALLMEBOT_APIKEY:
        channels.append(("whatsapp", send_whatsapp))

    delivered = False
    for name, fn in channels:
        try:
            fn(new_spots, all_spots)
            delivered = True
        except Exception as exc:
            log("WARNING: {} channel failed: {}".format(name, exc))
    return delivered


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f).get("spots", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_state(spots):
    parent = os.path.dirname(STATE_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "months": MONTHS,
            "spots": sorted(spots),
        }, f, indent=2)
        f.write("\n")


def main():
    if not PROFILE or not EVENT:
        log("ERROR: CALENDLY_PROFILE and CALENDLY_EVENT must both be set")
        return 1
    if not MONTHS:
        log("ERROR: WATCH_MONTHS must be set, e.g. 2026-10,2026-11")
        return 1
    has_email = bool(RESEND_KEY and ALERT_TO)
    has_whatsapp = bool(WHATSAPP_PHONE and CALLMEBOT_APIKEY)
    if not SEED and not has_email and not has_whatsapp:
        log("ERROR: configure email (RESEND_API_KEY + ALERT_TO) or "
            "whatsapp (WHATSAPP_PHONE + CALLMEBOT_APIKEY)")
        return 1

    uuid = event_uuid()
    current = set()
    for ym in MONTHS:
        found = fetch_month(uuid, ym)
        log("{}: {} open slot(s)".format(ym, len(found)))
        current |= set(found)

    previous = load_state()
    new = sorted(current - previous)

    if SEED:
        save_state(current)
        log("seeded state with {} slot(s), no email sent".format(len(current)))
        return 0

    if not new:
        # Nothing new. Still rewrite state so slots that got booked drop out
        # and can alert again if they free up later.
        save_state(current)
        log("no new slots ({} known open)".format(len(current)))
        return 0

    log("{} NEW slot(s)".format(len(new)))
    # Save only once something is away - a wholly failed send must retry on
    # the next run, not get swallowed by an updated snapshot.
    if not notify(new, current):
        log("ERROR: no channel accepted the alert; snapshot left untouched")
        return 1
    save_state(current)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log("ERROR: {}".format(exc))
        sys.exit(1)
