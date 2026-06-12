# claude-cal — Claude Code → Google Calendar activity logger

Two thin Python tools wrapping a model-in-the-loop. The model (Claude, running
in your `/loop`) is the aggregation engine: it reads the diff, writes the 3-word
headlines, and calls sync. The Python is deterministic plumbing.

```
extract.py  →  [model writes headlines]  →  sync.py
 (input)          (the loop body)           (output + cache commit)
```

## What each piece does

- **extract.py** — globs Claude Code transcripts (`*.jsonl`), buckets every
  message into a 2-hour LOCAL-time slot, diffs against `raw.json`, prints only
  changed slots (raw tasks + the headline last pushed for that slot). Commits
  nothing.
- **model** — reads that JSON, writes/refines a ≤3-word headline per changed
  slot (comma-join multiple in one slot), emits `{"headlines": {...}, "_snapshot": ...}`.
  Pass `_snapshot` through unchanged from extract's output.
- **sync.py** — upserts a 2-hour event per slot on the "claude code" calendar,
  matching existing events BY TIME WINDOW, touching ONLY events it created
  (marker `extendedProperties.private.claudecode=1`). On success, atomically
  commits `raw.json` + `headlines.json`.

## One-time setup

1. Install the official Google client (confirm current versions yourself):
   ```
   pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
   ```
2. Create an OAuth *Desktop* client in Google Cloud Console, enable the Calendar
   API, download the client secret JSON to `~/.cache/claude-cal/credentials.json`.
3. First run triggers a browser OAuth consent; the token is cached at
   `~/.cache/claude-cal/token.json`.

## The loop body

Each iteration:

```bash
# 1. INPUT — get changed slots. The model knows the transcript dir; pass it.
DELTA=$(python3 extract.py --src <PROJECT_TRANSCRIPT_DIR>)

# 2. MODEL — if DELTA.slots is empty, sleep and continue.
#    Otherwise, for each slot, write a <=3-word headline from .tasks,
#    refining .existing_headline rather than replacing wholesale.
#    Build: {"headlines": {slot: "Three Word Headline", ...},
#            "_snapshot": <copy DELTA._snapshot verbatim>}
#    A slot you want to clear -> map to "".

# 3. OUTPUT — push. Use --dry-run the very first time to inspect.
echo "$HEADLINES_JSON" | python3 sync.py --create-calendar
```

Then sleep (e.g. the slot length, or a few minutes) and repeat.

### Model rules for headlines
- ≤3 words each. Title Case.
- Multiple distinct threads in one slot → comma-separate: `"OAuth, Calendar Sync"`.
- Prefer editing the existing headline so titles stay stable across the live
  2-hour slot as new messages arrive. Overwriting is fine and expected.

## Cache files (`~/.cache/claude-cal/`)
- `raw.json` — last source snapshot (diff basis). Written by sync on success.
- `headlines.json` — last pushed headline per slot. Written by sync on success.
- `credentials.json` / `token.json` — OAuth.

## Assumptions you must verify (I could not — no network here)

1. **Transcript path & schema.** extract expects JSONL lines with a timestamp
   (`timestamp`/`time`/`ts`/`created_at`) and text (`message.content` string or
   text-block list, or `text`/`summary`). If your lines differ, edit
   `_extract_message()` in extract.py. This is the load-bearing unknown.
2. **events.list semantics.** Assumed: `timeMin/timeMax` return events
   overlapping the window, and `privateExtendedProperty=claudecode=1` filters to
   our own. Both standard; confirm with `--dry-run` and a test slot.
3. **Library is current.** `google-api-python-client` is Google's official,
   maintained client — but confirm the latest version and that the OAuth
   `run_local_server` flow still matches Google's quickstart.

## Safety properties (verified against a stubbed calendar)
- Identical headline = no calendar write (no churn from the live slot).
- Foreign events in a slot are never edited or deleted.
- Empty headline deletes only our own event.
- Failed push commits nothing → delta survives to the next run.
