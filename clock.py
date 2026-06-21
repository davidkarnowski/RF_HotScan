#!/usr/bin/env python3
"""
RF HotScan — shared time base.

One source of truth for timestamps across the scanner, the heatmap, and the
transmission recorder, so captured data agrees on the clock.

Conventions:
  * Persist UTC epoch seconds (float) in DBs / metadata — unambiguous, sortable.
  * Render ISO-8601 (with timezone) only for display / sidecars.
  * Derive durations from sample counts or monotonic() deltas, never from
    subtracting two wall-clock reads (immune to NTP steps).
"""

import time
import datetime as _dt

# stdlib only — safe to import from the GQRX-only (dependency-free) path too.


def now_unix():
    """Current wall-clock time as UTC epoch seconds (float)."""
    return time.time()


def mono():
    """Monotonic seconds — use for durations, never for absolute time."""
    return time.monotonic()


def utc_iso(t):
    """UTC ISO-8601 with millisecond precision, e.g. 2026-06-11T22:30:01.123Z."""
    dt = _dt.datetime.fromtimestamp(t, _dt.timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def local_iso(t):
    """Local ISO-8601 with offset, e.g. 2026-06-11T15:30:01.123-07:00."""
    dt = _dt.datetime.fromtimestamp(t, _dt.timezone.utc).astimezone()
    return dt.isoformat(timespec="milliseconds")


def now_iso():
    """Local ISO-8601 string for 'now' (the visible wall clock)."""
    return local_iso(now_unix())


def file_stamp(t):
    """Compact UTC stamp safe for filenames, e.g. 20260611T223001Z."""
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
