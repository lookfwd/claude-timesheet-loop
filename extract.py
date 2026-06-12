#!/usr/bin/env python3
"""
extract.py — input side of the claude-code -> calendar logger.

Reads Claude Code session transcripts (JSONL under ~/.claude/projects by
default), keeps only real human prompts and session titles, buckets them into
2-hour LOCAL-time slots, diffs against the last committed snapshot
(<cache>/raw.json), and prints ONLY the changed slots as JSON on stdout.

The full current snapshot is written to <cache>/raw.pending.json. It does NOT
travel through the model. sync.py promotes pending -> raw.json after a
successful calendar push, so a failed push keeps the delta alive.

What counts as activity:
  - type=="user" lines that are actual human prompts (userType=="external",
    not isSidechain/isMeta, content a string or text-block list; tool_result
    lists and <command>/[interrupt]-style noise are skipped)
  - type=="ai-title"/"custom-title" lines: per-session titles. They carry no
    timestamp, so each session's title is attached to every slot where that
    session contributed prompts. custom-title (user-set) beats ai-title.

Output shape (stdout):
{
  "slots": {
    "2026-06-12T14:00:00-04:00": {
      "prompts": ["fix oauth refresh fallback", ...],   # each <10 words
      "titles": ["Calendar Sync Loop", ...],
      "existing_headline": "OAuth, Calendar Sync"        # or null
    },
    "2026-06-10T10:00:00-04:00": {                       # gone from source
      "prompts": [], "titles": [], "vanished": true,
      "existing_headline": "Old Experiment"
    }
  }
}

Both the snapshot and the diff only cover slots newer than --horizon-days
(default 14). Claude Code prunes old transcripts (~30 days); without the
horizon, aged-out slots would look "vanished" and the loop would delete old
calendar events. Outside the horizon, calendar history is frozen. On a cold
cache the horizon is also the backfill depth, and because out-of-horizon
slots stay out of the snapshot, re-running with a deeper --horizon-days
backfills the additional days at any time.
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta

SLOT_HOURS = 2
MAX_WORDS = 9  # "<10 words"
MAX_PROMPTS_PER_SLOT = 50

DEFAULT_SRC = os.path.expanduser("~/.claude/projects")


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


def _iter_jsonl_files(srcs, excludes):
    for s in srcs:
        if os.path.isdir(s):
            paths = sorted(glob.glob(os.path.join(s, "**", "*.jsonl"), recursive=True))
        elif os.path.isfile(s):
            paths = [s]
        else:
            continue  # silently skip nonexistent; --src is caller-controlled
        for p in paths:
            if any(x in p for x in excludes):
                continue
            yield p


def _parse_ts(raw):
    """Parse an ISO-ish timestamp into an aware local datetime.

    Accepts trailing 'Z' (UTC), epoch seconds/ms, and naive strings (assumed
    local). Argument-less astimezone() applies the correct UTC offset for each
    instant, so timestamps across a DST boundary land in the right wall-clock
    slot.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            n = float(s)
            if n > 1e12:  # ms
                n /= 1000.0
            dt = datetime.fromtimestamp(n)
        except (ValueError, OverflowError, OSError):
            return None
    return dt.astimezone()


def _prompt_text(obj):
    """Return the human prompt text from a type=="user" line, or None.

    Tool results also arrive as type=="user" but with a content list of
    tool_result blocks — those are skipped, as are sidechain (subagent) and
    meta lines, and <command>/[interrupt]/[image]-style synthetic content.
    """
    if obj.get("userType") != "external":
        return None
    if obj.get("isSidechain") or obj.get("isMeta"):
        return None
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return None
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        text = " ".join(p for p in parts if p)
    else:
        return None
    text = " ".join(text.split())
    if not text or text[0] in "<[":
        return None
    return text


def _shorten(text):
    w = text.split(" ")
    return " ".join(w[:MAX_WORDS])


