#!/usr/bin/env python3
"""
sync.py — output side of the claude-code -> calendar logger.

Takes the model's headline map for changed slots and upserts a 2-hour event per
slot onto a dedicated calendar, matching existing events BY TIME WINDOW. It only
ever touches events it created itself, identified by an extendedProperties marker
(extendedProperties.private.claudecode == MARKER). Events in the same window
without that marker are left alone — the tool inserts its own alongside them and
never edits or deletes another layer/event.

On a successful push it atomically commits the cache: raw.json (the source
snapshot from extract.py) and headlines.json (the new headlines). If the push
fails, nothing is committed, so the next extract run still sees the delta.

Input (stdin or --in), JSON:
{
  "headlines": { "2025-06-12T14:00:00-07:00": "OAuth, Calendar sync", ... },
  "_snapshot": { ... }   # passed straight through from extract.py
}

A slot mapped to "" or null means "delete my event in that slot" (only mine).

Calendar selection: --calendar-id (default "claude code" by summary lookup, or
create it if missing — only when --create-calendar is passed).

Requires: google-api-python-client, google-auth-httplib2, google-auth-oauthlib
(the official Google-maintained client — confirm current versions yourself).
Auth: OAuth user creds cached at <cache>/token.json; client secrets at
--client-secret (default <cache>/credentials.json). Scope: calendar (read/write).

CONFIRM AGAINST LIVE API: events.list timeMin/timeMax semantics (it returns
events that *overlap* the window, not only those starting in it), and that
privateExtendedProperty filtering is supported on list. Both are standard, but
verify.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

SLOT_HOURS = 2
MARKER_KEY = "claudecode"
MARKER_VAL = "1"
DEFAULT_CAL_SUMMARY = "claude code"
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _eprint(*a):
    print(*a, file=sys.stderr)


def _load_creds(cache, client_secret):
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    token_path = os.path.join(cache, "token.json")
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
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
    end = start + timedelta(hours=SLOT_HOURS)
    return start, end


def _find_my_event(svc, cal_id, start, end):
    """Return my marked event whose start matches this slot, or None.

    Filters by the private extended property so only our own events come back,
    then matches exact start to avoid the overlap-window catching an adjacent
    slot's event.
    """
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


def sync(svc, cal_id, headlines):
    """Apply headlines. Returns (inserted, updated, deleted, skipped)."""
    ins = upd = dele = skip = 0
    for slot_iso, title in headlines.items():
        start, end = _slot_bounds(slot_iso)
        existing = _find_my_event(svc, cal_id, start, end)
        clean_title = (title or "").strip()

        if not clean_title:
            if existing:
                svc.events().delete(calendarId=cal_id, eventId=existing["id"]).execute()
                dele += 1
            else:
                skip += 1
            continue

        if existing:
            if existing.get("summary") == clean_title:
                skip += 1  # no-op: headline unchanged
                continue
            existing["summary"] = clean_title
            svc.events().update(
                calendarId=cal_id, eventId=existing["id"], body=existing
            ).execute()
            upd += 1
        else:
            svc.events().insert(
                calendarId=cal_id, body=_event_body(start, end, clean_title)
            ).execute()
            ins += 1
    return ins, upd, dele, skip


def _commit_cache(cache, snapshot, headlines):
    """Atomically write raw.json and merge headlines.json after success."""
    raw_path = os.path.join(cache, "raw.json")
    head_path = os.path.join(cache, "headlines.json")

    if snapshot is not None:
        tmp = raw_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        os.replace(tmp, raw_path)

    # Merge: keep prior headlines, apply new ones, drop the ones we deleted ("").
    prior = {}
    if os.path.exists(head_path):
        with open(head_path, "r", encoding="utf-8") as f:
            try:
                prior = json.load(f)
            except json.JSONDecodeError:
                prior = {}
    for slot, title in headlines.items():
        if (title or "").strip():
            prior[slot] = title.strip()
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
                    help="Print planned actions without calling the API")
    args = ap.parse_args()

    os.makedirs(args.cache, exist_ok=True)
    client_secret = args.client_secret or os.path.join(args.cache, "credentials.json")

    payload = json.load(open(args.infile) if args.infile else sys.stdin)
    headlines = payload.get("headlines", {})
    snapshot = payload.get("_snapshot")

    if args.dry_run:
        for slot, title in sorted(headlines.items()):
            start, end = _slot_bounds(slot)
            verb = "DELETE(mine)" if not (title or "").strip() else "UPSERT(mine)"
            _eprint(f"{verb} {start.isoformat()}  ->  {title!r}")
        _eprint(f"[dry-run] {len(headlines)} slot(s); cache not committed")
        return

    creds = _load_creds(args.cache, client_secret)
    svc = _service(creds)
    cal_id = _resolve_calendar(svc, args.calendar_id, args.create_calendar)

    ins, upd, dele, skip = sync(svc, cal_id, headlines)

    # Only commit if the push didn't raise. (sync raises on API error, which
    # skips this line, leaving the delta intact for the next run.)
    _commit_cache(args.cache, snapshot, headlines)

    _eprint(f"inserted={ins} updated={upd} deleted={dele} skipped={skip} cal={cal_id}")


if __name__ == "__main__":
    main()
