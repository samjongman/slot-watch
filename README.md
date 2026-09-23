# slot-watch

Polls a Calendly booking page on a schedule and emails when an appointment
slot opens up that was not there before. Useful when a calendar is fully
booked and cancellations are the only way in.

Python standard library only, no dependencies.

## How it works

1. `/api/booking/event_types/lookup` turns the profile and event slugs into an
   event-type UUID, resolved on every run so a change on the Calendly side
   does not silently break the watcher.
2. `/api/booking/event_types/<uuid>/calendar/range` returns each day's open
   slots. Calendly rejects ranges wider than a month, so months are fetched
   one at a time.
3. Open slots are diffed against the previous run's snapshot in
   `state/seen_spots.json`. Only slots that were not there before trigger an
   email, so a slot that stays open does not mail you every 15 minutes.
4. The snapshot is committed back to the repo by the workflow, and only after
   the email has actually been sent - a failed send retries on the next run
   instead of being silently swallowed.

## Configuration

All configuration is environment variables; nothing about the watched page
lives in this repo.

| Variable | Purpose |
| --- | --- |
| `CALENDLY_PROFILE` | the `<profile>` in `calendly.com/<profile>/<event>` |
| `CALENDLY_EVENT` | the `<event>` in that same URL |
| `WATCH_MONTHS` | comma-separated `YYYY-MM` |
| `RESEND_API_KEY` | [Resend](https://resend.com) API key |
| `ALERT_TO` | recipient address |
| `ALERT_FROM` | sender, defaults to `onboarding@resend.dev` |
| `ALERT_LABEL` | name shown in the email subject |
| `CALENDLY_TZ` | timezone the times are displayed in |
| `SEED` | `1` records current slots without emailing |

On Resend's free tier without a verified domain, the built-in
`onboarding@resend.dev` sender only delivers to the account's own address.

Run locally:

```sh
CALENDLY_PROFILE=... CALENDLY_EVENT=... WATCH_MONTHS=2026-10 \
RESEND_API_KEY=... ALERT_TO=... python3 check_availability.py
```

## GitHub Actions

`.github/workflows/check.yml` runs it every 15 minutes. Add
`CALENDLY_PROFILE`, `CALENDLY_EVENT`, `RESEND_API_KEY` and `ALERT_TO` as
repository secrets, then enable the workflow.

Use a public repo: Actions minutes are unlimited there, while a 15-minute cron
on a private repo costs ~2880 minutes/month against a 2000-minute allowance.
Drop the cron to `*/30` if it has to be private.

*Run workflow* takes two optional inputs: **months** to check a different
month once without touching the schedule, and **seed** to record current slots
without emailing.

## Notes

- GitHub's cron is best-effort and can run 5-20 minutes late under load.
- Scheduled workflows are disabled after 60 days of repository inactivity, so
  the workflow commits a heartbeat if nothing else has been committed in 20
  days.
- Calendly's booking API is undocumented and may change without notice; the
  run fails loudly if it does.
