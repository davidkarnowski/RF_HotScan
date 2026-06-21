#!/usr/bin/env python3
"""
RF HotScan — transmission recorder.

Records each held transmission to a mono 48 kHz / 16-bit PCM WAV with CLEAN CUTS:
recording starts at signal onset and ends at the signal drop (the hold-after-loss
squelch tail is excluded). Metadata (accurate UTC/ISO start+stop, freq, tag, ...)
goes into a SQLite store (`recordings.sqlite`, same conventions as the heatmap's
HeatmapDB) so a future playback panel can browse/query; the WAV bytes stay on disk
referenced by path. One agent-facing JSONL event is emitted per recording.

Used only by the RTL (direct) backend, which owns the audio samples. numpy is
imported lazily (only when audio is actually written).
"""

import os
import json
import wave
import math
import sqlite3
import threading

import clock

# Recordings live in a "recordings/" subdir next to the app (we don't use GQRX
# in direct mode, so ~/.config/gqrx isn't the right home). WAVs + the SQLite
# metadata DB + the JSONL event log all go here, together.
APPDIR = os.path.dirname(os.path.abspath(__file__))
RECORD_DIR = os.path.join(APPDIR, "recordings")
DB_PATH = os.path.join(RECORD_DIR, "recordings.sqlite")
EVENTS = os.path.join(RECORD_DIR, "recordings.events.jsonl")
APP_VERSION = "rfhotscan-rec-1.0"

SAMPLERATE = 48000
CLOSE_CONFIRM_S = 0.35      # signal must stay closed this long to end a transmission
MIN_DUR_S = 0.25            # discard transmissions shorter than this (blips)

_np = None


def _lazy_np():
    global _np
    if _np is None:
        import numpy as np
        _np = np
    return _np


SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  unix_start REAL, unix_stop REAL, iso_start TEXT, iso_stop TEXT,
  duration_s REAL, freq_hz INTEGER, name TEXT, tag TEXT,
  samplerate INTEGER DEFAULT 48000, channels INTEGER DEFAULT 1,
  format TEXT DEFAULT 'pcm_s16le', peak_dbfs REAL, n_frames INTEGER,
  wav_path TEXT, backend TEXT, app_version TEXT,
  transcript TEXT, transcript_engine TEXT, transcript_model TEXT,
  transcript_rt REAL, transcribed_at REAL);
