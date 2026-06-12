#!/usr/bin/env python3
"""
extract.py — input side of the claude-code -> calendar logger.

Reads Claude Code session transcripts (JSONL, one message per line with a
timestamp), buckets every message into a 2-hour slot in LOCAL time, diffs the
result against the cached previous run, and prints ONLY the changed slots as
JSON on stdout.

It does NOT generate headlines. That is the model's job in the loop. This script
just hands the model the raw material for changed slots plus whatever headline
was last pushed for that slot, so the model can refine in place.

The model is expected to know / pass the project transcript directory. We do not
hardcode a path here; pass --src (one or more dirs or files). JSONL is globbed
recursively from any dir given.

Output shape (stdout):
{
  "slots": {
    "2025-06-12T14:00:00-07:00": {
      "tasks": ["fixed oauth token refresh", "...", ...],   # each <10 words
      "existing_headline": "OAuth, Calendar sync"            # or null
    },
    ...
  }
}

Cache files (under --cache, default ~/.cache/claude-cal):
  raw.json        last-seen {slot_iso: [tasks]} — basis for the diff
  headlines.json  last-pushed {slot_iso: "headline, headline"} — written by sync.py

Confirm against your machine: the transcript schema below (timestamp + text
extraction). If your lines differ, adjust _extract_message().
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta

SLOT_HOURS = 2
MAX_WORDS = 9  # "<10 words"


def _cache_dir(arg):
    d = arg or os.path.expanduser("~/.cache/claude-cal")
    os.makedirs(d, exist_ok=True)
    return d


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _iter_jsonl_files(srcs):
    for s in srcs:
        if os.path.isdir(s):
            yield from sorted(glob.glob(os.path.join(s, "**", "*.jsonl"), recursive=True))
        elif os.path.isfile(s):
            yield s
        # silently skip nonexistent; the model controls --src


def _parse_ts(raw):
    """Parse an ISO-ish timestamp into an aware local datetime.

    Accepts trailing 'Z' (UTC) and naive strings (assumed local). Returns a
    timezone-aware datetime in the machine's LOCAL zone.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # epoch seconds / ms fallback
        try:
            n = float(s)
            if n > 1e12:  # ms
                n /= 1000.0
            dt = datetime.fromtimestamp(n)
        except (ValueError, OverflowError):
            return None
    local_tz = datetime.now().astimezone().tzinfo
    if dt.tzinfo is None:
        return dt.replace(tzinfo=local_tz)
    return dt.astimezone(local_tz)


def _extract_message(obj):
    """Return (timestamp_raw, text) from one transcript line, or (None, None).

    Defensive across schema variants: looks for a timestamp under common keys,
    and text under common keys (including a content list of blocks). Adjust here
    if your transcripts differ.
    """
    if not isinstance(obj, dict):
        return None, None

    ts = (obj.get("timestamp") or obj.get("time") or obj.get("ts")
          or obj.get("created_at") or obj.get("createdAt"))

    text = None
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        text = " ".join(p for p in parts if p)
    elif isinstance(msg, dict):
        text = msg.get("text") or msg.get("summary")

    if not text:
        return ts, None
    return ts, _shorten(text)


def _shorten(text):
    words = " ".join(str(text).split())
    w = words.split(" ")
    if len(w) > MAX_WORDS:
        words = " ".join(w[:MAX_WORDS])
    return words.strip()


def _floor_slot(dt):
    """Floor an aware local datetime to its 2-hour boundary, return ISO string."""
    hour = (dt.hour // SLOT_HOURS) * SLOT_HOURS
    slot = dt.replace(hour=hour, minute=0, second=0, microsecond=0)
    return slot.isoformat()


def bucket(srcs):
    """Build {slot_iso: [tasks]} from all transcript lines under srcs."""
    slots = {}
    for path in _iter_jsonl_files(srcs):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts_raw, text = _extract_message(obj)
                    if not text:
                        continue
                    dt = _parse_ts(ts_raw)
                    if dt is None:
                        continue
                    slot = _floor_slot(dt)
                    bucket_list = slots.setdefault(slot, [])
                    if text not in bucket_list:  # de-dup identical lines
                        bucket_list.append(text)
        except OSError:
            continue
    return slots


def diff(current, previous):
    """Return slots that are new or whose task list changed."""
    changed = {}
    for slot, tasks in current.items():
        if previous.get(slot) != tasks:
            changed[slot] = tasks
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", nargs="+", required=True,
                    help="Transcript dirs (globbed for *.jsonl) and/or files")
    ap.add_argument("--cache", default=None)
    args = ap.parse_args()

    cache = _cache_dir(args.cache)
    head_path = os.path.join(cache, "headlines.json")
    raw_path = os.path.join(cache, "raw.json")

    previous = _load_json(raw_path, {})
    headlines = _load_json(head_path, {})

    current = bucket(args.src)
    changed = diff(current, previous)

    out = {"slots": {}, "_snapshot": current}
    for slot, tasks in sorted(changed.items()):
        out["slots"][slot] = {
            "tasks": tasks,
            "existing_headline": headlines.get(slot),
        }

    # NOTE: we do NOT write raw.json here. sync.py commits the snapshot AND the
    # new headlines together, only after a successful calendar push. That keeps
    # the delta alive if a push fails. The "_snapshot" field carries the full
    # current bucketing through the model to sync.py for that atomic commit.

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