def _floor_slot(dt):
    """Floor an aware local datetime to its 2-hour boundary, return ISO string."""
    hour = (dt.hour // SLOT_HOURS) * SLOT_HOURS
    return dt.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def bucket(srcs, excludes):
    """Build {slot_iso: {"prompts": [...], "titles": [...]}} from transcripts."""
    slot_prompts = {}     # slot_iso -> set of prompt strings
    session_slots = {}    # sessionId -> set of slot_iso
    titles = {}           # sessionId -> {"ai-title": str, "custom-title": str}

    for path in _iter_jsonl_files(srcs, excludes):
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
                    if not isinstance(obj, dict):
                        continue
                    kind = obj.get("type")
                    sid = obj.get("sessionId")
                    if kind == "user":
                        text = _prompt_text(obj)
                        if not text:
                            continue
                        dt = _parse_ts(obj.get("timestamp"))
                        if dt is None:
                            continue
                        slot = _floor_slot(dt)
                        slot_prompts.setdefault(slot, set()).add(_shorten(text))
                        if sid:
                            session_slots.setdefault(sid, set()).add(slot)
                    elif kind in ("ai-title", "custom-title") and sid:
                        title = obj.get("aiTitle") or obj.get("customTitle")
                        if title:
                            # last occurrence per kind wins
                            titles.setdefault(sid, {})[kind] = str(title).strip()
        except OSError:
            continue

    slot_titles = {}  # slot_iso -> set of titles
    for sid, slots in session_slots.items():
        t = titles.get(sid, {})
        title = t.get("custom-title") or t.get("ai-title")
        if not title:
            continue
        for slot in slots:
            slot_titles.setdefault(slot, set()).add(title)

    return {
        slot: {
            "prompts": sorted(prompts)[:MAX_PROMPTS_PER_SLOT],
            "titles": sorted(slot_titles.get(slot, ())),
        }
        for slot, prompts in slot_prompts.items()
    }


def _in_horizon(slot_iso, cutoff):
    if cutoff is None:
        return True
    try:
        return datetime.fromisoformat(slot_iso) >= cutoff
    except ValueError:
        return False


def diff(current, previous, cutoff):
    """Changed + vanished slots. `current` is already horizon-filtered.

    Returns {slot_iso: value-or-None}; None marks a vanished slot. Slots in
    `previous` that merely aged out of the horizon are NOT vanished — they
    roll off the snapshot and their calendar events freeze (transcript expiry
    must not delete old calendar events).
    """
    changed = {}
    for slot, value in current.items():
        if previous.get(slot) != value:
            changed[slot] = value
    for slot in previous:
        if slot not in current and _in_horizon(slot, cutoff):
            changed[slot] = None  # vanished
    return changed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", nargs="+", default=[DEFAULT_SRC],
                    help=f"Transcript dirs (globbed for *.jsonl) and/or files "
                         f"(default: {DEFAULT_SRC})")
    ap.add_argument("--exclude", action="append", default=[],
                    help="Skip transcript files whose path contains this "
                         "substring (repeatable). Use it to exclude the "
                         "loop's own project.")
    ap.add_argument("--cache", default=None)
    ap.add_argument("--horizon-days", type=int, default=14,
                    help="Only diff slots newer than this many days; on a "
                         "cold cache this is also the backfill depth "
                         "(0 = unbounded; see module docstring)")
    args = ap.parse_args()

    cache = _cache_dir(args.cache)
    previous = _load_json(os.path.join(cache, "raw.json"), {})
    headlines = _load_json(os.path.join(cache, "headlines.json"), {})

    cutoff = None
    if args.horizon_days > 0:
        cutoff = datetime.now().astimezone() - timedelta(days=args.horizon_days)

    # The snapshot is horizon-filtered too: raw.json only tracks the active
    # window. Old slots roll off it (their calendar events freeze), and
    # re-running with a deeper --horizon-days re-surfaces older slots as new,
    # which is what makes backfill work after the first commit.
    current = {s: v for s, v in bucket(args.src, args.exclude).items()
               if _in_horizon(s, cutoff)}
    changed = diff(current, previous, cutoff)

    out = {"slots": {}}
    for slot, value in sorted(changed.items()):
        # copy: don't leak existing_headline into the snapshot we dump below
        entry = dict(value) if value is not None else {"prompts": [], "titles": [], "vanished": True}
        entry["existing_headline"] = headlines.get(slot)
        out["slots"][slot] = entry

    # The snapshot goes out-of-band: sync.py promotes pending -> raw.json only
    # after a successful push, so the delta survives a failed push.
    pending = os.path.join(cache, "raw.pending.json")
    tmp = pending + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    os.replace(tmp, pending)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
