#!/usr/bin/env python3
"""
sync.py — output side of the claude-code -> calendar logger.

Takes the model's headline map for changed slots and upserts a 2-hour event per
slot onto a dedicated calendar, matching existing events BY TIME WINDOW. It only
ever touches events it created itself, identified by an extendedProperties
marker (extendedProperties.private.claudecode == "1"). Events in the same
window without that marker are left alone — the tool inserts its own alongside
them and never edits or deletes another layer/event.

On a successful push it commits the cache: <cache>/raw.pending.json (written by
extract.py) is promoted to raw.json, and headlines.json is merged. If the push
fails, nothing is committed, so the next extract run still sees the delta.

Input (stdin or --in), JSON:
{
  "headlines": { "2026-06-12T14:00:00-04:00": "OAuth, Calendar Sync", ... }
}

A slot mapped to "" or null means "delete my event in that slot" (only mine).
Slots omitted from the map are left untouched.

Calendar selection: --calendar-id, or lookup by summary "Claude Code"
(created if missing when --create-calendar is passed).

events.list window semantics (per the official API reference): timeMin is an
exclusive lower bound on the event's END, timeMax an exclusive upper bound on
its START — so querying [slot_start, slot_end] can't return adjacent slots'
events. The exact-start instant match below additionally disambiguates the
DST fall-back hour, where two distinct slot keys overlap in real time.

Requires: google-api-python-client google-auth-httplib2 google-auth-oauthlib
Auth: OAuth user creds cached at <cache>/token.json; client secrets at
--client-secret (default <cache>/credentials.json). Scope: calendar.

--dry-run authenticates and READS the calendar to print the exact op per slot
(insert/patch/delete/skip-identical/skip-no-event) but writes and commits
nothing.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

SLOT_HOURS = 2
MARKER_KEY = "claudecode"
MARKER_VAL = "1"
DEFAULT_CAL_SUMMARY = "Claude Code"
SCOPES = ["https://www.googleapis.com/auth/calendar"]
PENDING_STALE_SECS = 3600


def _eprint(*a):
    print(*a, file=sys.stderr)


def _load_creds(cache, client_secret):
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    token_path = os.path.join(cache, "token.json")
    creds = None
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except ValueError:
            creds = None
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:  # revoked/expired refresh token -> re-consent
            _eprint(f"token refresh failed ({e}); falling back to browser flow")
            creds = None
    if not creds or not creds.valid:
        if not os.path.exists(client_secret):
            raise SystemExit(
                f"OAuth client secret not found at {client_secret}. Create a "
                f"Desktop OAuth client in Google Cloud Console (Calendar API "
                f"enabled) and download its JSON there, or pass --client-secret."
            )
        flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
        creds = flow.run_local_server(port=0)
    with open(token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    return creds


def _service(creds):
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _resolve_calendar(svc, calendar_id, create):
    if calendar_id:
        return calendar_id
    page = None
    while True:
        resp = svc.calendarList().list(pageToken=page).execute()
        for item in resp.get("items", []):
            if item.get("summary") == DEFAULT_CAL_SUMMARY:
                return item["id"]
        page = resp.get("nextPageToken")
        if not page:
            break
    if create:
        cal = svc.calendars().insert(body={"summary": DEFAULT_CAL_SUMMARY}).execute()
        return cal["id"]
    raise SystemExit(
        f'Calendar "{DEFAULT_CAL_SUMMARY}" not found. Pass --calendar-id or '
        f"--create-calendar."
    )


def _slot_bounds(slot_iso):
    start = datetime.fromisoformat(slot_iso)
    if start.tzinfo is None:
        raise SystemExit(f"slot key {slot_iso!r} has no UTC offset")
    return start, start + timedelta(hours=SLOT_HOURS)


def _find_my_event(svc, cal_id, start, end):
    """Return my marked event whose start matches this slot instant, or None."""
    resp = svc.events().list(
        calendarId=cal_id,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        privateExtendedProperty=f"{MARKER_KEY}={MARKER_VAL}",
        singleEvents=True,
        maxResults=50,
    ).execute()
    for ev in resp.get("items", []):
        ev_start = ev.get("start", {}).get("dateTime")
        if ev_start and datetime.fromisoformat(ev_start) == start:
            return ev
    return None


def _event_body(start, end, title):
    return {
        "summary": title,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "extendedProperties": {"private": {MARKER_KEY: MARKER_VAL}},
    }


def _plan(svc, cal_id, slot_iso, title):
    """Decide the op for one slot. Returns (op, existing_event_or_None)."""
    start, end = _slot_bounds(slot_iso)
    existing = _find_my_event(svc, cal_id, start, end)
    clean = (title or "").strip()
    if not clean:
        return ("delete" if existing else "skip-no-event"), existing
    if existing:
        if existing.get("summary") == clean:
            return "skip-identical", existing
        return "patch", existing
    return "insert", None


def sync(svc, cal_id, headlines, dry_run):
    """Apply (or with dry_run just print) the planned op per slot."""
    counts = {"insert": 0, "patch": 0, "delete": 0,
              "skip-identical": 0, "skip-no-event": 0}
    for slot_iso, title in sorted(headlines.items()):
        op, existing = _plan(svc, cal_id, slot_iso, title)
        counts[op] += 1
        clean = (title or "").strip()
        if dry_run:
            _eprint(f"[dry-run] {op:<14} {slot_iso}  ->  {clean!r}")
            continue
        start, end = _slot_bounds(slot_iso)
        if op == "delete":
            svc.events().delete(calendarId=cal_id, eventId=existing["id"]).execute()
        elif op == "patch":
            svc.events().patch(
                calendarId=cal_id, eventId=existing["id"], body={"summary": clean}
            ).execute()
        elif op == "insert":
            svc.events().insert(
                calendarId=cal_id, body=_event_body(start, end, clean)
            ).execute()
    return counts


def _commit_cache(cache, headlines):
    """Promote raw.pending.json -> raw.json and merge headlines.json."""
    pending = os.path.join(cache, "raw.pending.json")
    raw_path = os.path.join(cache, "raw.json")
    head_path = os.path.join(cache, "headlines.json")

    if os.path.exists(pending):
        age = time.time() - os.path.getmtime(pending)
        if age > PENDING_STALE_SECS:
            _eprint(f"warning: raw.pending.json is {int(age // 60)} min old — "
                    f"is the loop running extract right before sync?")
        os.replace(pending, raw_path)
    else:
        _eprint("warning: raw.pending.json missing — snapshot not committed "
                "(harmless if re-running sync after a success)")

    prior = {}
    if os.path.exists(head_path):
        with open(head_path, "r", encoding="utf-8") as f:
            try:
                prior = json.load(f)
            except json.JSONDecodeError:
                prior = {}
    for slot, title in headlines.items():
        clean = (title or "").strip()
        if clean:
            prior[slot] = clean
        else:
            prior.pop(slot, None)
    tmp = head_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prior, f, ensure_ascii=False, indent=2)
    os.replace(tmp, head_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default=None,
                    help="JSON input file; default stdin")
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/claude-cal"))
    ap.add_argument("--calendar-id", default=None)
    ap.add_argument("--create-calendar", action="store_true")
    ap.add_argument("--client-secret", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="Authenticate and read, but write and commit nothing")
    args = ap.parse_args()

    os.makedirs(args.cache, exist_ok=True)
    client_secret = args.client_secret or os.path.join(args.cache, "credentials.json")

    payload = json.load(open(args.infile) if args.infile else sys.stdin)
    headlines = payload.get("headlines", {})
    if not headlines:
        _eprint("no headlines in input; nothing to do (cache not committed)")
        return

    creds = _load_creds(args.cache, client_secret)
    svc = _service(creds)
    cal_id = _resolve_calendar(svc, args.calendar_id, args.create_calendar)

    counts = sync(svc, cal_id, headlines, args.dry_run)

    if args.dry_run:
        _eprint(f"[dry-run] {len(headlines)} slot(s); nothing written, "
                f"cache not committed")
        return

    # Only reached if no API call raised; a failed push commits nothing and
    # the next extract run re-emits the delta.
    _commit_cache(args.cache, headlines)

    _eprint("  ".join(f"{k}={v}" for k, v in counts.items()) + f"  cal={cal_id}")


if __name__ == "__main__":
    main()