CREATE INDEX IF NOT EXISTS ix_rec_start ON recordings(unix_start);
CREATE INDEX IF NOT EXISTS ix_rec_freq  ON recordings(freq_hz);
"""

# additive columns for DBs created before STT existed (guarded migration)
_MIGRATE = ("transcript TEXT", "transcript_engine TEXT", "transcript_model TEXT",
            "transcript_rt REAL", "transcribed_at REAL")

_REC_COLS = ("id", "unix_start", "unix_stop", "iso_start", "iso_stop",
             "duration_s", "freq_hz", "name", "tag", "samplerate", "channels",
             "format", "peak_dbfs", "n_frames", "wav_path", "backend",
             "app_version")


class RecordingsDB:
    """SQLite store for transmission recordings (mirrors heatmap's HeatmapDB)."""

    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        for col in _MIGRATE:                  # add transcript cols to old DBs
            try:
                self.conn.execute(f"ALTER TABLE recordings ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass                          # already exists
        self.conn.commit()
        self.lock = threading.Lock()

    def set_transcript(self, rec_id, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE recordings SET {cols} WHERE id=?",
                              (*fields.values(), rec_id))
            self.conn.commit()

    def insert(self, m):
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO recordings(unix_start, unix_stop, iso_start,
                   iso_stop, duration_s, freq_hz, name, tag, samplerate,
                   channels, format, peak_dbfs, n_frames, wav_path, backend,
                   app_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (m["unix_start"], m["unix_stop"], m["iso_start"], m["iso_stop"],
                 m["duration_s"], m["freq_hz"], m["name"], m["tag"],
                 m.get("samplerate", SAMPLERATE), m.get("channels", 1),
                 m.get("format", "pcm_s16le"), m["peak_dbfs"], m["n_frames"],
                 m["wav_path"], m.get("backend", "rtl"), APP_VERSION))
            self.conn.commit()
            return cur.lastrowid

    def list(self, limit=200, since=None, freq=None, tag=None):
        q = "SELECT * FROM recordings"
        cond, args = [], []
        if since is not None:
            cond.append("unix_start >= ?"); args.append(since)
        if freq is not None:
            cond.append("freq_hz = ?"); args.append(int(freq))
        if tag is not None:
            cond.append("tag = ?"); args.append(tag)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY unix_start DESC LIMIT ?"
        args.append(int(limit))
        cur = self.conn.execute(q, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get(self, rec_id):
        cur = self.conn.execute("SELECT * FROM recordings WHERE id=?", (rec_id,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([c[0] for c in cur.description], row))

    def delete(self, rec_id):
        rec = self.get(rec_id)
        if rec and rec.get("wav_path"):
            try:
                os.remove(rec["wav_path"])
            except OSError:
                pass
        with self.lock:
            self.conn.execute("DELETE FROM recordings WHERE id=?", (rec_id,))
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()


def _emit(rec):
    try:
        with open(EVENTS, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


class WavRecorder:
    """Records held transmissions to per-transmission WAVs with clean cuts.

    Driven block-by-block from the RTL audio callback:
        rec.arm({"freq_hz","name","tag","backend"})   # at hold start
        rec.feed(audio_f32, is_open, t_unix)           # each demod block
        rec.finalize()                                 # at hold end

    `is_open` is the squelch decision (signal >= threshold). Only open blocks are
    written, so the WAV ends at the signal drop. A close shorter than
    CLOSE_CONFIRM_S does not split a transmission.
    """

    def __init__(self, db=None, outdir=RECORD_DIR, samplerate=SAMPLERATE,
                 log=None, on_record=None, on_start=None):
        self.db = db if db is not None else RecordingsDB()
        self.outdir = outdir
        self.sr = samplerate
        self.log = log or (lambda *_a, **_k: None)
        # called with the inserted recording dict (incl. "id") after each WAV is
        # finalized — the GUI wires this to the transcription service.
        self.on_record = on_record
        # called at signal onset (when the WAV opens) with {wav_path, name, tag,
        # freq_hz, unix_start, iso_start} so the UI can list the transmission
        # live, before it ends. wav_path is the stable key across start/stop/text.
        self.on_start = on_start
        # called with the wav_path when a started transmission is discarded as a
        # blip (< MIN_DUR_S) so the UI can drop the live line it already showed.
        self.on_discard = None
        os.makedirs(outdir, exist_ok=True)
        self._meta = {}
        self._reset()

    def _reset(self):
        self._wav = None
        self._path = None
        self._frames = 0
        self._peak = 0.0
        self._start_unix = None
        self._closed_since = None

    def arm(self, meta):
        # close anything still open from a previous hold, then set new meta
        self.finalize()
        self._meta = dict(meta)

    def feed(self, audio, is_open, t_unix):
        if is_open:
            self._closed_since = None
            if self._wav is None:
                self._open_wav(t_unix)
            self._write(audio)
        elif self._wav is not None:
            if self._closed_since is None:
                self._closed_since = t_unix
            elif t_unix - self._closed_since >= CLOSE_CONFIRM_S:
                self.finalize()           # signal stayed gone -> clean stop

    def finalize(self):
        if self._wav is None:
            self._reset()
            return
        try:
            self._wav.close()
        except Exception:
            pass
        frames, peak, start = self._frames, self._peak, self._start_unix
        path = self._path
        self._reset()
        dur = frames / float(self.sr)
        if dur < MIN_DUR_S:               # discard blips
            try:
                os.remove(path)
            except OSError:
                pass
            if self.on_discard is not None:
                try:
                    self.on_discard(path)     # drop the live UI line we showed
                except Exception:
                    pass
            return
        stop = start + dur                # sample-accurate stop time
        peak_dbfs = 20.0 * math.log10(peak) if peak > 0 else -120.0
        m = {"unix_start": start, "unix_stop": stop,
             "iso_start": clock.utc_iso(start), "iso_stop": clock.utc_iso(stop),
             "duration_s": round(dur, 3), "freq_hz": int(self._meta.get("freq_hz", 0)),
             "name": self._meta.get("name", ""), "tag": self._meta.get("tag", ""),
             "samplerate": self.sr, "channels": 1, "format": "pcm_s16le",
             "peak_dbfs": round(peak_dbfs, 1), "n_frames": frames,
             "wav_path": path, "backend": self._meta.get("backend", "rtl")}
        try:
            m["id"] = self.db.insert(m)
        except Exception:
            pass
        _emit({"event": "recording", **m})
        self.log(f"Recorded {dur:.1f}s {m['name']} -> {os.path.basename(path)}")
        if self.on_record is not None and m.get("id") is not None:
            try:
                self.on_record(m)             # -> transcription service, etc.
            except Exception:
                pass

    # ---- internals ----
    def _open_wav(self, t_unix):
        np = _lazy_np()
        self._start_unix = t_unix
        fname = "%s_%d_%s.wav" % (clock.file_stamp(t_unix),
                                  int(self._meta.get("freq_hz", 0)),
                                  self._meta.get("tag", "") or "NA")
        self._path = os.path.join(self.outdir, fname)
        w = wave.open(self._path, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(self.sr)
        self._wav = w
        self._frames = 0
        self._peak = 0.0
        if self.on_start is not None:
            try:
                self.on_start({"wav_path": self._path,
                               "name": self._meta.get("name", ""),
                               "tag": self._meta.get("tag", ""),
                               "freq_hz": int(self._meta.get("freq_hz", 0)),
                               "unix_start": t_unix,
                               "iso_start": clock.utc_iso(t_unix)})
            except Exception:
                pass

    def _write(self, audio):
        np = _lazy_np()
        a = np.asarray(audio, dtype=np.float32)
        p = float(np.max(np.abs(a))) if a.size else 0.0
        if p > self._peak:
            self._peak = p
        pcm = np.clip(a, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype("<i2").tobytes()
        self._wav.writeframes(pcm)
        self._frames += a.size

    def close(self):
        self.finalize()
        try:
            self.db.close()
        except Exception:
            pass
